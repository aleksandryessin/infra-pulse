"""The read cache of ``forecast_db`` on PostgreSQL: nothing new is hidden (OPS-01).

With every cached read warm, a new publication (generation 2 releases an open card), a
dispatcher decision and a check result are visible on the very next request: the cache
is keyed by the publication, decisions and check results are never cached. The TTL is
raised to an hour here, so only the key can explain what the test sees. Synthetic
observations; the history ends tomorrow (MSK), so a decision taken now lies before
``data_as_of`` and belongs to the journal. Needs ``INFRA_TEST_RECEIVED_DSN`` on a
disposable database (``make check-db``); the test uses its own scope.
"""

import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from app_role import app_dsn
from fastapi.testclient import TestClient

from infra_pulse_backend.api import forecast_db
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.api.read_cache import ReadCache
from infra_pulse_backend.config import Settings
from infra_pulse_backend.operations import forecast_publish as fp
from infra_pulse_core.contracts.forecast import (
    ForecastJournalCounts,
    ForecastJournalList,
    ForecastList,
    ForecastState,
)
from infra_pulse_core.contracts.scheme import ObjectScheme, ObjectSchemeList
from infra_pulse_core.features.phase_feeder_episodes import MSK

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PHASE = "Состояние фазы"
DAYS = 380  # the list needs 365 days of history per object
FIRST = datetime.combine(
    datetime.now(MSK).date() - timedelta(days=DAYS - 2), datetime.min.time(), MSK
)
NAMES = ["ГРО{n} ПК{p}", "ФВ{n} ПК{p}-{q}", "ФАНС{n} ПК{p}"]


@pytest.fixture(scope="module")
def dsn() -> str:
    value = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not value:
        pytest.skip("local PostgreSQL integration DSN not provided")
    with psycopg.connect(value) as connection:
        for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))
    return app_dsn(value)


class Scope:
    def __init__(self, dsn: str, objects: int = 6):
        self.dsn = dsn
        self.namespace = "cache-test"
        self.stream = f"cache-{uuid4().hex[:10]}"
        self.position = 0
        self.random = random.Random(5)
        # Decisions are keyed by card ID (object + cutoff): new objects on every run.
        base = 10 * random.randrange(10_000_000, 99_999_999)
        self.objects = [str(base + index) for index in range(objects)]
        self.channels = {
            object_id: [f"{self.stream}-{object_id}-{index}" for index in range(len(NAMES))]
            for object_id in self.objects
        }
        with psycopg.connect(dsn) as connection:
            connection.execute(
                """INSERT INTO dispatch_replay_snapshots (namespace_id, snapshot_id,
                     manifest_sha256, window_start, window_end, row_count, alarm_count,
                     scope_kind)
                   VALUES (%s, %s, NULL, %s, %s, 0, 0, 'received')""",
                (self.namespace, self.stream, FIRST, FIRST + timedelta(days=DAYS + 5)),
            )
            fp.pg.upsert_layout(
                connection,
                [
                    fp.layout_row(
                        channel,
                        name=NAMES[index].format(n=index + 1, p=10 * index + number, q=5),
                        object_id=object_id,
                        sensor_type=PHASE,
                        system_type="Диспетчерский контроль",
                        tag=f"SYN-{index}",
                        reference_version="synthetic-ref-v1",
                    )
                    for number, object_id in enumerate(self.objects)
                    for index, channel in enumerate(self.channels[object_id])
                ],
            )
            fp.pg.upsert_objects(
                connection, [(oid, f"Синтетический объект {oid}", "syn") for oid in self.objects]
            )

    def days(self, first: int, last: int, *, quiet: bool = False) -> list[tuple]:
        rows = []
        for day in range(first, last):
            base = FIRST + timedelta(days=day)
            for number, object_id in enumerate(self.objects):
                chance = 0 if quiet else 0.03 + 0.03 * number
                for channel in self.channels[object_id]:
                    rows.append((channel, object_id, base + timedelta(hours=6), "Есть питание"))
                    if self.random.random() < chance:
                        start = base + timedelta(hours=10)
                        rows.append((channel, object_id, start, "Неисправен"))
                        rows.append((channel, object_id, start + timedelta(minutes=2), "Обесточен"))
                        rows.append((channel, object_id, start + timedelta(hours=2), "Норма"))
        return rows

    def insert(self, rows: list[tuple]) -> None:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                with cursor.copy(
                    """COPY dispatch_observations (namespace_id, snapshot_id, row_uid,
                         channel_id, object_id, sensor_type, system_type, value_raw, alarm,
                         event_at, available_at, availability_basis, source_file,
                         source_sha256, record_ordinal, event_local_raw, received_position)
                       FROM STDIN"""
                ) as copy:
                    for channel, object_id, at, value in rows:
                        self.position += 1
                        copy.write_row(
                            (
                                self.namespace,
                                self.stream,
                                f"row-{self.position}",
                                channel,
                                object_id,
                                PHASE,
                                "Диспетчерский контроль",
                                value,
                                value == "Неисправен",
                                at,
                                at,
                                "observed",
                                "synthetic.csv",
                                "0" * 64,
                                self.position,
                                at.strftime("%d.%m.%Y %H:%M:%S"),
                                self.position,
                            )
                        )

    def publish(self, last_day: int, **kwargs) -> fp.PublishResult:
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            return fp.recompute(
                connection,
                data_as_of=FIRST + timedelta(days=last_day) - timedelta(seconds=1),
                namespace_id=self.namespace,
                snapshot_id=self.stream,
                **kwargs,
            )

    def client(self) -> TestClient:
        settings = Settings(
            mode="received",
            db_dsn=self.dsn,
            received_namespace=self.namespace,
            received_stream_id=self.stream,
            _env_file=None,
        )
        return TestClient(create_app(settings))


def warm(client: TestClient) -> dict:
    """Every cached read, twice: the second answer comes from the cache."""
    answers = {}
    for _ in range(2):
        answers = {
            "state": ForecastState.model_validate(client.get("/api/v1/forecast-state").json()),
            "open": ForecastList.model_validate(client.get("/api/v1/forecasts").json()),
            "released": ForecastList.model_validate(
                client.get("/api/v1/forecasts", params={"list_state": "released"}).json()
            ),
            "counts": ForecastJournalCounts.model_validate(
                client.get("/api/v1/forecast-journal/summary").json()
            ),
            "journal": ForecastJournalList.model_validate(
                client.get("/api/v1/forecast-journal", params={"limit": 100}).json()
            ),
            "schemes": ObjectSchemeList.model_validate(client.get("/api/v1/schemes").json()),
        }
    return answers


def journal(client: TestClient, **params) -> ForecastJournalList:
    response = client.get("/api/v1/forecast-journal", params={"limit": 100, **params})
    assert response.status_code == 200, response.text
    return ForecastJournalList.model_validate(response.json())


def test_new_generation_decision_and_check_result_are_read_at_once(dsn, monkeypatch):
    monkeypatch.setattr(forecast_db, "READ_CACHE", ReadCache(ttl_seconds=3600))
    scope = Scope(dsn)
    scope.insert(scope.days(0, DAYS))
    assert scope.publish(DAYS, list_from=(FIRST + timedelta(days=DAYS - 10)).date()).generation == 1
    client = scope.client()
    before = warm(client)
    # Cards of the publication, two lists, the counters, the schemes, the recommendation
    # inputs (earlier cards of each pair, section switches); the journal and the state are
    # assembled per request (decisions, check results, source time).
    assert len(forecast_db.READ_CACHE) == 6
    assert before["state"].generation == 1
    assert before["open"].items, "a year of synthetic history opens cards"
    opened = before["open"].items[0].card
    scheme = ObjectScheme.model_validate(client.get(f"/api/v1/schemes/{opened.object_id}").json())
    assert opened.id in scheme.open_card_ids

    # Generation 2: the first listed feeder of the open card fails the day after tomorrow.
    event_at = FIRST + timedelta(days=DAYS, hours=15)
    feeder = opened.channels[0].channel_id
    scope.insert(
        [
            (feeder, opened.object_id, event_at, "Неисправен"),
            (feeder, opened.object_id, event_at + timedelta(hours=1), "Норма"),
        ]
        + scope.days(DAYS, DAYS + 1, quiet=True)
    )
    second = scope.publish(DAYS + 1)
    assert second.generation == 2 and opened.id in second.released_card_ids
    after = warm(client)
    assert after["state"].generation == 2
    assert after["state"].data_as_of > before["state"].data_as_of
    assert after["open"].runs[0].generation == 2
    assert after["open"].publication_token != before["open"].publication_token
    assert opened.id not in {view.card.id for view in after["open"].items}
    released = {view.card.id: view for view in after["released"].items}
    assert released[opened.id].released_at == event_at
    assert sum(row.cards_released for row in after["counts"].rows) == 1 + sum(
        row.cards_released for row in before["counts"].rows
    )
    entry = next(item for item in after["journal"].items if item.card.id == opened.id)
    assert entry.outcome.status == "realized" and entry.outcome.first_event_at == event_at
    scheme = ObjectScheme.model_validate(client.get(f"/api/v1/schemes/{opened.object_id}").json())
    assert opened.id not in scheme.open_card_ids
    assert opened.id in {card.forecast_id for card in scheme.released_7d}
    summary = next(item for item in after["schemes"].items if item.object_id == opened.object_id)
    assert summary.released_7d >= 1

    # A decision: «без решения» loses the card on the next request, the entry carries it.
    now = datetime.now(MSK)
    card_id = next(
        item.card.id
        for item in reversed(journal(client).items)
        if item.card.status == "scored" and item.card.published_at < now
    )
    assert card_id in {item.card.id for item in journal(client, decision_state="none").items}
    assert card_id not in {item.card.id for item in journal(client, decision_state="any").items}
    created = client.post(
        f"/api/v1/forecasts/{card_id}/decisions",
        json={
            "decision_code": "R2",
            "reason_code": "R2.1",
            "reason_text": "синтетическая проверка по журналу",
            "verification_methods": ["source_records"],
            "idempotency_key": f"synthetic-{uuid4()}",
            "expected_revision": 0,
        },
    )
    assert created.status_code == 201, created.text
    assert card_id not in {item.card.id for item in journal(client, decision_state="none").items}
    decided = {item.card.id: item for item in journal(client, decision_state="any").items}
    assert decided[card_id].decision is not None
    assert decided[card_id].decision.decision_code == "R2"

    # A check result: shown in the journal entry on the next request.
    assert decided[card_id].check_result is None
    recorded = client.post(
        f"/api/v1/forecasts/{card_id}/check-result",
        json={
            "check_result": "no_violation",
            "result_at": (now - timedelta(minutes=5)).isoformat(),
            "idempotency_key": f"synthetic-{uuid4()}",
            "expected_revision": 0,
        },
    )
    assert recorded.status_code == 201, recorded.text
    checked = {item.card.id: item for item in journal(client, decision_state="any").items}
    assert checked[card_id].check_result is not None
    assert checked[card_id].check_result.check_result == "no_violation"
