"""B1 → B2 → B3 on PostgreSQL: an uploaded journal is published, decided and journaled.

The ingestion worker (B1) finds ``operations.forecast_publish.recompute`` (B2) through
``RECOMPUTE_MODULE``; a synthetic phase CSV of a little over a year makes the import
``published`` with cards. A dispatcher decision (B3) on a published card is stored
(201), on a card that is not published it is 404, and the forecast journal shows the
decision from its time on (nothing later than ``as_of``): the synthetic history ends
tomorrow, so the scope's ``data_as_of`` is after the decision. Needs
``INFRA_TEST_RECEIVED_DSN`` (``make check-db``); every test uses its own schema, dropped
after it. All rows are synthetic.
"""

import io
import os
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from app_role import app_dsn
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_backend.operations import forecast_publish
from infra_pulse_backend.worker import __main__ as worker_main
from infra_pulse_backend.worker import runner
from infra_pulse_core.contracts.forecast import (
    ForecastDecisionSummary,
    ForecastJournalList,
    ForecastList,
)
from infra_pulse_core.contracts.imports import ImportFile
from infra_pulse_core.features.phase_feeder_episodes import MSK

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
NAMESPACE = "b123-test"
STREAM = "b123-stream"
PHASE = "Состояние фазы"
DAYS = 380  # the list needs 365 days of history per object
# The history ends tomorrow (MSK): the journal's as_of is later than a decision taken now.
FIRST_DAY = datetime.now(MSK).date() - timedelta(days=DAYS - 2)
OBJECTS = [str(910000 + index) for index in range(6)]
FEEDERS = ["ГРО{n} ПК{p}", "ФВ{n} ПК{p}-{q}", "ФАНС{n} ПК{p}", "Резерв {n}"]
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
    schema = f"b123_{uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    scoped = make_conninfo(base, options=f"-csearch_path={schema}")
    worker_main.apply_migrations(scoped, MIGRATIONS)
    try:
        yield app_dsn(scoped)
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


def channel_ids(object_id: str) -> list[str]:
    return [f"{object_id}{index}" for index in range(len(FEEDERS) + 1)]


def channels_csv() -> bytes:
    lines = [CHANNELS_HEADER]
    for number, object_id in enumerate(OBJECTS):
        ids = channel_ids(object_id)
        for index, template in enumerate(FEEDERS):
            name = template.format(n=index + 1, p=10 * index + number, q=10 * index + 5)
            lines.append(f"{ids[index]},Электроснабжение,{PHASE},SYN-{index},{name},{object_id}")
        lines.append(f"{ids[-1]},Электроснабжение,{PHASE},SYN-IN,АВР Ввод1 ПК{number},{object_id}")
    return ("\n".join(lines) + "\n").encode()


def journal_csv(first: int, last: int, seed: int = 11) -> bytes:
    """Daily «Есть питание» on every channel and «Неисправен» → «Обесточен» → «Норма»
    episodes on the first three feeders (object ``i`` fails more often than ``i - 1``)."""
    rng = random.Random(seed)
    lines = [JOURNAL_HEADER]
    event_id = 0

    def row(channel: str, day: date, clock: str, value: str) -> None:
        nonlocal event_id
        event_id += 1
        alarm = "true" if value == "Неисправен" else "false"
        lines.append(f'{event_id},{channel},"{day.isoformat()}","{clock}",{alarm},"{value}"')

    for offset in range(first, last):
        day = FIRST_DAY + timedelta(days=offset)
        for number, object_id in enumerate(OBJECTS):
            chance = 0.03 + 0.03 * number
            for index, channel in enumerate(channel_ids(object_id)):
                row(channel, day, "06:00:00", "Есть питание")
                if index < 3 and rng.random() < chance:
                    row(channel, day, f"10:{index * 3:02d}:00", "Неисправен")
                    row(channel, day, f"10:{index * 3 + 2:02d}:00", "Обесточен")
                    row(channel, day, f"12:{index * 3:02d}:00", "Норма")
    return ("\n".join(lines) + "\n").encode()


def upload(client: TestClient, name: str, data: bytes, fmt: str = "journal_csv") -> ImportFile:
    response = client.post(
        "/api/v1/imports",
        files={"file": (name, io.BytesIO(data), "text/csv")},
        data={"format": fmt},
    )
    assert response.status_code == 202, response.text
    return ImportFile.model_validate(response.json())


def drain(settings: Settings, recompute) -> int:
    config = runner.WorkerConfig(
        dsn=settings.db_dsn.get_secret_value(),
        upload_dir=settings.upload_dir,
        namespace_id=NAMESPACE,
        stream_id=STREAM,
        worker_id="worker-b123",
        lease_seconds=60,
        retry_seconds=0,
    )
    processed = 0
    while runner.run_once(config, recompute):
        processed += 1
    return processed


def report(client: TestClient, import_id: str) -> ImportFile:
    response = client.get(f"/api/v1/imports/{import_id}")
    assert response.status_code == 200, response.text
    return ImportFile.model_validate(response.json())


def test_uploaded_journal_is_published_decided_and_journaled(dsn, tmp_path):
    settings = Settings(
        mode="received",
        db_dsn=dsn,
        upload_dir=tmp_path / "uploads",
        received_namespace=NAMESPACE,
        received_stream_id=STREAM,
        _env_file=None,
    )
    client = TestClient(create_app(settings))
    # The worker of B1 resolves the recompute of B2, not its stub.
    recompute = runner.resolve_recompute()
    assert recompute is forecast_publish.recompute

    reference = upload(client, "channels.csv", channels_csv(), "reference_channels_csv")
    assert drain(settings, recompute) == 1
    assert report(client, reference.id).status == "published"

    history = upload(client, "history.csv", journal_csv(0, DAYS))
    assert drain(settings, recompute) == 1
    loaded = report(client, history.id)
    assert loaded.status == "published", loaded
    assert loaded.forecast_generation == 1 and loaded.finished_at is not None
    assert loaded.new_card_ids and {timing.stage for timing in loaded.timings} >= {
        "store",
        "parse",
        "load",
        "detect",
        "score",
        "publish",
    }
    with psycopg.connect(dsn) as connection:
        stored = connection.execute(
            """SELECT card_id, status, object_id, issued_at, clock_timestamp()
               FROM forecast_cards WHERE namespace_id = %s AND snapshot_id = %s""",
            (NAMESPACE, STREAM),
        ).fetchall()
    assert {row[0] for row in stored} == set(loaded.new_card_ids)
    scored = sorted(row[0] for row in stored if row[1] == "scored")
    assert scored, "a year of history gives scored cards"

    listing = ForecastList.model_validate(client.get("/api/v1/forecasts").json())
    assert listing.mode == "received" and listing.runs[0].generation == 1
    assert {view.card.id for view in listing.items} <= set(scored)

    # The next day: another upload, another publication.
    next_day = upload(client, "day.csv", journal_csv(DAYS, DAYS + 1, seed=12))
    assert drain(settings, recompute) == 1
    assert report(client, next_day.id).forecast_generation == 2

    # B3 checks the card through forecast_db.forecast_card before storing a decision.
    # The earliest scored card: published well before the decision is taken.
    _, card_id, object_id = min(
        (issued_at, key, obj)
        for key, status, obj, issued_at, now in stored
        if status == "scored" and issued_at < now
    )
    body = {
        "decision_code": "R2",
        "reason_code": "R2.1",
        "reason_text": "синтетическая проверка по журналу",
        "verification_methods": ["source_records"],
        "idempotency_key": f"synthetic-{uuid4()}",
        "expected_revision": 0,
    }
    created = client.post(f"/api/v1/forecasts/{card_id}/decisions", json=body)
    assert created.status_code == 201, created.text
    decision = ForecastDecisionSummary.model_validate(created.json())
    assert (decision.revision, decision.simulated, decision.actor_id) == (
        1,
        False,
        "local-operator",
    )
    missing = client.post(
        "/api/v1/forecasts/feeders-14d-000000-20990101/decisions",
        json=body | {"idempotency_key": f"synthetic-{uuid4()}"},
    )
    assert (missing.status_code, missing.json()["detail"]) == (404, "forecast_not_found")
    assert client.get("/api/v1/forecasts/unknown-card/decisions").status_code == 404

    journal = ForecastJournalList.model_validate(
        client.get("/api/v1/forecast-journal", params={"object_id": object_id, "limit": 100}).json()
    )
    entries = {entry.card.id: entry for entry in journal.items}
    assert journal.total == len(entries)
    assert entries[card_id].decision is not None
    assert (entries[card_id].decision.decision_code, entries[card_id].decision.revision) == (
        "R2",
        1,
    )
    assert all(entry.decision is None for key, entry in entries.items() if key != card_id)
    # Before the decision was taken the journal does not show it.
    earlier = ForecastJournalList.model_validate(
        client.get(
            "/api/v1/forecast-journal",
            params={
                "object_id": object_id,
                "limit": 100,
                "as_of": (decision.decided_at - timedelta(seconds=1)).isoformat(),
            },
        ).json()
    )
    assert card_id in {entry.card.id for entry in earlier.items}
    assert all(entry.decision is None for entry in earlier.items)
    with psycopg.connect(dsn) as connection:
        audited = connection.execute(
            """SELECT count(*) FROM audit_events
               WHERE action = 'decision.created' AND target_id = %s""",
            (card_id,),
        ).fetchone()[0]
    assert audited == 1


def test_file_of_stored_records_keeps_the_publication(dsn, tmp_path):
    """P2-5 of the 29.09.2026 rehearsal: a file whose every record is already stored is
    ``published`` with the current generation and no recompute; after a new channel
    reference or with a new record the worker recomputes as before."""
    settings = Settings(
        mode="received",
        db_dsn=dsn,
        upload_dir=tmp_path / "uploads",
        received_namespace=NAMESPACE,
        received_stream_id=STREAM,
        _env_file=None,
    )
    client = TestClient(create_app(settings))
    recompute = runner.resolve_recompute()
    upload(client, "channels.csv", channels_csv(), "reference_channels_csv")
    drain(settings, recompute)
    history = journal_csv(0, 5)
    first = upload(client, "history.csv", history)
    drain(settings, recompute)
    assert report(client, first.id).forecast_generation == 1

    def runs() -> list[tuple]:
        with psycopg.connect(dsn) as connection:
            return connection.execute(
                """SELECT generation, import_id FROM forecast_runs
                   WHERE namespace_id = %s AND snapshot_id = %s ORDER BY generation""",
                (NAMESPACE, STREAM),
            ).fetchall()

    header, *records = history.decode().splitlines()
    # Other bytes (not a byte duplicate of the upload), the same records.
    again = upload(client, "again.csv", "\n".join([header, *records[::-1]]).encode())
    drain(settings, recompute)
    kept = report(client, again.id)
    assert (kept.status, kept.forecast_generation, kept.rows_accepted, kept.rows_duplicate) == (
        "published",
        1,
        0,
        len(records),
    )
    assert runner.UNCHANGED_NOTE in kept.notes
    assert {timing.stage for timing in kept.timings} == {"store", "parse", "load"}
    assert not kept.new_card_ids and not kept.released_card_ids
    assert runs() == [(1, first.id)]

    # A new channel reference reaches the layout only through a recompute.
    extra = f"999999,Электроснабжение,{PHASE},SYN-X,Резерв 9,{OBJECTS[0]}\n"
    upload(client, "channels-v2.csv", channels_csv() + extra.encode(), "reference_channels_csv")
    drain(settings, recompute)
    after_reference = upload(client, "again-2.csv", "\n".join([header, *records[1:]]).encode())
    drain(settings, recompute)
    assert report(client, after_reference.id).forecast_generation == 2

    new_day = upload(client, "day.csv", journal_csv(5, 6, seed=12))
    drain(settings, recompute)
    assert report(client, new_day.id).forecast_generation == 3
    assert runs() == [(1, first.id), (2, after_reference.id), (3, new_day.id)]
