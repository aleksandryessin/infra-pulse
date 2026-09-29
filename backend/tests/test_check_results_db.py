"""PostgreSQL path of C0.4 (migration 0019): decision deadlines and check results.

R3 «Сообщено энергетику» stores recipient, time and ``awaiting_result_until``; R1 «Под
наблюдением» stores ``watch_until``; a deadline not after the decision is 422. A check
result is a new audited revision; a repeated key returns it, a stale revision is 409,
``event_cause`` needs a card whose event was registered, rows are append-only.
Needs ``INFRA_TEST_RECEIVED_DSN`` on a disposable database (``make check-db``).
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from app_role import app_dsn
from fastapi.testclient import TestClient

from infra_pulse_backend.api import forecast_db
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.api.auth_deps import AuthRuntime
from infra_pulse_backend.auth.ldap import DirectoryUser, InvalidCredentials
from infra_pulse_backend.auth.sessions import CSRF_COOKIE, CSRF_HEADER, PgAuthStore
from infra_pulse_backend.auth.throttle import LoginThrottle
from infra_pulse_backend.config import Settings
from infra_pulse_backend.storage import check_results_pg

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PASSWORD = "synthetic-password-1"
MSK = timezone(timedelta(hours=3))


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def dsn() -> str:
    value = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not value:
        pytest.skip("local PostgreSQL integration DSN not provided")
    with psycopg.connect(value) as connection:
        for _ in range(2):  # every migration, 0001–0019, is applied twice: idempotent
            for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                connection.execute(migration.read_text(encoding="utf-8"))
    return app_dsn(value)


class Directory:
    users = {"dispatcher-c04": {"dispatcher"}}

    def authenticate(self, username: str, password: str) -> DirectoryUser:
        if username not in self.users or password != PASSWORD:
            raise InvalidCredentials
        return DirectoryUser(username, f"Тест {username}", frozenset(self.users[username]))


@pytest.fixture
def cards(monkeypatch) -> dict[str, str]:
    """Published cards stand-in: card ID -> list state (released = event registered)."""
    known: dict[str, str] = {}

    def forecast_card(settings, forecast_id, *, as_of):
        state = known.get(forecast_id)
        return None if state is None else SimpleNamespace(list_state=state)

    monkeypatch.setattr(forecast_db, "forecast_card", forecast_card)
    return known


def signed_in(dsn: str) -> TestClient:
    app = create_app(Settings(mode="replay", auth_mode="ldap", db_dsn=dsn, _env_file=None))
    app.state.auth_runtime = AuthRuntime(
        store=PgAuthStore(dsn), directory=Directory(), throttle=LoginThrottle(5, 300)
    )
    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "dispatcher-c04", "password": PASSWORD},
        headers={CSRF_HEADER: "login"},
    )
    assert response.status_code == 200, response.text
    client.headers[CSRF_HEADER] = client.cookies[CSRF_COOKIE]
    return client


def decision(**overrides) -> dict:
    return {
        "decision_code": "R3",
        "reason_code": "R3.1",
        "reason_text": "устойчивая или повторная потеря связи",
        "verification_methods": ["source_records"],
        "idempotency_key": f"synthetic-{uuid4()}",
        "expected_revision": 0,
        "notified_to": "дежурный энергетик (синтетика)",
        "notified_at": "2026-09-01T10:00:00+03:00",
        "awaiting_result_until": "2099-01-01T00:00:00+03:00",
    } | overrides


def check(**overrides) -> dict:
    return {
        "check_result": "fixed",
        "found": ["breaker"],
        "result_at": "2026-09-01T12:00:00+03:00",
        "comment": "синтетика",
        "idempotency_key": f"synthetic-{uuid4()}",
        "expected_revision": 0,
    } | overrides


def test_decision_deadlines_are_stored_and_checked(dsn, cards):
    card = f"synthetic-c04-{uuid4()}"
    cards[card] = "open"
    client = signed_in(dsn)
    created = client.post(f"/api/v1/forecasts/{card}/decisions", json=decision())
    assert created.status_code == 201, created.text
    assert moment(created.json()["awaiting_result_until"]) == datetime(2099, 1, 1, tzinfo=MSK)
    watch = client.post(
        f"/api/v1/forecasts/{card}/decisions",
        json=decision(
            decision_code="R1",
            reason_code="R1.1",
            notified_to=None,
            notified_at=None,
            awaiting_result_until=None,
            watch_until="2099-01-02T00:00:00+03:00",
            expected_revision=1,
        ),
    )
    assert watch.status_code == 201, watch.text
    early = client.post(
        f"/api/v1/forecasts/{card}/decisions",
        json=decision(awaiting_result_until="2026-09-01T11:00:00+03:00", expected_revision=2),
    )
    assert (early.status_code, early.json()["detail"]) == (422, "deadline_before_decision")
    listed = client.get(f"/api/v1/forecasts/{card}/decisions").json()["items"]
    assert [item["decision_code"] for item in listed] == ["R1", "R3"]
    assert moment(listed[0]["watch_until"]) == datetime(2099, 1, 2, tzinfo=MSK)
    with psycopg.connect(dsn) as connection:
        details = connection.execute(
            """SELECT details->>'awaiting_result_until' FROM audit_events
               WHERE target_id = %s AND action = 'decision.created'
               ORDER BY occurred_at LIMIT 1""",
            (card,),
        ).fetchone()[0]
    assert moment(details) == datetime(2099, 1, 1, tzinfo=MSK)


def test_check_result_revisions_audit_and_rules(dsn, cards):
    card = f"synthetic-c04-{uuid4()}"
    cards[card] = "released"
    client = signed_in(dsn)
    body = check(event_cause="protection_trip")
    first = client.post(f"/api/v1/forecasts/{card}/check-result", json=body)
    assert first.status_code == 201, first.text
    assert (first.json()["revision"], first.json()["found_confirmed_by_customer"]) == (1, False)
    again = client.post(f"/api/v1/forecasts/{card}/check-result", json=body)
    assert again.json() == first.json()
    reused = client.post(
        f"/api/v1/forecasts/{card}/check-result", json=body | {"comment": "другой текст"}
    )
    assert (reused.status_code, reused.json()["detail"]) == (409, "idempotency_key_reused")
    stale = client.post(f"/api/v1/forecasts/{card}/check-result", json=check())
    assert (stale.status_code, stale.json()["detail"]) == (409, "check_result_revision_conflict")
    future = client.post(
        f"/api/v1/forecasts/{card}/check-result",
        json=check(result_at="2099-01-01T00:00:00+03:00", expected_revision=1),
    )
    assert (future.status_code, future.json()["detail"]) == (422, "result_after_record")
    second = client.post(
        f"/api/v1/forecasts/{card}/check-result",
        json=check(check_result="no_violation", found=[], expected_revision=1),
    )
    assert second.status_code == 201 and second.json()["revision"] == 2
    open_card = f"synthetic-c04-{uuid4()}"
    cards[open_card] = "open"
    cause = client.post(
        f"/api/v1/forecasts/{open_card}/check-result", json=check(event_cause="unknown")
    )
    assert (cause.status_code, cause.json()["detail"]) == (422, "event_cause_requires_event")
    listed = client.get(f"/api/v1/forecasts/{card}/check-results").json()["items"]
    assert [item["revision"] for item in listed] == [2, 1]
    with psycopg.connect(dsn) as connection:
        latest = check_results_pg.latest_check_results(connection, [card])
        assert latest[card].check_result == "no_violation"
        audits = connection.execute(
            """SELECT count(*) FROM audit_events
               WHERE target_id = %s AND action = 'check_result.created'""",
            (card,),
        ).fetchone()[0]
        assert audits == 2
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(dsn) as connection:
            connection.execute("DELETE FROM forecast_check_results WHERE forecast_id = %s", (card,))
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(dsn) as connection:
            connection.execute(
                """INSERT INTO forecast_check_results
                   (check_result_id, forecast_id, revision, check_result, found, result_at,
                    actor_id, actor_role, idempotency_key, payload_sha256, request_id)
                   VALUES (%s, %s, 9, 'not_done', ARRAY['cable'], now(), 'a', 'dispatcher',
                           'synthetic-key-x', %s, 'req-x')""",
                (uuid4(), card, "0" * 64),
            )
