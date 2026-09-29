"""PostgreSQL path of CSV ingestion: upload -> queue -> worker -> observations.

Runs only with ``INFRA_TEST_RECEIVED_DSN`` pointing to a disposable database
(``make check-db``). Every test works in its own schema, which is dropped after it.
All CSV rows are synthetic. The upload writes ``import.uploaded`` to ``audit_events``
(B3, SEC-03) in the same transaction as ``import_files`` and ``jobs``.
"""

import hashlib
import io
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from app_role import app_dsn, owner_dsn
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.api.auth_deps import AuthRuntime
from infra_pulse_backend.auth.ldap import DirectoryUser, InvalidCredentials
from infra_pulse_backend.auth.sessions import CSRF_COOKIE, CSRF_HEADER, PgAuthStore
from infra_pulse_backend.auth.throttle import LoginThrottle
from infra_pulse_backend.config import Settings
from infra_pulse_backend.ingestion import imports_pg
from infra_pulse_backend.worker import __main__ as worker_main
from infra_pulse_backend.worker import runner
from infra_pulse_backend.worker.queue import claim_next
from infra_pulse_backend.worker.runner import WorkerConfig, recompute_stub, run_once
from infra_pulse_core.contracts.attention import AttentionList
from infra_pulse_core.contracts.imports import ImportFile, ImportList
from infra_pulse_core.features.phase_feeder_episodes import MSK

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
NAMESPACE = "b1-test"
STREAM = "b1-stream"
JOURNAL_HEADER = '"ид_события","ид_канала_данных","дата","время","тревожное","значение_датчика"'
CHANNELS_HEADER = (
    '"ид_канала_данных","тип_инж_системы","тип_датчика","тег_инженерной_системы",'
    '"название_датчика","ид_объект"'
)


@pytest.fixture
def dsn():
    base = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not base:
        pytest.skip("local PostgreSQL integration DSN not provided")
    schema = f"b1_{uuid4().hex[:12]}"
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


def worker(settings: Settings, name: str = "worker-a", **overrides) -> WorkerConfig:
    return WorkerConfig(
        dsn=settings.db_dsn.get_secret_value(),
        upload_dir=settings.upload_dir,
        namespace_id=NAMESPACE,
        stream_id=STREAM,
        worker_id=name,
        lease_seconds=overrides.get("lease_seconds", 60),
        retry_seconds=overrides.get("retry_seconds", 0),
    )


def drain(settings: Settings, recompute=recompute_stub, name: str = "worker-a") -> int:
    processed = 0
    while run_once(worker(settings, name), recompute):
        processed += 1
    return processed


def journal(rows: list[tuple], *, archive: bool = False) -> bytes:
    """Organizers' shape (quotes, true/false, BOM) or archive shape (plain, t/f)."""
    if archive:
        lines = [JOURNAL_HEADER.replace('"', "")]
        for event_id, channel, date, clock, alarm, value in rows:
            lines.append(f"{event_id},{channel},{date},{clock},{'t' if alarm else 'f'},{value}")
        return ("\n".join(lines) + "\n").encode()
    lines = [JOURNAL_HEADER]
    for event_id, channel, date, clock, alarm, value in rows:
        flag = "true" if alarm else "false"
        lines.append(f'{event_id},{channel},"{date}","{clock}",{flag},"{value}"')
    return ("﻿" + "\n".join(lines) + "\n").encode()


def channels(mapping: dict[str, str | None]) -> bytes:
    lines = [CHANNELS_HEADER]
    for index, (channel, object_id) in enumerate(mapping.items(), start=1):
        lines.append(
            f'{channel},Электроснабжение,Состояние фазы,"90-1.1.{index}.",'
            f"Фаза ПК{index},{object_id or ''}"
        )
    return ("\n".join(lines) + "\n").encode()


def upload(client: TestClient, name: str, data: bytes, fmt: str = "journal_csv") -> ImportFile:
    response = client.post(
        "/api/v1/imports",
        files={"file": (name, io.BytesIO(data), "text/csv")},
        data={"format": fmt},
    )
    assert response.status_code == 202, response.text
    return ImportFile.model_validate(response.json())


def report(client: TestClient, import_id: str) -> ImportFile:
    response = client.get(f"/api/v1/imports/{import_id}")
    assert response.status_code == 200, response.text
    return ImportFile.model_validate(response.json())


def scalar(dsn: str, query: str, params: tuple = ()):
    with psycopg.connect(dsn) as connection:
        return connection.execute(query, params).fetchone()[0]


def load_reference(client, settings, mapping) -> ImportFile:
    item = upload(client, "channels.csv", channels(mapping), "reference_channels_csv")
    drain(settings)
    return report(client, item.id)


def test_upload_is_queued_then_worker_loads_received_scope(client, settings, dsn):
    ref = load_reference(client, settings, {"700001": "5001", "700002": "5001", "700003": "5002"})
    assert (ref.status, ref.rows_accepted, ref.finished_at is not None) == ("published", 3, True)
    assert ref.reference_version and ref.reference_version.startswith("channels-")

    rows = [
        ("9000001", "700001", "2026-09-20", "03:09:27", False, "28"),
        ("9000002", "700002", "2026-09-20", "10:00:00", True, "Неисправен"),
        ("9000002", "700003", "2026-09-20", "11:00:00", False, "0.01"),
        ("9000004", "799999", "2026-09-20", "12:00:00", False, "Норма"),
        ("9000005", "700001", "1970-01-01", "03:00:00", False, "01.01.1970 03:00:00"),
        ("9000006", "700001", "2026-09-20", "25:00:00", False, "bad time"),
        ("9000007", "700001", "2026-09-20", "13:00:00", False, "Обрыв; ТО"),
    ]
    queued = upload(client, "journal-2026-09-20.csv", journal(rows))
    assert (queued.status, queued.uploaded_by, queued.simulated) == (
        "queued",
        "local-operator",
        False,
    )
    assert [timing.stage for timing in queued.timings] == ["store"]
    # HTTP only stored the file and queued the job.
    assert scalar(dsn, "SELECT count(*) FROM dispatch_observations") == 0
    assert scalar(dsn, "SELECT state FROM jobs WHERE import_id = %s", (queued.id,)) == "queued"
    assert (settings.upload_dir / f"{queued.id}.csv").read_bytes() == journal(rows)

    assert drain(settings) == 1
    done = report(client, queued.id)
    assert done.status == "imported"  # recompute stub; the B2 path: test_upload_publish_decide_db
    assert done.finished_at is None and done.forecast_generation is None
    assert (done.rows_total, done.rows_accepted, done.rows_duplicate, done.rows_quarantined) == (
        7,
        6,
        0,
        1,
    )
    assert done.unknown_channels == 1
    assert done.quarantine_reasons == {"bad_time": 1}
    assert [(item.line_no, item.reason) for item in done.quarantine_sample] == [(7, "bad_time")]
    assert done.event_from == datetime(2026, 9, 20, 0, 9, 27, tzinfo=UTC)
    assert done.event_to == datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    assert done.reference_version == ref.reference_version
    assert {timing.stage for timing in done.timings} == {"store", "parse", "load"}

    page = AttentionList.model_validate(
        client.get("/api/v1/attention", params={"view": "all", "limit": 100}).json()
    )
    assert page.total == 6
    messages = {entry.message.value_raw: entry.message for entry in page.items}
    assert set(messages) == {
        "28",
        "Неисправен",
        "0.01",
        "Норма",
        "01.01.1970 03:00:00",
        "Обрыв; ТО",
    }
    assert {message.availability_basis for message in messages.values()} == {"observed"}
    assert {message.source_file for message in messages.values()} == {"journal-2026-09-20.csv"}
    assert messages["28"].object_id == "5001" and messages["28"].value_numeric == 28.0
    assert messages["Неисправен"].alarm is True and messages["Неисправен"].value_numeric is None
    assert messages["Норма"].object_id is None
    assert "reference_unmatched" in messages["Норма"].quality_flags
    assert "is_epoch_placeholder" in messages["01.01.1970 03:00:00"].quality_flags
    assert messages["0.01"].sensor_type == "Состояние фазы"
    assert messages["0.01"].event_at == datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
    alarms = client.get("/api/v1/attention", params={"view": "attention"}).json()
    assert alarms["source_alarm_count"] == 1
    with psycopg.connect(dsn) as connection:
        raw = connection.execute(
            """SELECT event_local_raw, reference_version, received_position
               FROM dispatch_observations ORDER BY received_position"""
        ).fetchall()
        scope = connection.execute(
            "SELECT row_count, alarm_count, scope_kind FROM dispatch_replay_snapshots"
        ).fetchone()
    assert [row[0] for row in raw][:2] == ["2026-09-20 03:09:27", "2026-09-20 10:00:00"]
    assert {row[1] for row in raw} == {ref.reference_version}
    assert [row[2] for row in raw] == [1, 2, 3, 4, 5, 6]
    assert scope == (6, 1, "received")

    listing = ImportList.model_validate(client.get("/api/v1/imports", params={"limit": 1}).json())
    assert (listing.total, [item.id for item in listing.items]) == (2, [queued.id])
    second = ImportList.model_validate(
        client.get("/api/v1/imports", params={"limit": 1, "cursor": listing.next_cursor}).json()
    )
    assert ([item.id for item in second.items], second.next_cursor) == ([ref.id], None)
    assert client.get("/api/v1/imports", params={"cursor": "x1"}).status_code == 422
    assert client.get("/api/v1/imports/imp-missing").status_code == 404


class AdminDirectory:
    """Directory stand-in: one synthetic administrator (password is synthetic too)."""

    def authenticate(self, username: str, password: str) -> DirectoryUser:
        if (username, password) != ("admin-b1", "synthetic-password-1"):
            raise InvalidCredentials
        return DirectoryUser(username, "Тест admin-b1", frozenset({"admin"}))


def audit_rows(dsn: str, import_id: str) -> list[tuple]:
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            """SELECT action, outcome, actor_id, actor_role, target_kind, request_id, details
               FROM audit_events WHERE target_id = %s ORDER BY occurred_at""",
            (import_id,),
        ).fetchall()


def test_upload_audit_commits_with_the_import_or_not_at_all(client, settings, dsn, monkeypatch):
    data = journal([("1", "700001", "2026-09-20", "01:00:00", False, "a")])
    response = client.post(
        "/api/v1/imports",
        files={"file": ("audit.csv", io.BytesIO(data), "text/csv")},
        data={"format": "journal_csv"},
        headers={"X-Request-ID": "req-b1-upload-0001"},
    )
    assert response.status_code == 202, response.text
    queued = ImportFile.model_validate(response.json())
    [row] = audit_rows(dsn, queued.id)
    assert row[:6] == (
        "import.uploaded",
        "success",
        "local-operator",
        "admin",
        "import_file",
        "req-b1-upload-0001",
    )
    assert row[6] == {"sha256": queued.sha256, "format": "journal_csv", "size_bytes": len(data)}

    # An audit failure rolls back import_files and jobs; the stored copy is removed.
    def broken(connection, event):
        raise psycopg.errors.CheckViolation("synthetic audit failure")

    monkeypatch.setattr(imports_pg, "record_audit_event", broken)
    before = sorted(path.name for path in settings.upload_dir.iterdir())
    failed = client.post(
        "/api/v1/imports",
        files={"file": ("lost.csv", io.BytesIO(data + b"2,700001,2026-09-20,01:00:01,f,b\n"))},
        data={"format": "journal_csv"},
    )
    assert (failed.status_code, failed.json()["detail"]) == (503, "imports_storage_unavailable")
    assert scalar(dsn, "SELECT count(*) FROM import_files") == 1
    assert scalar(dsn, "SELECT count(*) FROM jobs") == 1
    assert scalar(dsn, "SELECT count(*) FROM audit_events") == 1
    assert sorted(path.name for path in settings.upload_dir.iterdir()) == before


def test_upload_author_comes_from_the_directory_session(settings, dsn):
    app = create_app(settings.model_copy(update={"auth_mode": "ldap"}))
    app.state.auth_runtime = AuthRuntime(
        store=PgAuthStore(dsn), directory=AdminDirectory(), throttle=LoginThrottle(5, 300)
    )
    client = TestClient(app, base_url="https://testserver")
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin-b1", "password": "synthetic-password-1"},
        headers={CSRF_HEADER: "login"},
    )
    assert login.status_code == 200, login.text
    data = channels({"700001": "5001"})
    response = client.post(
        "/api/v1/imports",
        files={"file": ("channels.csv", io.BytesIO(data), "text/csv")},
        data={"format": "reference_channels_csv"},
        headers={CSRF_HEADER: client.cookies[CSRF_COOKIE]},
    )
    assert response.status_code == 202, response.text
    queued = ImportFile.model_validate(response.json())
    assert queued.uploaded_by == "admin-b1"
    [row] = audit_rows(dsn, queued.id)
    assert (row[0], row[2], row[3]) == ("import.uploaded", "admin-b1", "admin")


def test_same_file_is_duplicate_and_overlap_keeps_each_record_once(client, settings, dsn):
    load_reference(client, settings, {"700001": "5001", "700002": "5002"})
    first_rows = [
        ("1", "700001", "2026-09-20", "00:00:01", False, "1.5"),
        ("1", "700002", "2026-09-20", "00:00:01", False, "1.5"),
        ("2", "700001", "2026-09-20", "00:00:02", True, "Неисправен"),
        ("3", "700001", "2026-09-20", "00:00:03", False, "Норма"),
    ]
    archive = journal(first_rows, archive=True)
    first = upload(client, "archive.csv", archive)
    drain(settings)
    assert report(client, first.id).rows_accepted == 4

    again = upload(client, "archive-copy.csv", archive)
    drain(settings)
    duplicate = report(client, again.id)
    assert (duplicate.status, duplicate.duplicate_of, duplicate.rows_accepted) == (
        "duplicate",
        first.id,
        0,
    )
    assert duplicate.finished_at is not None
    assert not (settings.upload_dir / f"{again.id}.csv").exists()
    assert scalar(dsn, "SELECT count(*) FROM dispatch_observations") == 4

    overlap_rows = [
        *first_rows[2:],  # already stored, other quoting
        ("3", "700001", "2026-09-20", "00:00:03", False, "Норма"),  # repeat inside the file
        ("3", "700001", "2026-09-20", "00:00:03", False, "Обрыв"),  # same ID, other value
        ("4", "700002", "2026-09-20", "00:00:04", False, "2.5"),
    ]
    overlap = upload(client, "organizers.csv", journal(overlap_rows))
    drain(settings)
    loaded = report(client, overlap.id)
    assert (loaded.rows_total, loaded.rows_accepted, loaded.rows_duplicate) == (5, 2, 3)
    with psycopg.connect(dsn) as connection:
        stored = connection.execute(
            """SELECT source_event_id, value_raw, source_file, received_position
               FROM dispatch_observations ORDER BY received_position"""
        ).fetchall()
        scope_rows = connection.execute(
            "SELECT row_count FROM dispatch_replay_snapshots"
        ).fetchone()
    assert [row[3] for row in stored] == [1, 2, 3, 4, 5, 6]
    assert scope_rows == (6,)
    assert [(row[0], row[1]) for row in stored if row[0] == "3"] == [("3", "Норма"), ("3", "Обрыв")]
    assert stored[-1][2] == "organizers.csv"


def seed_like(owner: str, records: list[tuple]) -> None:
    """Store history the way ``backend/scripts/seed_forecast_history.py`` does (as the owner).

    The seed keeps the row_uid of the curated snapshot, ``sha256("<source_sha256>:<ordinal>")``
    (``data-science/scripts/curate_journal.py``), not the ingestion ``csv-<day>-<hash>``.
    ``alarm=None`` stands for a record stored without the source flag.
    """
    source_sha = hashlib.sha256(b"synthetic curated history").hexdigest()
    with psycopg.connect(owner) as connection:
        connection.execute(
            """INSERT INTO dispatch_replay_snapshots
                 (namespace_id, snapshot_id, manifest_sha256, window_start, window_end,
                  row_count, alarm_count, scope_kind)
               VALUES (%s, %s, NULL, '2026-06-01', '2026-07-01', 0, 0, 'received')
               ON CONFLICT (namespace_id, snapshot_id) DO NOTHING""",
            (NAMESPACE, STREAM),
        )
        for ordinal, (event_id, channel, local, value, alarm) in enumerate(records, start=1):
            at = local.replace(tzinfo=MSK)
            connection.execute(
                """INSERT INTO dispatch_observations
                     (namespace_id, snapshot_id, row_uid, source_event_id, channel_id, object_id,
                      sensor_type, system_type, value_raw, value_numeric, alarm, event_at,
                      available_at, availability_basis, source_file, source_sha256,
                      record_ordinal, event_local_raw, quality_flags, received_position)
                   VALUES (%s, %s, %s, %s, %s, '5001', 'Состояние фазы', 'Электроснабжение',
                           %s, NULL, %s, %s, %s, 'observed', 'curated', %s, %s, %s, '[]', %s)""",
                (
                    NAMESPACE,
                    STREAM,
                    hashlib.sha256(f"{source_sha}:{ordinal}".encode()).hexdigest(),
                    event_id,
                    channel,
                    value,
                    alarm,
                    at,
                    at,
                    source_sha,
                    ordinal,
                    f"{local:%Y-%m-%d %H:%M:%S}",
                    ordinal,
                ),
            )
        connection.execute(
            """UPDATE dispatch_replay_snapshots SET row_count = %s, alarm_count = %s
               WHERE namespace_id = %s AND snapshot_id = %s""",
            (len(records), sum(record[4] is True for record in records), NAMESPACE, STREAM),
        )


def test_seeded_history_under_another_row_uid_is_a_duplicate(client, settings, dsn):
    """P0-1 of the 29.09.2026 rehearsal: the natural key decides, not the row_uid.

    The seeded history and the ingestion compute row_uid differently; a seeded record
    sent again (file or API batch) must be a duplicate, and a stored record without the
    source flag gets it filled, whatever its row_uid.
    """
    load_reference(client, settings, {"700001": "5001", "700002": "5001"})
    seed_like(
        owner_dsn(dsn),
        [
            ("9000001", "700001", datetime(2026, 6, 30, 12, 0, 0), "Есть питание", False),
            (None, "700002", datetime(2026, 6, 30, 12, 0, 5), "Обесточен", False),
            ("9000003", "700001", datetime(2026, 6, 30, 12, 0, 9), "Неисправен", None),
        ],
    )
    rows = [
        ("9000001", "700001", "2026-06-30", "12:00:00", False, "Есть питание"),  # seeded
        ("", "700002", "2026-06-30", "12:00:05", False, "Обесточен"),  # seeded, no event ID
        ("9000003", "700001", "2026-06-30", "12:00:09", True, "Неисправен"),  # seeded, no flag
        ("9000001", "700001", "2026-06-30", "12:00:00", False, "Обесточен"),  # other value
        ("9000004", "700002", "2026-06-30", "12:00:05", False, "Обесточен"),  # other event ID
    ]
    item = upload(client, "journal-2026-06-30.csv", journal(rows))
    drain(settings)
    done = report(client, item.id)
    assert (done.rows_total, done.rows_accepted, done.rows_duplicate) == (5, 2, 3)
    assert "Признак тревожности дополнен у ранее принятых записей: 1" in done.notes
    with psycopg.connect(dsn) as connection:
        keys = connection.execute(
            """SELECT source_event_id, channel_id, value_raw, alarm, count(*)
               FROM dispatch_observations
               GROUP BY source_event_id, channel_id, event_at, value_raw, alarm
               ORDER BY 1 NULLS FIRST, 2, 3"""
        ).fetchall()
        scope = connection.execute(
            "SELECT row_count, alarm_count FROM dispatch_replay_snapshots"
        ).fetchone()
    assert keys == [
        (None, "700002", "Обесточен", False, 1),
        ("9000001", "700001", "Есть питание", False, 1),
        ("9000001", "700001", "Обесточен", False, 1),
        ("9000003", "700001", "Неисправен", True, 1),
        ("9000004", "700002", "Обесточен", False, 1),
    ]
    assert scope == (5, 1)


def test_reference_version_applies_forward_only(client, settings, dsn):
    v1 = load_reference(client, settings, {"700001": "5001"})
    first = upload(
        client, "day1.csv", journal([("1", "700001", "2026-09-20", "01:00:00", False, "a")])
    )
    drain(settings)
    v2 = load_reference(client, settings, {"700001": "5002", "700009": "5009"})
    assert v2.reference_version != v1.reference_version
    second = upload(
        client, "day2.csv", journal([("2", "700001", "2026-09-21", "01:00:00", False, "b")])
    )
    drain(settings)
    assert report(client, first.id).reference_version == v1.reference_version
    assert report(client, second.id).reference_version == v2.reference_version
    with psycopg.connect(dsn) as connection:
        mapped = connection.execute(
            "SELECT value_raw, object_id, reference_version FROM dispatch_observations ORDER BY 1"
        ).fetchall()
    assert mapped == [("a", "5001", v1.reference_version), ("b", "5002", v2.reference_version)]
    # The same bytes again are a duplicate and do not switch the active version back.
    again = upload(client, "channels.csv", channels({"700001": "5001"}), "reference_channels_csv")
    drain(settings)
    assert report(client, again.id).duplicate_of == v1.id
    third = upload(
        client, "day3.csv", journal([("3", "700001", "2026-09-22", "01:00:00", False, "c")])
    )
    drain(settings)
    assert report(client, third.id).reference_version == v2.reference_version


def test_file_level_failures_are_reported(client, settings, dsn):
    no_ref = upload(
        client, "early.csv", journal([("1", "700001", "2026-09-20", "01:00:00", False, "a")])
    )
    drain(settings)
    failed = report(client, no_ref.id)
    assert (failed.status, failed.error_code) == ("failed", "reference_missing")
    assert failed.finished_at is not None

    load_reference(client, settings, {"700001": "5001"})
    header = upload(client, "wrong.csv", b"a,b,c\n1,2,3\n")
    only_bad = upload(
        client, "bad.csv", journal([("1", "700001", "2026-13-01", "01:00:00", False, "a")])
    )
    utf16 = upload(client, "utf16.csv", JOURNAL_HEADER.encode("utf-16"))
    drain(settings)
    assert report(client, header.id).error_code == "bad_header"
    assert report(client, utf16.id).error_code == "bad_encoding"
    bad = report(client, only_bad.id)
    assert (bad.error_code, bad.rows_quarantined, bad.quarantine_reasons) == (
        "no_valid_rows",
        1,
        {"bad_date": 1},
    )
    assert bad.quarantine_sample[0].line_no == 2
    # The earlier failed upload is not a duplicate target: the same bytes load now.
    retry = upload(
        client, "early.csv", journal([("1", "700001", "2026-09-20", "01:00:00", False, "a")])
    )
    drain(settings)
    assert (report(client, retry.id).status, report(client, retry.id).rows_accepted) == (
        "imported",
        1,
    )
    assert scalar(dsn, "SELECT count(*) FROM dispatch_observations") == 1


def test_crash_inside_load_leaves_nothing_and_retry_loads_once(client, settings, dsn, monkeypatch):
    load_reference(client, settings, {"700001": "5001"})
    rows = [("1", "700001", "2026-09-20", f"00:00:0{i}", False, str(i)) for i in range(5)]
    rows.append(("9", "700001", "2026-09-20", "99:00:00", False, "q"))
    item = upload(client, "day.csv", journal(rows))
    original = runner.load_journal

    def crash_after_insert(connection, **kwargs):
        original(connection, **kwargs)
        assert connection.execute("SELECT count(*) FROM dispatch_observations").fetchone()[0] == 5
        raise RuntimeError("simulated crash in the middle of the file")

    monkeypatch.setattr(runner, "load_journal", crash_after_insert)
    assert run_once(worker(settings), recompute_stub)
    assert report(client, item.id).status == "queued"
    assert scalar(dsn, "SELECT count(*) FROM dispatch_observations") == 0
    assert scalar(dsn, "SELECT count(*) FROM import_quarantine") == 0
    assert scalar(dsn, "SELECT count(*) FROM dispatch_replay_snapshots") == 0
    job = scalar(
        dsn,
        "SELECT ARRAY[state, attempts::text, last_error] FROM jobs WHERE import_id = %s",
        (item.id,),
    )
    assert job == ["queued", "1", "RuntimeError"]

    monkeypatch.setattr(runner, "load_journal", original)
    drain(settings)
    done = report(client, item.id)
    assert (done.status, done.rows_accepted, done.rows_quarantined) == ("imported", 5, 1)
    assert scalar(dsn, "SELECT count(*) FROM dispatch_observations") == 5
    assert scalar(dsn, "SELECT attempts FROM jobs WHERE import_id = %s", (item.id,)) == 2


def test_restarted_worker_resumes_after_lease_expiry(client, settings, dsn, monkeypatch):
    load_reference(client, settings, {"700001": "5001"})
    item = upload(
        client, "day.csv", journal([("1", "700001", "2026-09-20", "01:00:00", False, "a")])
    )
    later = upload(
        client, "day2.csv", journal([("2", "700001", "2026-09-21", "01:00:00", False, "b")])
    )
    # A worker claims the job and dies after committing «parsing».
    with psycopg.connect(dsn, autocommit=True) as connection:
        claim, _ = claim_next(connection, worker_id="crashed-worker", lease_seconds=60)
        assert claim is not None and claim.import_id == item.id
        connection.execute(
            "UPDATE import_files SET status = 'parsing' WHERE import_id = %s", (item.id,)
        )
    # While the lease is live nothing else is taken: uploads stay in order.
    assert run_once(worker(settings, "worker-b"), recompute_stub) is False
    assert report(client, later.id).status == "queued"
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second'"
        )

    # The restarted process (CLI entry point) takes the job over and finishes the queue.
    # It resolves the recompute of B2 (RECOMPUTE_MODULE), so both journals are published.
    monkeypatch.setenv("INFRA_DB_DSN", dsn)
    monkeypatch.setenv("INFRA_RECEIVED_NAMESPACE", NAMESPACE)
    monkeypatch.setenv("INFRA_RECEIVED_STREAM_ID", STREAM)
    monkeypatch.setenv("INFRA_UPLOAD_DIR", str(settings.upload_dir))
    monkeypatch.setenv("INFRA_WORKER_MIGRATIONS_DIR", str(MIGRATIONS))
    assert worker_main.main(["--once"]) == 0
    assert [report(client, i.id).status for i in (item, later)] == ["published", "published"]
    with psycopg.connect(dsn) as connection:
        jobs = connection.execute(
            "SELECT state, attempts, lease_owner FROM jobs WHERE kind = 'import' ORDER BY job_id"
        ).fetchall()
        order = connection.execute(
            "SELECT value_raw FROM dispatch_observations ORDER BY received_position"
        ).fetchall()
    assert jobs[-2:] == [("done", 2, None), ("done", 1, None)]
    assert order == [("a",), ("b",)]

    # A job whose last allowed attempt lost its lease is failed, not retried forever.
    stuck = upload(
        client, "day3.csv", journal([("3", "700001", "2026-09-22", "01:00:00", False, "c")])
    )
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("UPDATE jobs SET max_attempts = 1 WHERE import_id = %s", (stuck.id,))
        claim_next(connection, worker_id="crashed-worker", lease_seconds=60)
        connection.execute(
            "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second'"
        )
    assert run_once(worker(settings, "worker-b"), recompute_stub)
    assert (report(client, stuck.id).status, report(client, stuck.id).error_code) == (
        "failed",
        "internal_error",
    )


def test_recompute_result_publishes_or_fails_without_losing_rows(client, settings, dsn):
    load_reference(client, settings, {"700001": "5001"})
    calls = []

    def publish(connection, *, data_as_of, import_id):
        calls.append((data_as_of, import_id))
        return SimpleNamespace(generation=7, new_card_ids=["card-1"], released_card_ids=[])

    item = upload(
        client, "day.csv", journal([("1", "700001", "2026-09-20", "05:00:00", True, "a")])
    )
    drain(settings, recompute=publish)
    published = report(client, item.id)
    assert (published.status, published.forecast_generation, published.new_card_ids) == (
        "published",
        7,
        ["card-1"],
    )
    assert published.finished_at is not None
    assert {timing.stage for timing in published.timings} == {"store", "parse", "load", "publish"}
    assert calls == [(datetime(2026, 9, 20, 2, 0, tzinfo=UTC), item.id)]

    def broken(connection, *, data_as_of, import_id):
        raise RuntimeError("B2 failure")

    failing = upload(
        client, "day2.csv", journal([("2", "700001", "2026-09-21", "05:00:00", False, "b")])
    )
    drain(settings, recompute=broken)
    failed = report(client, failing.id)
    assert (failed.status, failed.error_code, failed.rows_accepted) == (
        "failed",
        "recompute_failed",
        1,
    )
    assert scalar(dsn, "SELECT count(*) FROM dispatch_observations") == 2


def test_daily_170k_row_csv_is_processed_within_budget(client, settings, dsn):
    """Measured locally; the numbers are printed for the hand-over (run with -s).

    The local budget is 15 s (measured 4-5 s). Shared CI runners are about three times
    slower on the load stage, so CI passes its own budget; the product limit (OPS-01)
    is 300 s receive->UI.
    """
    budget = float(os.environ.get("INFRA_TEST_CSV_170K_BUDGET_SECONDS", "15"))
    channel_ids = [str(800000 + index) for index in range(11_500)]
    load_reference(
        client,
        settings,
        {channel: str(6000 + index % 78) for index, channel in enumerate(channel_ids)},
    )
    day = datetime(2026, 9, 23)
    lines = [JOURNAL_HEADER]
    for index in range(170_000):
        at = day + timedelta(seconds=index * 86_399 // 170_000)
        value = "Неисправен" if index % 997 == 0 else str(index % 40)
        alarm = "true" if index % 997 == 0 else "false"
        lines.append(
            f"{5_000_000_000 + index},{channel_ids[index % len(channel_ids)]},"
            f'"{at:%Y-%m-%d}","{at:%H:%M:%S}",{alarm},"{value}"'
        )
    data = ("﻿" + "\n".join(lines) + "\n").encode()

    started = time.perf_counter()
    item = upload(client, "synthetic-day.csv", data)
    uploaded = time.perf_counter()
    assert drain(settings) == 1
    finished = time.perf_counter()
    done = report(client, item.id)
    assert (done.status, done.rows_accepted, done.rows_quarantined) == ("imported", 170_000, 0)
    timings = {timing.stage: timing.seconds for timing in done.timings}
    total = finished - started
    print(
        f"\n170k rows, {len(data) / 1e6:.1f} MB: upload {uploaded - started:.2f}s, "
        f"worker {finished - uploaded:.2f}s, total {total:.2f}s, budget {budget:.0f}s, "
        f"stages {timings}"
    )
    assert total < budget
