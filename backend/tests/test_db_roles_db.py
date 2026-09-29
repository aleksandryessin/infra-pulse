"""Runtime PostgreSQL role on a database (SEC-03, SEC-04): grants, refusals, a real login.

Needs ``INFRA_TEST_RECEIVED_DSN`` of a superuser on a disposable cluster (``make
check-db``); every test works in its own schema, dropped after it. Roles are global to
the cluster, so the tests never drop ``infra_pulse_app`` or ``analytics_read``. A test
that logs in as ``infra_pulse_app`` sets LOGIN and a random password through the deploy
path (``migrations_pg.migrate``) and restores the role's previous LOGIN and password
(the stored verifier) afterwards; a helper role gets a unique name and is dropped.
The other database tests run their application code as the same role
(``app_role.app_dsn``); here the grants are compared with ``9000_runtime_role.sql``
and the refusals are checked in a session whose login itself is the runtime role.
All rows are synthetic.
"""

import json
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from app_role import app_dsn, db_role
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_db_roles import decisions, privileges
from test_ingestion_db import (
    NAMESPACE,
    STREAM,
    drain,
    journal,
    load_reference,
    report,
    upload,
)

from infra_pulse_backend.admin.__main__ import main as admin_main
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_backend.storage.migrations_pg import (
    RUNTIME_ROLE,
    apply_migrations,
    enable_runtime_login,
    migrate,
    runtime_dsn,
    runtime_role_problems,
)
from infra_pulse_backend.worker.__main__ import migrate_at_start

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
HISTORY = ("audit_events", "forecast_decisions", "forecast_check_results", "work_order_drafts")


@pytest.fixture
def owner() -> Iterator[str]:
    """A fresh schema migrated twice by the DSN user (the owner); its scoped DSN."""
    base = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not base:
        pytest.skip("local PostgreSQL integration DSN not provided")
    schema = f"roles_{uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(base, options=f"-csearch_path={schema}")
    try:
        for _ in range(2):  # every file is re-applied on each run: idempotent
            apply_migrations(scoped, MIGRATIONS)
        yield scoped
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def login(owner) -> Iterator[str]:
    """DSN of a real login as the runtime role, enabled the way the deploy does it."""
    with psycopg.connect(owner, autocommit=True) as connection:
        superuser = connection.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()[0]
        if not superuser:
            pytest.skip("the login tests need a superuser DSN (they restore the role's password)")
        saved = connection.execute(
            "SELECT rolcanlogin, rolpassword FROM pg_authid WHERE rolname = %s", (RUNTIME_ROLE,)
        ).fetchone()
    password = secrets.token_hex(32)
    try:
        lines = migrate(owner, MIGRATIONS, password)
        assert lines[-1].startswith(f"Runtime role {RUNTIME_ROLE}: LOGIN set, login checked")
        assert password not in "\n".join(lines)
        yield runtime_dsn(owner, password)
    finally:
        can_login, verifier = saved
        with psycopg.connect(owner, autocommit=True) as connection:
            connection.execute(
                sql.SQL("ALTER ROLE {} WITH {} PASSWORD {}").format(
                    sql.Identifier(RUNTIME_ROLE),
                    sql.SQL("LOGIN" if can_login else "NOLOGIN"),
                    sql.Literal(verifier),
                )
            )


def server_version(connection: psycopg.Connection) -> int:
    return int(connection.execute("SHOW server_version_num").fetchone()[0])


def table_privileges(connection: psycopg.Connection, role: str, schema: str) -> dict[str, set[str]]:
    names = list(TABLE_PRIVILEGES) + (["MAINTAIN"] if server_version(connection) >= 170000 else [])
    result = {}
    for relname, oid in connection.execute(
        """SELECT c.relname, c.oid FROM pg_class AS c
           JOIN pg_namespace AS n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')""",
        (schema,),
    ):
        result[relname] = {
            name
            for name in names
            if connection.execute(
                "SELECT has_table_privilege(%s, %s::oid, %s)", (role, oid, name)
            ).fetchone()[0]
        }
    return result


def test_grants_follow_the_decisions_for_every_relation(owner):
    matrix = decisions()
    with psycopg.connect(owner, autocommit=True) as connection:
        schema = connection.execute("SELECT current_schema()").fetchone()[0]
        actual = table_privileges(connection, RUNTIME_ROLE, schema)
        assert set(actual) == set(matrix), "relation without a decision in 9000_runtime_role.sql"
        for relation, value in matrix.items():
            # Table-level privileges; the column-level UPDATE (alarm) is checked below.
            expected = privileges(value) - ({"UPDATE"} if "UPDATE (" in value else set())
            assert actual[relation] == expected, relation
        column = [
            connection.execute(
                "SELECT has_column_privilege(%s, 'dispatch_observations', %s, 'UPDATE')",
                (RUNTIME_ROLE, name),
            ).fetchone()[0]
            for name in ("alarm", "value_raw", "event_at", "channel_id")
        ]
        assert column == [True, False, False, False]
        sequences = connection.execute(
            """SELECT s.relname, t.relname,
                      has_sequence_privilege(%(role)s, s.oid, 'USAGE'),
                      has_sequence_privilege(%(role)s, s.oid, 'SELECT'),
                      has_sequence_privilege(%(role)s, s.oid, 'UPDATE')
               FROM pg_class AS s
               JOIN pg_depend AS d ON d.classid = 'pg_class'::regclass AND d.objid = s.oid
                AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
               JOIN pg_class AS t ON t.oid = d.refobjid
               WHERE s.relkind = 'S' AND s.relnamespace = current_schema()::regnamespace""",
            {"role": RUNTIME_ROLE},
        ).fetchall()
        assert {name for name, *_ in sequences} == {
            "import_files_seq_seq",
            "jobs_job_id_seq",
            "ref_versions_activation_seq_seq",
        }
        for name, table, usage, select, update in sequences:
            assert usage == ("INSERT" in privileges(matrix[table])), name
            assert not select and not update, name  # setval() and currval() of others: no


def test_runtime_role_is_least_privilege(owner):
    with psycopg.connect(owner, autocommit=True) as connection:
        assert runtime_role_problems(connection) == []
        checks = connection.execute(
            """SELECT has_database_privilege(%(role)s, current_database(), 'CONNECT'),
                      has_database_privilege(%(role)s, current_database(), 'TEMPORARY'),
                      has_database_privilege(%(role)s, current_database(), 'CREATE'),
                      has_schema_privilege(%(role)s, current_schema(), 'USAGE'),
                      has_schema_privilege(%(role)s, current_schema(), 'CREATE'),
                      has_schema_privilege(%(role)s, 'analytics', 'USAGE')""",
            {"role": RUNTIME_ROLE},
        ).fetchone()
        assert checks == (True, True, False, True, False, False)
        analytics = table_privileges(connection, RUNTIME_ROLE, "analytics")
        assert analytics and not any(analytics.values())
        if server_version(connection) >= 150000:
            assert not connection.execute(
                "SELECT has_schema_privilege(%s, 'public', 'CREATE')", (RUNTIME_ROLE,)
            ).fetchone()[0]


def test_analytics_read_only_selects_the_analytics_views(owner):
    with psycopg.connect(owner, autocommit=True) as connection:
        schema = connection.execute("SELECT current_schema()").fetchone()[0]
        role = connection.execute(
            """SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication,
                      rolbypassrls, oid
               FROM pg_roles WHERE rolname = 'analytics_read'"""
        ).fetchone()
        assert role[:6] == (False,) * 6
        assert not connection.execute(
            "SELECT count(*) FROM pg_auth_members WHERE member = %s", (role[6],)
        ).fetchone()[0]
        views = table_privileges(connection, "analytics_read", "analytics")
        assert views and all(granted == {"SELECT"} for granted in views.values()), views
        kinds = connection.execute(
            """SELECT DISTINCT relkind::text FROM pg_class
               WHERE relnamespace = 'analytics'::regnamespace"""
        ).fetchall()
        assert kinds == [("v",)]
        operational = table_privileges(connection, "analytics_read", schema)
        assert operational and not any(operational.values())
        assert connection.execute(
            """SELECT has_schema_privilege('analytics_read', 'analytics', 'USAGE'),
                      has_schema_privilege('analytics_read', 'analytics', 'CREATE'),
                      has_schema_privilege('analytics_read', current_schema(), 'CREATE'),
                      has_database_privilege('analytics_read', current_database(), 'CREATE')"""
        ).fetchone() == (True, False, False, False)


def test_application_code_of_the_db_tests_runs_as_the_runtime_role(owner):
    """INFRA_TEST_DB_ROLE=runtime (default) really switches the other DB tests."""
    if db_role() != "runtime":
        pytest.skip("INFRA_TEST_DB_ROLE=owner")
    with psycopg.connect(app_dsn(owner)) as connection:
        assert connection.execute("SELECT current_user").fetchone()[0] == RUNTIME_ROLE


def test_worker_migrates_only_as_the_owner(owner):
    assert migrate_at_start(owner, MIGRATIONS) == len(list(MIGRATIONS.glob("[0-9]*_*.sql")))
    options = conninfo_to_dict(owner).get("options", "")
    as_role = make_conninfo(owner, options=f"{options} -crole={RUNTIME_ROLE}")
    assert migrate_at_start(as_role, MIGRATIONS) is None


def test_migrate_refuses_a_bad_password_and_reports_memberships(owner):
    helper = f"ip_roles_test_{uuid4().hex[:8]}"
    with psycopg.connect(owner, autocommit=True) as connection:
        for bad in ("", "short", "0123456789abcdef" * 2 + " spaces", "p@ss:w/rd" * 4):
            with pytest.raises(ValueError, match="INFRA_DB_APP_PASSWORD"):
                enable_runtime_login(connection, bad)
        connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(helper)))
        try:
            connection.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(helper), sql.Identifier(RUNTIME_ROLE)
                )
            )
            assert runtime_role_problems(connection) == [f"member of {helper}"]
            # The deploy stops before LOGIN or the password of such a role change.
            state = "SELECT rolcanlogin, rolpassword FROM pg_authid WHERE rolname = %s"
            before = connection.execute(state, (RUNTIME_ROLE,)).fetchone()
            with pytest.raises(RuntimeError, match=f"not ready: member of {helper}"):
                migrate(owner, MIGRATIONS, secrets.token_hex(32))
            assert connection.execute(state, (RUNTIME_ROLE,)).fetchone() == before
        finally:
            connection.execute(
                sql.SQL("REVOKE {} FROM {}").format(
                    sql.Identifier(helper), sql.Identifier(RUNTIME_ROLE)
                )
            )
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(helper)))
        assert runtime_role_problems(connection) == []


REFUSED = [
    # DDL on the schema, the database and the tables of the owner
    "CREATE TABLE rogue (id integer)",
    "CREATE VIEW rogue AS SELECT 1",
    "CREATE FUNCTION rogue() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
    "CREATE SCHEMA rogue",
    "CREATE INDEX rogue_idx ON audit_events (action)",
    "ALTER TABLE audit_events ADD COLUMN rogue integer",
    "ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only",
    "ALTER TABLE forecast_decisions DISABLE TRIGGER ALL",
    "DROP TABLE audit_events",
    "DROP TABLE dispatch_observations",
    # history is append-only; source texts are never rewritten; no TRUNCATE anywhere
    *[f"UPDATE {table} SET actor_id = 'someone-else'" for table in HISTORY[:3]],
    "UPDATE work_order_drafts SET note = 'rewritten'",
    *[f"DELETE FROM {table}" for table in HISTORY],
    *[f"TRUNCATE {table}" for table in HISTORY],
    "UPDATE dispatch_observations SET value_raw = 'rewritten'",
    "DELETE FROM dispatch_observations",
    "TRUNCATE dispatch_observations",
    "TRUNCATE jobs",
    "TRUNCATE import_files",
    "SELECT setval('jobs_job_id_seq', 1)",
    # superuser powers of the former DSN user
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    "CREATE EXTENSION IF NOT EXISTS dblink",
    "COPY (SELECT 1) TO PROGRAM 'id'",
    "COPY audit_events FROM '/etc/passwd'",
    "COPY audit_events TO '/tmp/infra-pulse-rogue.csv'",
    "SELECT rolpassword FROM pg_authid",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT lo_import('/etc/passwd')",
    "SET session_replication_role = replica",
    "CREATE ROLE rogue_role",
    f"ALTER ROLE {RUNTIME_ROLE} SUPERUSER",
    f"ALTER ROLE {RUNTIME_ROLE} CREATEROLE",
    # Grafana's schema is not the application's
    "SELECT count(*) FROM analytics.signals",
]


def test_runtime_login_refuses_ddl_history_rewrites_and_superuser_powers(owner, login):
    # The owner and every superuser of this cluster, whatever their names are.
    with psycopg.connect(owner, autocommit=True) as connection:
        owner_user = connection.execute("SELECT current_user").fetchone()[0]
        powerful = {owner_user} | {
            name for (name,) in connection.execute("SELECT rolname FROM pg_roles WHERE rolsuper")
        }
    statements = [
        *REFUSED,
        *(sql.SQL("SET ROLE {}").format(sql.Identifier(name)) for name in sorted(powerful)),
        sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(owner_user)),
    ]
    with psycopg.connect(login, autocommit=True) as connection:
        assert connection.execute("SELECT current_user, session_user").fetchone() == (
            RUNTIME_ROLE,
            RUNTIME_ROLE,
        )
        for statement in statements:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement)
        # What the worker and the API do need still works in the same session.
        connection.execute("CREATE TEMP TABLE scratch (id integer) ON COMMIT DROP")
        connection.execute("LISTEN infra_import_jobs")
        connection.execute("NOTIFY infra_import_jobs")
        assert connection.execute("SELECT nextval('jobs_job_id_seq')").fetchone()[0] >= 1
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(1)")
            connection.execute("SELECT job_id FROM jobs FOR UPDATE SKIP LOCKED").fetchall()
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] >= 0


def test_real_login_runs_upload_worker_token_and_observation_paths(
    owner, login, tmp_path, monkeypatch, capsys
):
    settings = Settings(
        mode="received",
        db_dsn=login,
        upload_dir=tmp_path / "uploads",
        received_namespace=NAMESPACE,
        received_stream_id=STREAM,
        _env_file=None,
    )
    client = TestClient(create_app(settings))
    reference = load_reference(client, settings, {"710001": "5101", "710002": "5101"})
    assert reference.status == "published"
    rows = [
        ("9100001", "710001", "2026-09-20", "03:09:27", False, "Норма"),
        ("9100002", "710002", "2026-09-20", "10:00:00", True, "Неисправен"),
    ]
    queued = upload(client, "journal-roles.csv", journal(rows))
    assert drain(settings) == 1
    assert report(client, queued.id).status == "imported"
    assert migrate_at_start(login, MIGRATIONS) is None

    monkeypatch.setenv("INFRA_DB_DSN", login)
    monkeypatch.chdir(Path(__file__).parent)  # no stray .env
    created = ["--actor", "admin-roles", "token", "create", "--name", "roles-1", "--json"]
    assert admin_main(created) == 0
    token = json.loads(capsys.readouterr().out)["token"]
    batch = {
        "batch_id": f"roles-{uuid4().hex[:8]}",
        "records": [
            {
                "event_id": "9100003",
                "channel_id": "710001",
                "date": "2026-09-20",
                "time": "11:00:00",
                "alarm": False,
                "value": "Обрыв; ТО",
            }
        ],
    }
    response = client.post(
        "/api/v1/observations",
        content=json.dumps(batch, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert response.status_code == 202, response.text
    assert drain(settings) == 1
    assert client.get("/health/ready").status_code == 200

    with psycopg.connect(owner) as connection:
        texts = sorted(
            value
            for (value,) in connection.execute(
                "SELECT value_raw FROM dispatch_observations WHERE snapshot_id = %s", (STREAM,)
            )
        )
        actions = {
            action for (action,) in connection.execute("SELECT DISTINCT action FROM audit_events")
        }
    assert texts == ["Неисправен", "Норма", "Обрыв; ТО"]
    assert {
        "import.uploaded",
        "import.status_changed",
        "integration_token.created",
        "observations.received",
    } <= actions
