"""PostgreSQL path of N1 notifications: critical records of the observation scope.

Needs ``INFRA_TEST_RECEIVED_DSN`` on a disposable database (``make check-db``); every test
works in its own schema, dropped afterwards. Observation rows are synthetic and inserted
directly. The published forecast state and journal are the synthetic fixture standing in
for B2 (``forecast_db`` is patched), so the stand clock is the fixture's ``data_as_of``.
Checks: exact counts and the newest 50, the policy texts (gas at ``alarm=false`` counts,
doors and «Неисправен» do not), nothing later than the stand clock (negative test), the
received/replay availability rule, the «похоже на ППР или ТО» series across ``since``,
object names from the reference, channel names of the members from the channel layout and
the latency on 100 000 rows.
"""

import os
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from app_role import app_dsn
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from infra_pulse_backend.api import forecast_db, forecast_fixture, notifications_db
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_backend.operations.notifications import NotificationWindowError, new_cards
from infra_pulse_backend.storage import notifications_pg
from infra_pulse_backend.worker import __main__ as worker_main
from infra_pulse_core.contracts.notifications import NotificationSummary

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
NAMESPACE = "n1-test"
SCOPE = "n1-scope"
STATE = forecast_fixture.forecast_state()
DATA_AS_OF = STATE.data_as_of
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_COLUMNS = (
    "namespace_id, snapshot_id, row_uid, channel_id, object_id, sensor_type, system_type, "
    "value_raw, alarm, event_at, available_at, availability_basis, source_file, "
    "source_sha256, record_ordinal, event_local_raw, received_position"
)


@pytest.fixture
def dsn():
    base = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not base:
        pytest.skip("local PostgreSQL integration DSN not provided")
    schema = f"n1_{uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    scoped = make_conninfo(base, options=f"-csearch_path={schema}")
    worker_main.apply_migrations(scoped, MIGRATIONS)
    try:
        yield app_dsn(scoped)
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture(autouse=True)
def published(monkeypatch):
    """The fixture forecast state and journal stand in for B2's ``forecast_db``."""

    def forecast_journal(settings, *, as_of, **filters):
        return forecast_fixture.forecast_journal(**filters)

    monkeypatch.setattr(forecast_db, "forecast_state", lambda settings: STATE)
    monkeypatch.setattr(forecast_db, "forecast_journal", forecast_journal)


def settings(dsn: str, mode: str) -> Settings:
    return Settings(
        mode=mode,
        db_dsn=dsn,
        replay_namespace=NAMESPACE,
        replay_snapshot_id=SCOPE,
        received_namespace=NAMESPACE,
        received_stream_id=SCOPE,
        _env_file=None,
    )


def create_scope(dsn: str, mode: str) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """INSERT INTO dispatch_replay_snapshots
               (namespace_id, snapshot_id, manifest_sha256, window_start, window_end,
                row_count, alarm_count, scope_kind)
               VALUES (%s, %s, NULL, %s, %s, 0, 0, %s)""",
            (NAMESPACE, SCOPE, DATA_AS_OF - timedelta(days=30), DATA_AS_OF, mode),
        )


def row(
    number: int,
    event_at: datetime,
    *,
    sensor_type: str = "Датчик дыма",
    value_raw: str = "Обнаружен дым",
    alarm: bool = False,
    object_id: str | None = "synthetic-object-11",
    channel_id: str | None = None,
    available_at: datetime | None = None,
) -> dict:
    return {
        "row_uid": f"synthetic-n1-{number:05d}",
        "channel_id": channel_id or f"synthetic-channel-{number:05d}",
        "object_id": object_id,
        "sensor_type": sensor_type,
        "value_raw": value_raw,
        "alarm": alarm,
        "event_at": event_at,
        "available_at": available_at or event_at + timedelta(seconds=5),
    }


def insert(dsn: str, mode: str, rows: list[dict]) -> None:
    observed = mode == "received"
    with psycopg.connect(dsn) as connection:
        start = connection.execute(
            "SELECT count(*) FROM dispatch_observations WHERE namespace_id = %s",
            (NAMESPACE,),
        ).fetchone()[0]
        connection.cursor().executemany(
            f"INSERT INTO dispatch_observations ({_COLUMNS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, 'synthetic', %s, %s, %s, %s, %s, 'synthetic.csv', %s, "
            "%s, %s, %s)",
            [
                (
                    NAMESPACE,
                    SCOPE,
                    item["row_uid"],
                    item["channel_id"],
                    item["object_id"],
                    item["sensor_type"],
                    item["value_raw"],
                    item["alarm"],
                    item["event_at"],
                    item["available_at"],
                    "observed" if observed else "simulated",
                    "0" * 64,
                    start + index,
                    item["event_at"].isoformat(),
                    start + index if observed else None,
                )
                for index, item in enumerate(rows, start=1)
            ],
        )


def load_object_names(dsn: str, names: dict[str, str]) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """INSERT INTO import_files (import_id, format, file_name, sha256, size_bytes,
                 stored_name, uploaded_by, status, finished_at)
               VALUES ('synthetic-objects', 'reference_objects_csv', 'objects.csv', %s, 1,
                 'objects.csv', 'synthetic', 'published', clock_timestamp())""",
            ("a" * 64,),
        )
        connection.execute(
            """INSERT INTO ref_versions (version_id, kind, sha256, import_id, row_count)
               VALUES ('synthetic-objects-v1', 'objects', %s, 'synthetic-objects', %s)""",
            ("a" * 64, len(names)),
        )
        for line, (object_id, name) in enumerate(names.items(), start=1):
            connection.execute(
                """INSERT INTO ref_objects (version_id, line_no, object_id, level_raw,
                     parent_raw, object_kind, name)
                   VALUES ('synthetic-objects-v1', %s, %s, '1', '', 'synthetic', %s)""",
                (line, object_id, name),
            )


def summary(dsn: str, mode: str, since: datetime):
    return notifications_db.notification_summary(settings(dsn, mode), since=since, now=NOW)


def test_exact_counts_newest_first_and_policy_texts(dsn):
    create_scope(dsn, "received")
    rows = [
        row(index, DATA_AS_OF - timedelta(minutes=5 * index), object_id=f"obj-{index % 12}")
        for index in range(1, 61)
    ]
    rows += [
        row(
            101,
            DATA_AS_OF - timedelta(minutes=3),
            sensor_type="Газовый датчик",
            value_raw="Обнаружен газ",
            alarm=False,
        ),
        row(
            102,
            DATA_AS_OF - timedelta(minutes=4),
            sensor_type="КД Дверь",
            value_raw="Не замкнут",
            alarm=True,
        ),
        row(103, DATA_AS_OF - timedelta(minutes=6), value_raw="Неисправен", alarm=True),
        row(
            104,
            DATA_AS_OF - timedelta(minutes=7),
            sensor_type="Состояние фазы",
            value_raw="Обесточен",
            alarm=True,
        ),
        row(
            105,
            DATA_AS_OF - timedelta(minutes=8),
            sensor_type="Датчик температуры",
            value_raw="Температура выше 40ºC",
            alarm=True,
        ),
        # (а) power-loss signature: «Обесточен» of object 11 two minutes later.
        row(
            106,
            DATA_AS_OF - timedelta(minutes=9),
            sensor_type="Состояние насоса",
            value_raw="Затоплен",
            alarm=True,
        ),
        row(
            107,
            DATA_AS_OF - timedelta(minutes=9),
            sensor_type="Состояние насоса",
            value_raw="Затоплен",
            alarm=True,
            object_id="synthetic-object-77",
        ),
    ]
    insert(dsn, "received", rows)
    result = summary(dsn, "received", DATA_AS_OF - timedelta(days=1))
    # 60 single smoke rows (night, one per object and hour) + gas at alarm=false + the pump
    # of object 77; the pump of object 11 is the power-loss signature (rule (а)).
    assert result.critical_alarms == 62
    cards = [entry.card for entry in forecast_fixture.journal_entries()]
    expected = new_cards(cards, since=DATA_AS_OF - timedelta(days=1), as_of=result.as_of)
    assert result.new_cards == len(expected) > 0  # cards counted apart from alarms
    assert len(result.items) == 50 and result.truncated
    # C0.5: by priority (a single gas record first), then newest first.
    assert result.items[0].ref_id == "synthetic-n1-00101" and result.items[0].priority == 0
    order = [(item.priority, -item.at.timestamp()) for item in result.items]
    assert order == sorted(order)
    assert result.policy_caption
    listed = {item.ref_id for item in result.items}
    assert "synthetic-n1-00101" in listed and "synthetic-n1-00107" in listed
    assert not listed & {
        "synthetic-n1-00102",
        "synthetic-n1-00103",
        "synthetic-n1-00104",
        "synthetic-n1-00106",
    }
    assert result.policy_version == "critical-alarms-v1"
    assert result.as_of == max(STATE.data_as_of, STATE.published_at)
    assert result.policy_confirmed is False and result.maintenance_check == "not_applied"
    gas = next(item for item in result.items if item.ref_id == "synthetic-n1-00101")
    assert gas.rule_id == "gas_detected" and gas.title == "Газовый датчик: «Обнаружен газ»"


def test_nothing_later_than_the_stand_clock(dsn):
    create_scope(dsn, "replay")
    insert(
        dsn,
        "replay",
        [
            row(1, DATA_AS_OF - timedelta(minutes=30)),
            row(2, DATA_AS_OF + timedelta(minutes=1)),  # future event
            # Past event, but in replay it becomes available after the stand clock.
            row(3, DATA_AS_OF - timedelta(minutes=1), available_at=DATA_AS_OF + timedelta(1)),
        ],
    )
    result = summary(dsn, "replay", DATA_AS_OF - timedelta(hours=2))
    alarms = [item.ref_id for item in result.items if item.kind == "critical_alarm"]
    assert alarms == ["synthetic-n1-00001"] and result.critical_alarms == 1
    assert all(item.at <= result.as_of for item in result.items)


def test_received_uploads_count_by_event_time(dsn):
    """A history upload arrives now (``available_at``) with past events: it is listed."""
    create_scope(dsn, "received")
    insert(
        dsn,
        "received",
        [
            row(1, DATA_AS_OF - timedelta(minutes=10), available_at=NOW - timedelta(minutes=1)),
            row(2, DATA_AS_OF + timedelta(minutes=10), available_at=NOW - timedelta(minutes=1)),
            row(3, DATA_AS_OF - timedelta(minutes=20), available_at=NOW + timedelta(minutes=1)),
        ],
    )
    result = summary(dsn, "received", DATA_AS_OF - timedelta(hours=1))
    alarms = [item.ref_id for item in result.items if item.kind == "critical_alarm"]
    assert alarms == ["synthetic-n1-00001"]


def test_detector_series_across_since_folds_into_one_row(dsn):
    """(б) Wednesday 11:56–12:07 MSK: six detector channels of one object, one row."""
    create_scope(dsn, "replay")
    since = DATA_AS_OF - timedelta(hours=12)  # 11:59:30 MSK
    series = [
        row(
            index,
            since + timedelta(minutes=index * 2 - 3),
            object_id="synthetic-object-12",
            sensor_type="Тепловой датчик" if index % 2 else "Датчик дыма",
            value_raw="Не замкнут" if index % 2 else "Обнаружен дым",
        )
        for index in range(1, 7)
    ]  # the first record lies before since, the rest after it, all within 10 minutes
    lone = row(50, since + timedelta(minutes=40), object_id="synthetic-object-13")
    night = [
        row(60 + index, DATA_AS_OF - timedelta(minutes=10 - index), object_id="synthetic-object-14")
        for index in range(5)
    ]  # 23:50–23:54 MSK: not marked, five single rows
    insert(dsn, "replay", [*series, lone, *night])
    load_object_names(dsn, {"synthetic-object-12": "Синтетическая станция 12"})
    result = summary(dsn, "replay", since)
    alarms = {item.ref_id: item for item in result.items if item.kind == "critical_alarm"}
    assert result.critical_alarms == 7 and len(alarms) == 7
    folded = alarms[series[-1]["row_uid"]]
    assert folded.title == "Похоже на ППР или ТО: 6 извещателей — сверить с графиком"
    assert folded.rule_id == "fire_test_series" and "6 записей" in folded.rule_text
    assert folded.object_name == "Синтетическая станция 12"
    assert alarms[lone["row_uid"]].rule_id == "fire_smoke"
    assert all(alarms[item["row_uid"]].rule_id == "fire_smoke" for item in night)


def test_gas_pump_and_flood_rules_through_storage(dsn):
    """(а), (в), (г) on the PostgreSQL path; nothing later than the stand clock."""
    create_scope(dsn, "replay")
    since = DATA_AS_OF - timedelta(hours=2)
    gas_series = [
        row(
            index,
            DATA_AS_OF - timedelta(minutes=60 - index),
            sensor_type="Газовый датчик",
            value_raw="Обнаружен газ",
            object_id="synthetic-object-20",
        )
        for index in range(1, 6)
    ]
    gas_single = row(
        10,
        DATA_AS_OF - timedelta(minutes=100),
        sensor_type="Газовый датчик",
        value_raw="Обнаружен газ",
        alarm=True,
        object_id="synthetic-object-21",
    )
    pump = row(
        20,
        DATA_AS_OF - timedelta(minutes=30),
        sensor_type="Состояние насоса",
        value_raw="Затоплен",
        object_id="synthetic-object-22",
        channel_id="synthetic-pump-22",
    )
    power = row(
        21,
        DATA_AS_OF - timedelta(minutes=30),
        sensor_type="Состояние насоса",
        value_raw="Неисправен",
        object_id="synthetic-object-22",
        channel_id="synthetic-pump-22",
    )
    # A pump whose «Обесточен» comes after the stand clock: not seen, so still critical.
    late_pump = row(
        22,
        DATA_AS_OF - timedelta(minutes=5),
        sensor_type="Состояние насоса",
        value_raw="Затоплен",
        object_id="synthetic-object-23",
    )
    late_power = row(
        23,
        DATA_AS_OF + timedelta(minutes=1),
        sensor_type="Состояние фазы",
        value_raw="Обесточен",
        object_id="synthetic-object-23",
    )
    flood = [
        row(
            30 + index,
            DATA_AS_OF - timedelta(minutes=50 - index * 5),
            sensor_type="Датчик затопления",
            value_raw="Не замкнут",
            alarm=True,
            object_id="synthetic-object-24",
            channel_id="synthetic-flood-24",
        )
        for index in range(4)
    ]  # 23:09–23:24 MSK: one channel, one clock hour
    insert(
        dsn,
        "replay",
        [*gas_series, gas_single, pump, power, late_pump, late_power, *flood],
    )
    result = summary(dsn, "replay", since)
    alarms = {item.ref_id: item for item in result.items if item.kind == "critical_alarm"}
    assert result.critical_alarms == 4 == len(alarms)
    gas_title = alarms[gas_series[-1]["row_uid"]].title
    assert gas_title == "Похоже на ППР или ТО: 5 газоанализаторов — сверить с графиком"
    assert alarms[gas_single["row_uid"]].rule_id == "gas_detected"
    assert pump["row_uid"] not in alarms  # (а) «Неисправен» of the same object
    assert alarms[late_pump["row_uid"]].rule_id == "flood_pump"
    chatter = alarms[flood[-1]["row_uid"]]
    assert chatter.title == "Датчик затопления: «Не замкнут» ×4"
    assert "4 записи одного канала за час, 23:09–23:24 МСК" in chatter.rule_text
    assert (chatter.row_kind, chatter.collapsed_count, len(chatter.members)) == ("chatter", 4, 4)


def test_members_name_their_channels(dsn):
    """QA 29.09, D3: a member carries the channel name of the channel layout; a channel
    absent from it keeps only its ID."""
    create_scope(dsn, "replay")
    named = row(1, DATA_AS_OF - timedelta(minutes=10), channel_id="synthetic-channel-named")
    unnamed = row(2, DATA_AS_OF - timedelta(minutes=5))
    insert(dsn, "replay", [named, unnamed])
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """INSERT INTO forecast_channel_layout
                 (channel_id, object_id, sensor_type, name, picket_form, layout_version)
               VALUES (%s, 'synthetic-object-11', 'Датчик дыма', 'Синт. дым ПК7', 'unknown',
                 'synthetic')""",
            (named["channel_id"],),
        )
    result = summary(dsn, "replay", DATA_AS_OF - timedelta(hours=1))
    members = {member.row_uid: member for item in result.items for member in item.members}
    assert members[named["row_uid"]].channel_name == "Синт. дым ПК7"
    assert members[unnamed["row_uid"]].channel_name is None
    assert members[unnamed["row_uid"]].channel_id == unnamed["channel_id"]


def test_unloaded_scope_and_bad_window(dsn):
    with pytest.raises(LookupError):
        summary(dsn, "replay", DATA_AS_OF - timedelta(hours=1))
    create_scope(dsn, "replay")
    with pytest.raises(NotificationWindowError):
        summary(dsn, "replay", DATA_AS_OF - timedelta(days=8))
    with pytest.raises(NotificationWindowError):
        summary(dsn, "replay", DATA_AS_OF + timedelta(days=1))


def test_latency_on_100k_rows(dsn):
    """Criterion N1-4: ≤ 200 ms on 100 000 synthetic rows without a new index."""
    create_scope(dsn, "received")
    with psycopg.connect(dsn) as connection:
        connection.execute(
            f"""INSERT INTO dispatch_observations ({_COLUMNS})
                SELECT %(ns)s, %(scope)s, 'perf-' || g, 'perf-channel-' || (g %% 3000),
                       'perf-object-' || (g %% 150),
                       CASE WHEN g %% 40 = 0 THEN 'Датчик дыма'
                            WHEN g %% 40 = 1 THEN 'Газовый датчик'
                            WHEN g %% 7 = 0 THEN 'КД Дверь' ELSE 'Датчик дыма' END,
                       'synthetic',
                       CASE WHEN g %% 40 = 0 THEN 'Обнаружен дым'
                            WHEN g %% 40 = 1 THEN 'Обнаружен газ'
                            WHEN g %% 7 = 0 THEN 'Не замкнут' ELSE 'Норма' END,
                       g %% 7 = 0,
                       %(start)s + g * interval '6 seconds',
                       %(start)s + g * interval '6 seconds' + interval '2 seconds',
                       'observed', 'synthetic.csv', repeat('0', 64), g, 'synthetic', g
                FROM generate_series(1, 100000) AS g""",
            {"ns": NAMESPACE, "scope": SCOPE, "start": DATA_AS_OF - timedelta(days=7)},
        )
        connection.execute("ANALYZE dispatch_observations")
    since = DATA_AS_OF - timedelta(days=7)
    timings = []
    for _ in range(6):
        started = time.perf_counter()
        read = notifications_pg.read_critical_alarms(
            dsn,
            namespace_id=NAMESPACE,
            snapshot_id=SCOPE,
            mode="received",
            since=since,
            data_as_of=DATA_AS_OF,
            available_as_of=NOW,
        )
        timings.append(time.perf_counter() - started)
    assert read.total == 5000 and len(read.records) == 50
    assert statistics.median(timings[1:]) <= 0.2, timings


def test_route_reads_the_scope(dsn):
    create_scope(dsn, "replay")
    insert(dsn, "replay", [row(1, DATA_AS_OF - timedelta(minutes=5))])
    client = TestClient(create_app(settings(dsn, "replay")))
    response = client.get(
        "/api/v1/notifications", params={"since": (DATA_AS_OF - timedelta(hours=1)).isoformat()}
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = NotificationSummary.model_validate(response.json())
    assert (body.mode, body.critical_alarms) == ("replay", 1)
    assert body.items[-1].ref_id == "synthetic-n1-00001"
