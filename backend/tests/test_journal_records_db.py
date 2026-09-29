"""Decisions and check results in the forecast journal on PostgreSQL (audit Б-1, 29.09.2026).

The stand replays history that ends months before the jury decides: ``data_as_of`` is in
the past, a decision is saved with the wall clock after it. The journal reads decisions
and check results up to ``records_as_of`` (the read, or a requested ``as_of``), cards and
outcomes up to ``as_of`` (data). Both cases on one synthetic scope: generation 1 ends 60
days ago (decision after the data), generation 2 ends tomorrow (the same decision inside
the data window). Counters and quality do not change with decisions; «без решения» and
the monthly report agree with the entries; a journal cursor pins its records bound.
Readiness reports the published forecast (audit I7). Needs ``INFRA_TEST_RECEIVED_DSN``
(``make check-db``); the test uses its own schema, dropped after it. Synthetic rows only.
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
from psycopg.conninfo import make_conninfo

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_backend.operations import forecast_publish as fp
from infra_pulse_backend.worker import __main__ as worker_main
from infra_pulse_core.contracts.attention import Capabilities
from infra_pulse_core.contracts.forecast import (
    ForecastCheckResult,
    ForecastDecisionSummary,
    ForecastJournalList,
)
from infra_pulse_core.contracts.reports import MonthlyReport
from infra_pulse_core.features.phase_feeder_episodes import MSK

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
NAMESPACE = "journal-records-test"
PHASE = "Состояние фазы"
DAYS = 380  # the list needs 365 days of history per object
BEHIND = 60  # generation 1 ends this many days before today, like the stand (30.06)
FIRST = datetime.combine(
    datetime.now(MSK).date() - timedelta(days=DAYS + BEHIND), datetime.min.time(), MSK
)
NAMES = ["ГРО{n} ПК{p}", "ФВ{n} ПК{p}-{q}", "ФАНС{n} ПК{p}"]


@pytest.fixture
def dsn():
    base = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not base:
        pytest.skip("local PostgreSQL integration DSN not provided")
    schema = f"journal_records_{uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    scoped = make_conninfo(base, options=f"-csearch_path={schema}")
    worker_main.apply_migrations(scoped, MIGRATIONS)
    try:
        yield app_dsn(scoped)
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


class Scope:
    def __init__(self, dsn: str, objects: int = 6):
        self.dsn = dsn
        self.stream = f"records-{uuid4().hex[:10]}"
        self.position = 0
        self.random = random.Random(7)
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
                (NAMESPACE, self.stream, FIRST, FIRST + timedelta(days=DAYS + BEHIND + 5)),
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
                                NAMESPACE,
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
                namespace_id=NAMESPACE,
                snapshot_id=self.stream,
                **kwargs,
            )

    def client(self) -> TestClient:
        settings = Settings(
            mode="received",
            db_dsn=self.dsn,
            received_namespace=NAMESPACE,
            received_stream_id=self.stream,
            _env_file=None,
        )
        return TestClient(create_app(settings))


def journal(client: TestClient, **params) -> ForecastJournalList:
    response = client.get("/api/v1/forecast-journal", params={"limit": 100, **params})
    assert response.status_code == 200, response.text
    return ForecastJournalList.model_validate(response.json())


def ids(page: ForecastJournalList) -> list[str]:
    return [entry.card.id for entry in page.items]


def decide(client: TestClient, card_id: str) -> ForecastDecisionSummary:
    created = client.post(
        f"/api/v1/forecasts/{card_id}/decisions",
        json={
            "decision_code": "R2",
            "reason_code": "R2.1",
            "reason_text": "синтетическая проверка журнала",
            "verification_methods": ["source_records"],
            "idempotency_key": f"synthetic-{uuid4()}",
            "expected_revision": 0,
        },
    )
    assert created.status_code == 201, created.text
    return ForecastDecisionSummary.model_validate(created.json())


def stage(client: TestClient) -> tuple[str, bool, bool]:
    capabilities = Capabilities.model_validate(client.get("/api/v1/capabilities").json())
    ready = client.get("/health/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json()["stage"] == capabilities.stage
    return capabilities.stage, capabilities.inference_ready, ready.json()["inference_ready"]


def test_decisions_after_and_inside_the_data_window(dsn):
    scope = Scope(dsn)
    scope.insert(scope.days(0, DAYS))
    client = scope.client()
    # I7: observations without a publication are not a ready forecast.
    assert stage(client) == ("observations_only", False, False)
    first = scope.publish(DAYS, list_from=(FIRST + timedelta(days=DAYS - 10)).date())
    assert first.generation == 1
    assert stage(client) == ("forecast_published", True, True)

    before = journal(client)
    data_as_of = before.as_of
    assert data_as_of < datetime.now(MSK) - timedelta(days=BEHIND - 1)
    assert before.records_as_of is not None and before.records_as_of > data_as_of
    scored = [entry for entry in before.items if entry.card.status == "scored"]
    assert len(scored) >= 3, "a year of synthetic history gives scored cards"
    assert all(entry.decision is None and entry.check_result is None for entry in before.items)
    counts = client.get("/api/v1/forecast-journal/summary").json()
    quality = client.get("/api/v1/forecast-journal/quality").json()

    # Б-1: a decision and a check result saved now, months after the data, are in the entry.
    card_id = scored[-1].card.id
    decision = decide(client, card_id)
    assert decision.decided_at > data_as_of
    recorded = client.post(
        f"/api/v1/forecasts/{card_id}/check-result",
        json={
            "check_result": "no_violation",
            "result_at": decision.decided_at.isoformat(),
            "idempotency_key": f"synthetic-{uuid4()}",
            "expected_revision": 0,
        },
    )
    assert recorded.status_code == 201, recorded.text
    check = ForecastCheckResult.model_validate(recorded.json())
    after = journal(client)
    assert after.as_of == data_as_of
    assert after.records_as_of >= check.recorded_at > data_as_of
    entry = next(item for item in after.items if item.card.id == card_id)
    assert (entry.decision.decision_code, entry.decision.revision) == ("R2", 1)
    assert entry.check_result.check_result == "no_violation"
    assert all(item.decision is None for item in after.items if item.card.id != card_id)
    # The snapshot, its outcome and the metrics do not depend on the decision.
    old = next(item for item in before.items if item.card.id == card_id)
    assert (entry.card, entry.outcome, entry.list_state) == (old.card, old.outcome, old.list_state)
    assert client.get("/api/v1/forecast-journal/summary").json() == counts
    assert client.get("/api/v1/forecast-journal/quality").json() == quality
    # «Без решения» and «с решением» agree with the entries.
    assert card_id not in ids(journal(client, decision_state="none"))
    assert ids(journal(client, decision_state="any")) == [card_id]
    # The monthly report (and its XLSX) counts the decision of the card's month.
    month = entry.card.issued_at.astimezone(MSK).strftime("%Y-%m")
    report = MonthlyReport.model_validate(
        client.get("/api/v1/reports/monthly", params={"month": month}).json()
    )
    assert {row.decision_code: row.count for row in report.decisions}.get("R2") == 1
    # A view «as it was» before the decision was saved does not show it.
    earlier = journal(client, as_of=(decision.decided_at - timedelta(seconds=1)).isoformat())
    assert earlier.as_of == data_as_of
    assert earlier.records_as_of == decision.decided_at - timedelta(seconds=1)
    assert card_id in ids(earlier)
    assert all(item.decision is None and item.check_result is None for item in earlier.items)
    assert card_id in ids(journal(client, as_of=earlier.as_of.isoformat(), decision_state="none"))

    # A cursor pins the records bound: a decision between pages does not shift them.
    pending = ids(journal(client, decision_state="none"))
    page = journal(client, decision_state="none", limit=1)
    assert ids(page) == pending[:1] and "r" in page.next_cursor
    decide(client, pending[0])
    following = journal(client, decision_state="none", limit=1, cursor=page.next_cursor)
    assert ids(following) == pending[1:2]
    assert following.records_as_of == page.records_as_of
    assert pending[0] not in ids(journal(client, decision_state="none"))

    # Generation 2 ends tomorrow: the same decision now lies inside the data window.
    scope.insert(scope.days(DAYS, DAYS + BEHIND + 1, quiet=True))
    second = scope.publish(DAYS + BEHIND + 1)
    assert second.generation == 2
    assert stage(client) == ("forecast_published", True, True)
    inside = journal(client)
    assert inside.as_of > decision.decided_at
    entry = next(item for item in inside.items if item.card.id == card_id)
    assert entry.decision.decided_at == decision.decided_at
    assert entry.check_result.recorded_at == check.recorded_at
    assert {card_id, pending[0]} <= set(ids(journal(client, decision_state="any")))
    # A requested moment bounds both axes inside the data window.
    moment = decision.decided_at - timedelta(seconds=1)
    earlier = journal(client, as_of=moment.isoformat())
    assert earlier.as_of == earlier.records_as_of == moment
    assert card_id in ids(earlier)
    assert all(item.decision is None for item in earlier.items)
    later = journal(client, as_of=check.recorded_at.isoformat())
    entry = next(item for item in later.items if item.card.id == card_id)
    assert entry.decision is not None and entry.check_result is not None
