"""G1 on PostgreSQL: XLSX and ТЗ Appendix 1 imports, audit of statuses and requests.

Runs only with ``INFRA_TEST_RECEIVED_DSN`` (``make check-db``); each test works in its
own schema. All rows are synthetic; the Appendix 1 rows are the ТЗ's printed example.
"""

import io
import os
from datetime import time as clock_time
from uuid import uuid4

import openpyxl
import psycopg
import pytest
from app_role import app_dsn
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from test_ingestion_db import (
    MIGRATIONS,
    NAMESPACE,
    STREAM,
    channels,
    drain,
    journal,
    report,
    scalar,
    upload,
)

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_backend.ingestion.journal_csv import JOURNAL_HEADER, TZ_JOURNAL_HEADER
from infra_pulse_backend.ingestion.reference_csv import CHANNELS_HEADER
from infra_pulse_backend.worker import __main__ as worker_main

TZ_EXAMPLE = [
    ("3008235019", "56682", "12", "25,00", "19.10.2026 12:15"),
    ("3008235020", "183582", "12", "25,40", "19.10.2026 12:16"),
    ("3008235021", "215811", "12", "33,50", "19.10.2026 12:17"),
]


@pytest.fixture
def dsn():
    base = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not base:
        pytest.skip("local PostgreSQL integration DSN not provided")
    schema = f"g1_{uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    scoped = make_conninfo(base, options=f"-csearch_path={schema}")
    worker_main.apply_migrations(scoped, MIGRATIONS)
    try:
        yield app_dsn(scoped)
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def settings(dsn, tmp_path) -> Settings:
    return Settings(
        mode="received",
        db_dsn=dsn,
        upload_dir=tmp_path / "uploads",
        received_namespace=NAMESPACE,
        received_stream_id=STREAM,
        _env_file=None,
    )


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings))


def tz_csv(rows) -> bytes:
    lines = [";".join(TZ_JOURNAL_HEADER)] + [";".join(row) for row in rows]
    return ("\n".join(lines) + "\n").encode()


def workbook(rows: list[list]) -> bytes:
    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def typed_reference(types: dict[str, str]) -> bytes:
    rows = [list(CHANNELS_HEADER)]
    for index, (channel, sensor_type) in enumerate(types.items(), start=1):
        rows.append(
            [int(channel), "Инженерные системы", sensor_type, f"МК-{index}", f"К{index}", 56]
        )
    return workbook(rows)


def observations(dsn: str) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        return connection.execute(
            """SELECT source_event_id, channel_id, sensor_type, alarm, value_raw,
                      source_channel_type_id, event_local_raw, received_position
               FROM dispatch_observations WHERE namespace_id = %s AND snapshot_id = %s
               ORDER BY received_position""",
            (NAMESPACE, STREAM),
        ).fetchall()


def audit_rows(dsn: str, action: str) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        return connection.execute(
            """SELECT action, outcome, actor_id, actor_role, target_kind, target_id,
                      request_id, client_address, details
               FROM audit_events WHERE action = %s ORDER BY occurred_at, audit_id""",
            (action,),
        ).fetchall()


def test_xlsx_reference_and_journal_load_like_csv(client, settings, dsn):
    reference = upload(
        client,
        "channels.xlsx",
        typed_reference({"700001": "Состояние фазы"}),
        "reference_channels_csv",
    )
    drain(settings)
    loaded = report(client, reference.id)
    assert loaded.status == "published" and loaded.rows_accepted == 1
    assert (loaded.source_layout, loaded.source_container) == ("reference", "xlsx")

    rows = [
        ("9000001", "700001", "2026-09-20", "03:09:27", True, "Неисправен"),
        ("9000002", "700001", "2026-09-20", "03:19:27", False, "01.01.1970 03:00:01"),
    ]
    sheet = [list(JOURNAL_HEADER)]
    for event_id, channel, day, moment, alarm, value in rows:
        sheet.append(
            [int(event_id), int(channel), day, clock_time.fromisoformat(moment), alarm, value]
        )
    item = upload(client, "journal.xlsx", workbook(sheet))
    drain(settings)
    first = report(client, item.id)
    assert first.status == "imported", first
    assert (first.rows_accepted, first.rows_quarantined, first.source_container) == (2, 0, "xlsx")
    assert first.notes == ["XLSX: прочитан первый лист книги"]
    stored = observations(dsn)
    assert [row["source_event_id"] for row in stored] == ["9000001", "9000002"]
    assert stored[1]["value_raw"] == "01.01.1970 03:00:01"
    # The same records as CSV are duplicates: the key does not depend on the container.
    again = upload(client, "journal.csv", journal(rows))
    drain(settings)
    second = report(client, again.id)
    assert (second.rows_accepted, second.rows_duplicate) == (0, 2)
    assert second.source_container == "csv" and second.notes == []


def test_tz_appendix1_journal_keeps_unknown_alarm_and_type(client, settings, dsn):
    upload(
        client,
        "channels.csv",
        typed_reference(
            {
                "56682": "Датчик температуры",
                "183582": "Датчик температуры",
                "215811": "Состояние фазы",
            }
        ),
        "reference_channels_csv",
    )
    drain(settings)
    item = upload(client, "tz.csv", tz_csv(TZ_EXAMPLE))
    drain(settings)
    loaded = report(client, item.id)
    assert loaded.status == "imported"
    assert (loaded.rows_total, loaded.rows_accepted, loaded.rows_quarantined) == (3, 2, 1)
    assert loaded.quarantine_reasons == {"channel_type_conflict": 1}
    assert loaded.quarantine_sample[0].line_no == 4
    assert (loaded.source_layout, loaded.alarm_not_provided) == ("tz_appendix1", 2)
    assert loaded.notes == [
        "Формат Приложения 1 ТЗ: признак тревожности источника не передан (2 записей); "
        "время с точностью до минуты"
    ]
    stored = observations(dsn)
    assert [row["alarm"] for row in stored] == [None, None]
    assert [row["source_channel_type_id"] for row in stored] == ["12", "12"]
    # The sensor type comes from the channel reference, not from the type ID.
    assert {row["sensor_type"] for row in stored} == {"Датчик температуры"}
    assert stored[0]["event_local_raw"] == "19.10.2026 12:15"
    assert (
        scalar(
            dsn,
            "SELECT alarm_count FROM dispatch_replay_snapshots WHERE snapshot_id = %s",
            (STREAM,),
        )
        == 0
    )
    # The API shows «не передан» as null, never false.
    listed = client.get(
        "/api/v1/attention",
        params={"view": "all", "as_of": "2026-10-20T00:00:00+03:00"},
    )
    assert listed.status_code == 200, listed.text
    assert {entry["message"]["alarm"] for entry in listed.json()["items"]} == {None}


def test_filled_alarm_replaces_null_and_null_never_overwrites(client, settings, dsn):
    upload(
        client,
        "channels.xlsx",
        typed_reference({"56682": "Датчик температуры"}),
        "reference_channels_csv",
    )
    drain(settings)
    upload(client, "tz.csv", tz_csv(TZ_EXAMPLE[:1]))
    drain(settings)
    assert [row["alarm"] for row in observations(dsn)] == [None]
    # Organizers' export of the same record (same key: channel, time, ID, value).
    filled = upload(
        client,
        "export.csv",
        journal([("3008235019", "56682", "2026-10-19", "12:15:00", True, "25,00")]),
    )
    drain(settings)
    loaded = report(client, filled.id)
    assert (loaded.rows_accepted, loaded.rows_duplicate) == (0, 1)
    assert loaded.notes == ["Признак тревожности дополнен у ранее принятых записей: 1"]
    assert [row["alarm"] for row in observations(dsn)] == [True]
    assert (
        scalar(
            dsn,
            "SELECT alarm_count FROM dispatch_replay_snapshots WHERE snapshot_id = %s",
            (STREAM,),
        )
        == 1
    )
    # A later Appendix 1 file with the same record never turns true back into NULL.
    upload(client, "tz-again.csv", tz_csv([TZ_EXAMPLE[0], TZ_EXAMPLE[1]]))
    drain(settings)
    assert [row["alarm"] for row in observations(dsn)] == [True, None]


def test_order_inside_a_minute_follows_the_record_id(client, settings, dsn):
    upload(
        client,
        "channels.xlsx",
        typed_reference({"56682": "Датчик температуры"}),
        "reference_channels_csv",
    )
    drain(settings)
    rows = [
        ("30", "56682", "12", "3", "19.10.2026 12:16"),
        ("300", "56682", "12", "5", "19.10.2026 12:15"),
        ("10", "56682", "12", "1", "19.10.2026 12:16"),
        ("20", "56682", "12", "2", "19.10.2026 12:16"),
    ]
    upload(client, "tz.csv", tz_csv(rows))
    drain(settings)
    assert [row["source_event_id"] for row in observations(dsn)] == ["300", "10", "20", "30"]


def test_import_status_changes_are_audited(client, settings, dsn):
    upload(client, "channels.csv", channels({"700001": "19"}), "reference_channels_csv")
    drain(settings)
    item = upload(
        client, "day.csv", journal([("1", "700001", "2026-09-20", "03:09:27", False, "Норма")])
    )
    drain(settings)
    rows = [row for row in audit_rows(dsn, "import.status_changed") if row["target_id"] == item.id]
    path = [(row["details"]["from"], row["details"]["to"]) for row in rows]
    # recompute_stub publishes nothing, so the job ends in «imported».
    assert path == [
        ("queued", "parsing"),
        ("parsing", "imported"),
        ("imported", "recomputing"),
        ("recomputing", "imported"),
    ]
    assert all(row["actor_id"].startswith("worker:") and row["actor_role"] is None for row in rows)
    assert rows[1]["details"]["rows_accepted"] == 1
    assert rows[1]["details"]["source_layout"] == "organizers_export"
    uploaded = [row for row in audit_rows(dsn, "import.uploaded") if row["target_id"] == item.id]
    assert uploaded and uploaded[0]["actor_id"] == "local-operator"


def test_requests_are_audited_polls_summed_rows_append_only(settings, dsn):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/imports").status_code == 200
        assert client.get("/api/v1/imports").status_code == 200  # folded repeat
        for _ in range(3):
            client.get("/api/v1/forecast-state")
        client.get("/health/live")
        # Before the window ends a poll leaves no row.
        assert audit_rows(dsn, "request.summary") == []
    viewed = audit_rows(dsn, "import.list_viewed")
    assert len(viewed) == 1
    row = viewed[0]
    assert (row["outcome"], row["actor_id"], row["actor_role"]) == (
        "success",
        "local-operator",
        "admin",
    )
    assert row["details"]["route"] == "/api/v1/imports" and row["details"]["status"] == 200
    assert row["client_address"] is None
    # Shutdown flushes the window: one summary per actor and route.
    summaries = {
        (item["target_id"], item["details"]["kind"]): item["details"]
        for item in audit_rows(dsn, "request.summary")
    }
    assert summaries[("/api/v1/forecast-state", "poll")]["count"] == 3
    assert summaries[("/api/v1/imports", "repeat")]["count"] == 1
    assert not any(target.startswith("/health") for target, _ in summaries)
    with psycopg.connect(dsn) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("DELETE FROM audit_events")
