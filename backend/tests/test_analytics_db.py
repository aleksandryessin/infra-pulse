"""PostgreSQL path of the Grafana analytics layer (OPS-06, migration 0016, OPS-G).

Role ``analytics_read`` reads the views of schema ``analytics`` and nothing else:
every operational table (observations, imports, references, jobs, sessions,
decisions, drafts, audit and, after B2, forecasts) is refused. The views keep the
source text verbatim and split numeric values, text states ordered by the state
reference and records with the source ``alarm`` flag (not a confirmed failure;
«Неисправен» stays technical_fault_state_proxy). The Grafana login created by
``deploy/grafana/create-reader.sql`` is a read-only member with a statement timeout.

The provisioned dashboards (``deploy/grafana/dashboards``) are checked statically, and
every query of them (selectors, panels, annotations) runs as the Grafana login with the
variables substituted as Grafana 12 does, for filled, single, cleared and empty
selections (migration 0020: directory fallback to the forecast layout, dashboard indexes).

Needs ``INFRA_TEST_RECEIVED_DSN`` on a disposable database (``make check-db``); the static
checks run without it. The operational tables live in a per-test schema that, like
``public``, grants USAGE to PUBLIC, so a refusal comes from the missing table privilege.
All rows are synthetic.
"""

import hashlib
import json
import os
import re
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from infra_pulse_backend.ingestion.journal_csv import parse_journal
from infra_pulse_backend.ingestion.load_pg import load_journal, load_reference
from infra_pulse_backend.ingestion.reference_csv import parse_reference
from infra_pulse_backend.worker import __main__ as worker_main

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "backend" / "migrations"
READER_SQL = ROOT / "deploy" / "grafana" / "create-reader.sql"
DASHBOARDS = ROOT / "deploy" / "grafana" / "dashboards"
DASHBOARD_FILES = ("signals-state.json", "signals-numeric.json")
DATASOURCE_UID = "infrapulse-analytics"
NAMESPACE = "opsg-test"
STREAM = "opsg-stream"
VIEWS = (
    "scopes",
    "channel_versions",
    "objects",
    "channels",
    "states",
    "signals",
    "signal_numeric",
    "signal_state",
    "source_alarms",
)
PASSWORD = "synthetic-reader-password-0123456789"
HOUSE_1 = "Синтетический дом 1"

CHANNELS = """\
"ид_канала_данных","тип_инж_системы","тип_датчика","тег_инженерной_системы","название_датчика","ид_объект"
syn-gas-1,Газоснабжение,Газовый датчик,"91-1.1.",Датчик газа ПК1,syn-obj-1
syn-temp-1,Теплоснабжение,Датчик температуры,"92-1.1.",Температура ИТП,syn-obj-1
syn-pump-1,Водоснабжение,Состояние насоса,"93-1.1.",Насос 1,syn-obj-1
syn-amb-1,Вентиляция,Состояние вентилятора,"94-1.1.",Вентилятор 1,syn-obj-2
syn-amb-1,Вентиляция,Состояние вентилятора,"94-1.2.",Вентилятор 1 (дубль),syn-obj-2
syn-conf-1,Вентиляция,Датчик температуры,"95-1.1.",Температура подвала,syn-obj-1
syn-conf-1,Вентиляция,Датчик температуры,"95-1.1.",Температура подвала,syn-obj-2
"""
OBJECTS = """\
"ид_объект","иерархия_уровень","родитель","вид_объекта","диспетчерское_название_объекта"
syn-obj-1,3,syn-root,Дом,Синтетический дом 1
syn-obj-2,3,syn-root,Дом,Синтетический дом 2
"""
STATES = """\
"тип_датчика","ид_набор_состояний","название_состояния","тревожное"
Состояние насоса,syn-s1,Норма,false
Состояние насоса,syn-s1,Неисправен,true
Состояние насоса,syn-s1,Обесточен,true
Газовый датчик,syn-s2,Обнаружен газ,true
Газовый датчик,syn-s2,Неисправен,true
Газовый датчик,syn-s3,Неисправен,false
"""
JOURNAL_HEADER = '"ид_события","ид_канала_данных","дата","время","тревожное","значение_датчика"'
# (event, channel, time on 2026-09-20, alarm, value): numbers, verbatim texts, a text
# outside the state reference, a channel with two names, a channel with two objects
# (mapping conflict) and an unknown channel.
RECORDS = (
    (1, "syn-gas-1", "10:00:00", "false", "0.35"),
    (2, "syn-gas-1", "10:05:00", "true", "Обнаружен газ"),
    (3, "syn-gas-1", "10:06:00", "true", "Неисправен"),
    (4, "syn-gas-1", "10:07:00", "false", "1,5"),
    (5, "syn-temp-1", "10:00:00", "false", "21.5"),
    (6, "syn-temp-1", "11:00:00", "false", "-3"),
    (7, "syn-pump-1", "10:00:00", "false", "Норма"),
    (8, "syn-pump-1", "10:30:00", "true", "Неисправен"),
    (9, "syn-pump-1", "10:45:00", "true", "Затоплен"),
    (10, "syn-amb-1", "10:00:00", "false", "12.5"),
    (11, "syn-unknown-1", "10:00:00", "false", "Норма"),
    (12, "syn-conf-1", "10:00:00", "false", "7.25"),
)


def journal_csv() -> bytes:
    lines = [JOURNAL_HEADER]
    for event, channel, clock, alarm, value in RECORDS:
        lines.append(f'{event},{channel},"2026-09-20","{clock}",{alarm},"{value}"')
    return ("\n".join(lines) + "\n").encode()


def register(connection: psycopg.Connection, fmt: str, name: str, data: bytes) -> tuple[str, str]:
    import_id = f"opsg-{uuid4().hex[:12]}"
    digest = hashlib.sha256(data).hexdigest()
    connection.execute(
        """INSERT INTO import_files
           (import_id, format, file_name, sha256, size_bytes, stored_name, uploaded_by, status)
           VALUES (%s, %s, %s, %s, %s, %s, 'test', 'queued')""",
        (import_id, fmt, name, digest, len(data), f"{import_id}.csv"),
    )
    return import_id, digest


def seed(dsn: str) -> None:
    with psycopg.connect(dsn) as connection:
        for fmt, kind, text in (
            ("reference_channels_csv", "channels", CHANNELS),
            ("reference_objects_csv", "objects", OBJECTS),
            ("reference_states_csv", "states", STATES),
        ):
            data = text.encode()
            import_id, digest = register(connection, fmt, f"{kind}.csv", data)
            load_reference(
                connection, import_id=import_id, parsed=parse_reference(data, kind), sha256=digest
            )
        data = journal_csv()
        import_id, digest = register(connection, "journal_csv", "journal.csv", data)
        load_journal(
            connection,
            import_id=import_id,
            parsed=parse_journal(data),
            file_name="journal.csv",
            sha256=digest,
            namespace_id=NAMESPACE,
            stream_id=STREAM,
        )


@pytest.fixture
def db():
    base = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not base:
        pytest.skip("local PostgreSQL integration DSN not provided")
    schema = f"opsg_{uuid4().hex[:12]}"
    readers: list[str] = []
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        # Same as the default `public` schema: anyone may look up names in it.
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO PUBLIC").format(sql.Identifier(schema))
        )
    scoped = make_conninfo(base, options=f"-csearch_path={schema}")
    for _ in range(2):  # the loader and the worker re-apply every file: idempotent
        worker_main.apply_migrations(scoped, MIGRATIONS)
    seed(scoped)
    try:
        yield {"base": base, "dsn": scoped, "schema": schema, "readers": readers}
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            # The analytics views of this run point to the schema and go with it.
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            for reader in readers:
                connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(reader)))


def rows(dsn: str, query: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(dsn) as connection:
        return connection.execute(query, params).fetchall()


def as_role(dsn: str, role: str, query: str) -> list[tuple]:
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        return connection.execute(query).fetchall()


def operational_tables(dsn: str, schema: str) -> list[str]:
    return [
        name
        for (name,) in rows(
            dsn,
            """SELECT relname FROM pg_class
               WHERE relnamespace = %s::regnamespace AND relkind IN ('r', 'p', 'v', 'm')
               ORDER BY relname""",
            (schema,),
        )
    ]


def create_reader(dsn: str, reader: str, password: str = PASSWORD) -> None:
    """What create-reader.sh sends to psql: the two settings, then the SQL file."""
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            "SELECT set_config('infrapulse.reader', %s, false),"
            " set_config('infrapulse.reader_password', %s, false)",
            (reader, password),
        )
        connection.execute(READER_SQL.read_text(encoding="utf-8"))


def test_views_keep_texts_verbatim_and_split_numbers_states_alarms(db):
    dsn = db["dsn"]
    numeric = rows(
        dsn,
        """SELECT channel_id, channel_name, object_name, sensor_type, value_numeric, value_raw
           FROM analytics.signal_numeric
           WHERE namespace_id = %s AND snapshot_id = %s ORDER BY channel_id, event_at""",
        (NAMESPACE, STREAM),
    )
    assert numeric == [
        # Two names in the reference: mapping kept (as in the loader), no name.
        ("syn-amb-1", None, "Синтетический дом 2", "Состояние вентилятора", 12.5, "12.5"),
        # Two objects: stored without object and type (reference_conflict), name kept.
        ("syn-conf-1", "Температура подвала", None, None, 7.25, "7.25"),
        ("syn-gas-1", "Датчик газа ПК1", HOUSE_1, "Газовый датчик", 0.35, "0.35"),
        ("syn-temp-1", "Температура ИТП", HOUSE_1, "Датчик температуры", 21.5, "21.5"),
        ("syn-temp-1", "Температура ИТП", HOUSE_1, "Датчик температуры", -3.0, "-3"),
    ]
    states = rows(
        dsn,
        """SELECT channel_id, value_raw, state_order, reference_alarm, in_state_reference,
                  source_alarm
           FROM analytics.signal_state
           WHERE namespace_id = %s AND snapshot_id = %s ORDER BY channel_id, event_at""",
        (NAMESPACE, STREAM),
    )
    assert states == [
        ("syn-gas-1", "Обнаружен газ", 1, True, True, True),
        # Listed with both alarm values in the reference: order kept, alarm unknown.
        ("syn-gas-1", "Неисправен", 2, None, True, True),
        # Comma decimal stays text (journal rule), outside the reference.
        ("syn-gas-1", "1,5", None, None, False, False),
        ("syn-pump-1", "Норма", 1, False, True, False),
        ("syn-pump-1", "Неисправен", 2, True, True, True),
        ("syn-pump-1", "Затоплен", None, None, False, True),
        ("syn-unknown-1", "Норма", None, None, False, False),
    ]
    alarms = rows(
        dsn,
        """SELECT channel_id, value_raw FROM analytics.source_alarms
           WHERE namespace_id = %s AND snapshot_id = %s ORDER BY event_at, channel_id""",
        (NAMESPACE, STREAM),
    )
    assert alarms == [
        ("syn-gas-1", "Обнаружен газ"),
        ("syn-gas-1", "Неисправен"),
        ("syn-pump-1", "Неисправен"),
        ("syn-pump-1", "Затоплен"),
    ]
    total = rows(
        dsn,
        "SELECT count(*) FROM analytics.signals WHERE namespace_id = %s AND snapshot_id = %s",
        (NAMESPACE, STREAM),
    )[0][0]
    assert total == len(RECORDS)


def test_reference_views_feed_the_dashboard_selectors(db):
    dsn = db["dsn"]
    channels = rows(
        dsn,
        """SELECT channel_id, channel_name, sensor_type, object_id, object_name, is_ambiguous
           FROM analytics.channels ORDER BY channel_id""",
    )
    assert channels == [
        ("syn-amb-1", None, "Состояние вентилятора", "syn-obj-2", "Синтетический дом 2", False),
        ("syn-conf-1", "Температура подвала", None, None, None, True),
        ("syn-gas-1", "Датчик газа ПК1", "Газовый датчик", "syn-obj-1", HOUSE_1, False),
        ("syn-pump-1", "Насос 1", "Состояние насоса", "syn-obj-1", HOUSE_1, False),
        (
            "syn-temp-1",
            "Температура ИТП",
            "Датчик температуры",
            "syn-obj-1",
            "Синтетический дом 1",
            False,
        ),
    ]
    states = rows(
        dsn,
        """SELECT sensor_type, state_order, state_name, reference_alarm, alarm_conflict,
                  state_set_ids
           FROM analytics.states ORDER BY sensor_type, state_order""",
    )
    assert states == [
        ("Газовый датчик", 1, "Обнаружен газ", True, False, "syn-s2"),
        ("Газовый датчик", 2, "Неисправен", None, True, "syn-s2, syn-s3"),
        ("Состояние насоса", 1, "Норма", False, False, "syn-s1"),
        ("Состояние насоса", 2, "Неисправен", True, False, "syn-s1"),
        ("Состояние насоса", 3, "Обесточен", True, False, "syn-s1"),
    ]
    scopes = rows(dsn, "SELECT namespace_id, snapshot_id, scope_kind FROM analytics.scopes")
    assert (NAMESPACE, STREAM, "received") in scopes


def test_analytics_read_sees_only_the_analytics_views(db):
    dsn, schema = db["dsn"], db["schema"]
    role = rows(
        dsn,
        """SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls
           FROM pg_roles WHERE rolname = 'analytics_read'""",
    )
    assert role == [(False, False, False, False, False)]
    for view in VIEWS:
        as_role(dsn, "analytics_read", f"SELECT count(*) FROM analytics.{view}")
    tables = operational_tables(dsn, schema)
    assert {"dispatch_observations", "import_files", "ref_channels", "audit_events"} <= set(tables)
    assert {"auth_sessions", "forecast_decisions", "work_order_drafts", "jobs"} <= set(tables)
    for table in tables:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_role(dsn, "analytics_read", f'SELECT 1 FROM "{schema}"."{table}" LIMIT 1')
    granted = rows(
        dsn,
        """SELECT relname FROM pg_class
           WHERE relnamespace IN (%s::regnamespace, 'public'::regnamespace)
             AND relkind IN ('r', 'p', 'v', 'm')
             AND has_table_privilege('analytics_read', oid, 'SELECT, INSERT, UPDATE, DELETE')""",
        (schema,),
    )
    assert granted == []
    # Read only inside analytics too: no DDL, and a simple (updatable) view refuses writes.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_role(dsn, "analytics_read", "CREATE TABLE analytics.scratch (id int)")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_role(
            dsn,
            "analytics_read",
            "UPDATE analytics.scopes SET row_count = 0 RETURNING namespace_id",
        )
    settings = rows(
        dsn,
        """SELECT unnest(setconfig) FROM pg_db_role_setting
           WHERE setrole = 'analytics_read'::regrole AND setdatabase = 0 ORDER BY 1""",
    )
    assert settings == [("default_transaction_read_only=on",), ("statement_timeout=10s",)]


def test_grafana_login_is_a_read_only_member(db):
    dsn, base, schema = db["dsn"], db["base"], db["schema"]
    reader = f"opsg_reader_{uuid4().hex[:8]}"
    db["readers"].append(reader)
    create_reader(base, reader)
    create_reader(base, reader)  # re-run: password and settings updated, no error
    role = rows(
        dsn,
        """SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolinherit, rolconnlimit,
                  pg_has_role(oid, 'analytics_read', 'MEMBER')
           FROM pg_roles WHERE rolname = %s""",
        (reader,),
    )
    assert role == [(True, False, False, False, True, 5, True)]
    settings = rows(
        dsn,
        """SELECT unnest(setconfig) FROM pg_db_role_setting
           WHERE setrole = %s::regrole AND setdatabase = 0 ORDER BY 1""",
        (reader,),
    )
    assert settings == [
        ("default_transaction_read_only=on",),
        ("idle_in_transaction_session_timeout=60s",),
        ("statement_timeout=10s",),
    ]
    # A real login gets the settings; views work, operational tables are refused.
    login = make_conninfo(dsn, user=reader, password=PASSWORD)
    with psycopg.connect(login) as connection:
        assert connection.execute("SHOW default_transaction_read_only").fetchone() == ("on",)
        assert connection.execute("SHOW statement_timeout").fetchone() == ("10s",)
        assert connection.execute(
            "SELECT count(*) FROM analytics.signal_numeric WHERE snapshot_id = %s", (STREAM,)
        ).fetchone() == (5,)
    with psycopg.connect(login) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(f'SELECT 1 FROM "{schema}".dispatch_observations LIMIT 1')
    with psycopg.connect(login) as connection:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("CREATE TEMP TABLE scratch (id int)")


@pytest.mark.parametrize(
    ("setup", "reader", "password", "message"),
    [
        (None, "Bad-Name", PASSWORD, "must match"),
        (None, "opsg_short", "short-password", "at least 24"),
        ("CREATE ROLE {} LOGIN CREATEDB", None, PASSWORD, "privileged role"),
        ("CREATE ROLE {} LOGIN IN ROLE pg_read_all_data", None, PASSWORD, "other memberships"),
    ],
)
def test_create_reader_refuses_unsafe_logins(db, setup, reader, password, message):
    base = db["base"]
    if reader is None:
        reader = f"opsg_reader_{uuid4().hex[:8]}"
        db["readers"].append(reader)
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(sql.SQL(setup).format(sql.Identifier(reader)))
    with pytest.raises(psycopg.errors.RaiseException, match=message):
        create_reader(base, reader, password)
    current = rows(base, "SELECT current_user")[0][0]
    with pytest.raises(psycopg.errors.RaiseException, match="separate role"):
        create_reader(base, current)


# ---- Grafana dashboards (OPS-G, migration 0020) -------------------------------------------

# Window of the synthetic journal (2026-09-20, Moscow) as Grafana sends it: UTC, RFC 3339.
WINDOW = ("2026-09-19T21:00:00Z", "2026-09-20T21:00:00Z")
LATER_WINDOW = ("2026-09-22T21:00:00Z", "2026-09-29T21:00:00Z")


def load_dashboard(name: str) -> dict:
    return json.loads((DASHBOARDS / name).read_text(encoding="utf-8"))


def dashboard_queries(dashboard: dict) -> list[tuple[str, str]]:
    """Every SQL text of a dashboard: selectors, annotations and panel targets."""
    found = [
        (f"variable {variable['name']}", variable["query"])
        for variable in dashboard["templating"]["list"]
        if variable["type"] == "query"
    ]
    found += [
        (f"annotation {annotation['name']}", annotation["target"]["rawSql"])
        for annotation in dashboard["annotations"]["list"]
        if "target" in annotation
    ]
    found += [
        (f"panel {panel['id']}", target["rawSql"])
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]
    return found


def grafana_sql(text: str, values: dict, window: tuple[str, str] = WINDOW) -> str:
    """The SQL Grafana 12 sends for the dashboards: ``${name:sqlstring}`` is every value
    quoted with '' escaping and joined by commas, so an empty multi-value selection
    becomes nothing; ``$__timeFilter(column)`` is ``column BETWEEN from AND to`` and
    ``$__timeFrom()`` is the quoted start of the window."""

    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def substitute(match: re.Match) -> str:
        value = values[match.group(1)]
        return ",".join(map(quote, value)) if isinstance(value, list) else quote(value)

    text = re.sub(
        r"\$__timeFilter\(([^)]+)\)",
        lambda match: f"{match.group(1)} BETWEEN '{window[0]}' AND '{window[1]}'",
        text,
    )
    text = text.replace("$__timeFrom()", quote(window[0]))
    return re.sub(r"\$\{(\w+):sqlstring\}", substitute, text)


@pytest.mark.parametrize("name", DASHBOARD_FILES)
def test_dashboard_sql_never_expands_to_an_empty_list(name):
    """Static rules behind the stand errors of 28.09: `IN ()` from an empty selection
    (SQLSTATE 42601) and selectors that return no rows (Grafana: «at least one field
    expected for variable»). Runs without a database."""
    dashboard = load_dashboard(name)
    variables = {variable["name"]: variable for variable in dashboard["templating"]["list"]}
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    for where, text in dashboard_queries(dashboard):
        assert not re.search(r"\bIN\s*\(\s*\$", text), (name, where)
        assert "[[" not in text, (name, where)
        references = re.findall(r"\$\{(\w+)(?::(\w+))?\}", text)
        assert all(fmt == "sqlstring" and ref in variables for ref, fmt in references), where
        rest = re.sub(r"\$\{\w+:sqlstring\}|\$__timeFilter\(|\$__timeFrom\(\)", "", text)
        assert "$" not in rest, (name, where)  # no bare $name or other macros
        for ref in {ref for ref, _ in references if variables[ref].get("multi")}:
            # A multi-value list always sits in an array, so no selection is ARRAY[]::text[].
            assert text.count(f"${{{ref}:sqlstring}}") == text.count(
                f"ARRAY[${{{ref}:sqlstring}}]::text[]"
            ), (name, where, ref)
    for variable in variables.values():
        if variable["type"] == "query":
            # Two string fields and a placeholder row when nothing matches.
            assert "__text" in variable["query"] and "__value" in variable["query"]
            assert "WHERE NOT EXISTS" in variable["query"], variable["name"]
    for panel in dashboard["panels"]:
        if panel.get("targets"):
            assert panel["datasource"]["uid"] == DATASOURCE_UID
            # An empty panel says why instead of a bare «No data».
            assert panel["fieldConfig"]["defaults"].get("noValue"), panel["title"]


def add_forecast_layout(dsn: str) -> None:
    """Channel layout and object names as the stand seed writes them (migration 0013):
    one channel also in the reference (the reference wins), one only in the layout."""
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """INSERT INTO forecast_objects (object_id, object_name, reference_version)
               VALUES ('syn-obj-1', 'Имя из раскладки', 'syn-layout'),
                      ('syn-obj-3', 'Синтетический дом 3', 'syn-layout')"""
        )
        connection.execute(
            """INSERT INTO forecast_channel_layout
                 (channel_id, object_id, sensor_type, system_type, name, tag, picket_form,
                  layout_version, reference_version)
               VALUES ('syn-gas-1', 'syn-obj-3', 'Газовый датчик', 'Газоснабжение',
                       'Имя из раскладки', '91', 'unknown', 'syn', 'syn-layout'),
                      ('syn-unknown-1', 'syn-obj-3', 'Состояние насоса', 'Водоснабжение',
                       'Насос из раскладки', '99', 'unknown', 'syn', 'syn-layout')"""
        )


def test_directory_falls_back_to_the_forecast_layout(db):
    dsn, schema = db["dsn"], db["schema"]
    add_forecast_layout(dsn)
    channels = rows(
        dsn,
        """SELECT channel_id, channel_name, sensor_type, object_id, object_name, is_ambiguous
           FROM analytics.channels WHERE channel_id IN ('syn-gas-1', 'syn-unknown-1')
           ORDER BY channel_id""",
    )
    assert channels == [
        ("syn-gas-1", "Датчик газа ПК1", "Газовый датчик", "syn-obj-1", HOUSE_1, False),
        (
            "syn-unknown-1",
            "Насос из раскладки",
            "Состояние насоса",
            "syn-obj-3",
            "Синтетический дом 3",
            False,
        ),
    ]
    assert rows(dsn, "SELECT count(*) FROM analytics.channels")[0][0] == 6
    objects = rows(dsn, "SELECT object_id, object_name FROM analytics.objects ORDER BY 1")
    assert objects == [
        ("syn-obj-1", HOUSE_1),
        ("syn-obj-2", "Синтетический дом 2"),
        ("syn-obj-3", "Синтетический дом 3"),
    ]
    # The record keeps its verbatim text; the name now comes from the layout.
    names = rows(
        dsn,
        """SELECT channel_name, value_raw FROM analytics.signal_state
           WHERE snapshot_id = %s AND channel_id = 'syn-unknown-1'""",
        (STREAM,),
    )
    assert names == [("Насос из раскладки", "Норма")]
    indexes = rows(
        dsn,
        """SELECT indexname FROM pg_indexes
           WHERE schemaname = %s AND tablename = 'dispatch_observations'
             AND indexname IN ('dispatch_observations_event_time_idx',
                               'dispatch_observations_numeric_idx')
           ORDER BY 1""",
        (schema,),
    )
    assert indexes == [
        ("dispatch_observations_event_time_idx",),
        ("dispatch_observations_numeric_idx",),
    ]


def run_dashboard(connection: psycopg.Connection, dashboard: dict, pick, window=WINDOW) -> dict:
    """Refresh the selectors in order as Grafana does and run every panel and annotation.

    ``pick(variable, options)`` returns the selection; every selector must return at
    least one row with the fields __text and __value."""
    values: dict = {}
    options: dict = {}
    for variable in dashboard["templating"]["list"]:
        cursor = connection.execute(grafana_sql(variable["query"], values, window))
        assert [column.name for column in cursor.description] == ["__text", "__value"]
        options[variable["name"]] = cursor.fetchall()
        assert options[variable["name"]], variable["name"]
        values[variable["name"]] = pick(variable, options[variable["name"]])
    results = {
        where: connection.execute(grafana_sql(text, values, window)).fetchall()
        for where, text in dashboard_queries(dashboard)
        if not where.startswith("variable")
    }
    return {"options": options, "values": values, "results": results}


def select_all(variable, options):
    if not variable.get("multi"):
        return options[0][1]
    return [value for _, value in options]


def select_first(variable, options):
    return [options[0][1]] if variable.get("multi") else options[0][1]


def select_none(variable, options):
    """Every chip removed from multi-value selectors."""
    return [] if variable.get("multi") else options[0][1]


def select_missing_scope(variable, options):
    if variable["name"] in ("namespace", "snapshot"):
        return "нет такой области"
    return [] if variable.get("multi") else options[0][1]


def grafana_login(db) -> str:
    reader = f"opsg_reader_{uuid4().hex[:8]}"
    db["readers"].append(reader)
    create_reader(db["base"], reader)
    return make_conninfo(db["dsn"], user=reader, password=PASSWORD)


def test_text_dashboard_queries_run_for_every_selection(db):
    add_forecast_layout(db["dsn"])
    dashboard = load_dashboard("signals-state.json")
    with psycopg.connect(grafana_login(db), autocommit=True) as connection:
        filled = run_dashboard(connection, dashboard, select_all)
        assert filled["values"]["namespace"] == NAMESPACE
        assert filled["values"]["snapshot"] == STREAM
        # Objects of the directory with records in the scope; syn-obj-3 has none.
        assert filled["values"]["object"] == ["syn-obj-1", "syn-obj-2"]
        assert filled["values"]["sensor_type"] == [
            "Газовый датчик",
            "Датчик температуры",
            "Состояние вентилятора",
            "Состояние насоса",
        ]
        timeline = filled["results"]["panel 2"]
        # Text records in time order, labelled «name · ID», texts verbatim.
        assert [(channel, state) for _, channel, state in timeline] == [
            ("Насос 1 · syn-pump-1", "Норма"),
            ("Датчик газа ПК1 · syn-gas-1", "Обнаружен газ"),
            ("Датчик газа ПК1 · syn-gas-1", "Неисправен"),
            ("Датчик газа ПК1 · syn-gas-1", "1,5"),
            ("Насос 1 · syn-pump-1", "Неисправен"),
            ("Насос 1 · syn-pump-1", "Затоплен"),
        ]
        texts = {(row[0], row[1]): row[2:5] for row in filled["results"]["panel 3"]}
        assert texts[("Насос 1 · syn-pump-1", "Неисправен")] == (2, 1, 1)
        assert texts[("Насос 1 · syn-pump-1", "Затоплен")] == (None, 1, 1)
        assert len(filled["results"]["annotation alarm источника"]) == 4
        first, last, records, in_window = filled["results"]["panel 5"][0][:4]
        assert (first.day, last.day, records) == (20, 20, len(RECORDS))
        assert in_window == str(len(timeline))  # text records of the chosen channels
        single = run_dashboard(connection, dashboard, select_first)
        assert single["values"]["object"] == ["syn-obj-1"]
        for pick in (select_none, select_missing_scope):
            empty = run_dashboard(connection, dashboard, pick)
            assert empty["options"]["channel"] == [("— нет каналов с записями —", "")]
            # One labelled row: an empty state timeline would read «no time field».
            assert [row[1:] for row in empty["results"]["panel 2"]] == [
                ("— нет текстовых записей в окне —", "сравните окно с датами в «Область данных»")
            ]
            assert empty["results"]["panel 3"] == []
        missing = run_dashboard(connection, dashboard, select_missing_scope)
        assert missing["options"]["object"][0][1] == ""
        assert missing["results"]["panel 5"][0][:3] == (None, None, None)


def test_numeric_dashboard_queries_run_for_every_selection(db):
    add_forecast_layout(db["dsn"])
    dashboard = load_dashboard("signals-numeric.json")
    with psycopg.connect(grafana_login(db), autocommit=True) as connection:
        filled = run_dashboard(connection, dashboard, select_all)
        # Only types and channels with numeric values in the window.
        assert filled["values"]["sensor_type"] == [
            "Газовый датчик",
            "Датчик температуры",
            "Состояние вентилятора",
        ]
        assert sorted(filled["values"]["channel"]) == ["syn-amb-1", "syn-gas-1", "syn-temp-1"]
        series = filled["results"]["panel 2"]
        assert [time for time, _, _ in series] == sorted(time for time, _, _ in series)
        assert sorted((metric, value) for _, metric, value in series) == [
            ("syn-amb-1 · syn-amb-1", 12.5),  # two names in the reference: ID only
            ("Датчик газа ПК1 · syn-gas-1", 0.35),
            ("Температура ИТП · syn-temp-1", -3.0),
            ("Температура ИТП · syn-temp-1", 21.5),
        ]
        table = filled["results"]["panel 3"]
        assert {(row[1], row[4]) for row in table} == {
            ("Синтетический дом 2", "12.5"),
            (HOUSE_1, "0.35"),
            (HOUSE_1, "21.5"),
            (HOUSE_1, "-3"),
        }
        assert filled["results"]["panel 5"][0][3:5] == ("есть", "4")
        # A window without numbers: the selectors say so, the panels stay empty.
        later = run_dashboard(connection, dashboard, select_all, LATER_WINDOW)
        assert later["options"]["sensor_type"] == [("— нет числовых записей в окне —", "")]
        assert later["options"]["channel"] == [("— нет каналов с числовыми записями в окне —", "")]
        assert later["results"]["panel 2"] == [] and later["results"]["panel 3"] == []
        for pick in (select_first, select_none, select_missing_scope):
            run_dashboard(connection, dashboard, pick)
