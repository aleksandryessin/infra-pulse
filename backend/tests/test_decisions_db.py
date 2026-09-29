"""PostgreSQL path of B3 (migration 0014): sessions, decisions, drafts and audit.

API-03/API-04/SEC-03: one POST writes decision + draft «не отправлен» + audit in one
transaction with the session author; a repeated idempotency key returns the stored
decision without new rows; a stale revision is 409; an audit failure rolls back the
decision; concurrent writers leave one revision; history rows are append-only.
«Кому и когда сообщено» (C0.1) is stored with the revision and its audit row; a
notification later than the decision is 422 and is also rejected by the table.
Needs ``INFRA_TEST_RECEIVED_DSN`` on a disposable database (``make check-db``).
"""

import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from app_role import app_dsn, owner_dsn
from fastapi.testclient import TestClient

from infra_pulse_backend.api import forecast_db
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.api.auth_deps import AuthRuntime
from infra_pulse_backend.auth.ldap import DirectoryUser, InvalidCredentials
from infra_pulse_backend.auth.sessions import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    NewSession,
    PgAuthStore,
    token_digest,
)
from infra_pulse_backend.auth.throttle import LoginThrottle
from infra_pulse_backend.config import Settings
from infra_pulse_backend.storage import decisions_pg
from infra_pulse_backend.storage.audit_pg import AuditEvent
from infra_pulse_core.contracts.auth import Me
from infra_pulse_core.contracts.forecast import (
    ForecastDecisionCreate,
    ForecastDecisionList,
    ForecastDecisionSummary,
)

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PASSWORD = "synthetic-password-1"
DECISIONS = "SELECT count(*) FROM forecast_decisions WHERE forecast_id = %s"
DRAFTS = "SELECT count(*) FROM work_order_drafts WHERE forecast_id = %s"
AUDIT = "SELECT count(*) FROM audit_events WHERE target_id = %s"
DISPATCHER = Me(
    subject_id="dispatcher-db",
    display_name="Диспетчер",
    roles=["dispatcher"],
    auth_source="dev_stub",
)


@pytest.fixture(scope="module")
def dsn() -> str:
    value = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not value:
        pytest.skip("local PostgreSQL integration DSN not provided")
    with psycopg.connect(value) as connection:
        for _ in range(2):  # the loader re-applies every file: must be idempotent
            for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                connection.execute(migration.read_text(encoding="utf-8"))
    return app_dsn(value)


def body(key: str | None = None, **overrides) -> dict:
    payload = {
        "decision_code": "R3",
        "reason_code": "R3.1",
        "reason_text": "устойчивая или повторная потеря связи",
        "verification_methods": ["source_records", "remote_poll"],
        "idempotency_key": key or f"synthetic-{uuid4()}",
        "expected_revision": 0,
        "draft_note": "осмотр фидеров",
        # C0.4: «Сообщено энергетику» (R3) needs recipient, time and a result deadline.
        "notified_to": "дежурный энергетик (синтетика)",
        "notified_at": "2026-09-01T10:00:00+03:00",
        "awaiting_result_until": "2099-01-01T00:00:00+03:00",
    }
    merged = payload | overrides
    if merged["decision_code"] != "R3":  # the result deadline belongs to R3 only (C0.4)
        merged.pop("awaiting_result_until", None)
    return merged


def count(dsn: str, sql: str, *params) -> int:
    with psycopg.connect(dsn) as connection:
        return connection.execute(sql, params).fetchone()[0]


class Directory:
    users = {"dispatcher-db": {"dispatcher"}, "analyst-db": {"analyst"}, "admin-db": {"admin"}}

    def authenticate(self, username: str, password: str) -> DirectoryUser:
        if username not in self.users or password != PASSWORD:
            raise InvalidCredentials
        return DirectoryUser(username, f"Тест {username}", frozenset(self.users[username]))


@pytest.fixture
def cards(monkeypatch) -> set[str]:
    """Published cards stand-in; the real forecast_card (B2) is in test_upload_publish_decide_db."""
    known: set[str] = set()

    def forecast_card(settings, forecast_id, *, as_of):
        return object() if forecast_id in known else None

    monkeypatch.setattr(forecast_db, "forecast_card", forecast_card)
    return known


def ldap_client(dsn: str, **settings) -> TestClient:
    settings = {"mode": "replay"} | settings
    app = create_app(Settings(auth_mode="ldap", db_dsn=dsn, _env_file=None, **settings))
    app.state.auth_runtime = AuthRuntime(
        store=PgAuthStore(dsn), directory=Directory(), throttle=LoginThrottle(5, 300)
    )
    return TestClient(app, base_url="https://testserver")


def signed_in(dsn: str, username: str, **settings) -> TestClient:
    client = ldap_client(dsn, **settings)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
        headers={CSRF_HEADER: "login", "X-Request-ID": f"req-login-{uuid4()}"},
    )
    assert response.status_code == 200, response.text
    client.headers[CSRF_HEADER] = client.cookies[CSRF_COOKIE]
    return client


def test_session_store_keeps_digests_and_audits_login_logout(dsn):
    store = PgAuthStore(dsn)
    new = NewSession.issue(f"user-{uuid4()}", "Пользователь", ["analyst"])
    request_id = f"req-{uuid4()}"
    session = store.open_session(
        new,
        ttl=timedelta(minutes=5),
        event=AuditEvent(
            action="auth.login",
            actor_id=new.subject_id,
            actor_role="analyst",
            target_kind="session",
            target_id=str(new.session_id),
            request_id=request_id,
        ),
    )
    assert store.find_session(new.token) == session
    assert session.me().session_expires_at == session.expires_at
    with psycopg.connect(dsn) as connection:
        stored = connection.execute(
            "SELECT token_sha256, csrf_sha256 FROM auth_sessions WHERE session_id = %s",
            (new.session_id,),
        ).fetchone()
        assert stored == (token_digest(new.token), token_digest(new.csrf_token))
        leaked = connection.execute(
            "SELECT count(*) FROM auth_sessions WHERE token_sha256 = %s OR csrf_sha256 = %s",
            (new.token, new.csrf_token),
        ).fetchone()[0]
        assert leaked == 0
        connection.execute(
            "UPDATE auth_sessions SET expires_at = created_at + interval '1 microsecond' "
            "WHERE session_id = %s",
            (new.session_id,),
        )
    assert store.find_session(new.token) is None
    logout = AuditEvent(
        action="auth.logout", actor_id=new.subject_id, target_kind="session", request_id="r" * 8
    )
    assert store.close_session(new.token, logout) is None
    assert count(dsn, "SELECT count(*) FROM audit_events WHERE request_id = %s", request_id) == 1


def test_login_logout_over_api_write_audit_with_request_id(dsn):
    client = ldap_client(dsn)
    request_id = f"req-login-{uuid4()}"
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "analyst-db", "password": PASSWORD},
        headers={CSRF_HEADER: "login", "X-Request-ID": request_id},
    )
    assert response.status_code == 200
    token = client.cookies[SESSION_COOKIE]
    assert client.get("/api/v1/auth/me").json()["subject_id"] == "analyst-db"
    wrong = client.post(
        "/api/v1/auth/login",
        json={"username": "analyst-db", "password": "wrong"},
        headers={CSRF_HEADER: "login"},
    )
    assert wrong.status_code == 401
    logout = client.post("/api/v1/auth/logout", headers={CSRF_HEADER: client.cookies[CSRF_COOKIE]})
    assert logout.status_code == 204
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            """SELECT action, outcome, actor_id, actor_role, target_kind, request_id,
                      occurred_at IS NOT NULL
               FROM audit_events
               WHERE actor_id = 'analyst-db' ORDER BY occurred_at DESC LIMIT 3""",
        ).fetchall()
        revoked = connection.execute(
            "SELECT revoked_at IS NOT NULL FROM auth_sessions WHERE token_sha256 = %s",
            (token_digest(token),),
        ).fetchone()[0]
    assert [row[:2] for row in rows] == [
        ("auth.logout", "success"),
        ("auth.login_failed", "failure"),
        ("auth.login", "success"),
    ]
    assert rows[2][2:] == ("analyst-db", "analyst", "session", request_id, True)
    assert revoked


def test_decision_draft_and_audit_in_one_post(dsn, cards):
    forecast_id = f"synthetic-card-{uuid4()}"
    cards.add(forecast_id)
    client = signed_in(dsn, "dispatcher-db")
    request_id = f"req-decide-{uuid4()}"
    payload = body()
    created = client.post(
        f"/api/v1/forecasts/{forecast_id}/decisions",
        json=payload,
        headers={"X-Request-ID": request_id},
    )
    assert created.status_code == 201, created.text
    decision = ForecastDecisionSummary.model_validate(created.json())
    assert (decision.revision, decision.simulated, decision.draft_status) == (1, False, "not_sent")
    assert (decision.actor_id, decision.actor_role) == ("dispatcher-db", "dispatcher")
    assert decision.verification_methods == ["source_records", "remote_poll"]
    with psycopg.connect(dsn) as connection:
        audit = connection.execute(
            """SELECT action, actor_id, actor_role, target_kind, target_id, details
               FROM audit_events WHERE request_id = %s ORDER BY action""",
            (request_id,),
        ).fetchall()
        draft = connection.execute(
            "SELECT status, note, created_by FROM work_order_drafts WHERE draft_id = %s",
            (decision.draft_id,),
        ).fetchone()
    assert [row[:5] for row in audit] == [
        ("decision.created", "dispatcher-db", "dispatcher", "forecast", forecast_id),
        ("draft.created", "dispatcher-db", "dispatcher", "work_order_draft", decision.draft_id),
    ]
    assert audit[0][5]["revision"] == 1
    assert draft == ("not_sent", "осмотр фидеров", "dispatcher-db")

    retried = client.post(f"/api/v1/forecasts/{forecast_id}/decisions", json=payload)
    assert retried.json() == created.json()
    reused = client.post(
        f"/api/v1/forecasts/{forecast_id}/decisions",
        json=payload | {"reason_text": "другой текст"},
    )
    assert (reused.status_code, reused.json()["detail"]) == (409, "idempotency_key_reused")
    stale = client.post(f"/api/v1/forecasts/{forecast_id}/decisions", json=body())
    assert (stale.status_code, stale.json()["detail"]) == (409, "decision_revision_conflict")
    second = client.post(
        f"/api/v1/forecasts/{forecast_id}/decisions",
        json=body(
            decision_code="R2",
            reason_code="R2.1",
            expected_revision=1,
            draft_note=None,
            verification_methods=["not_checked"],
        ),
    )
    assert second.status_code == 201
    assert second.json()["draft_id"] is None
    assert count(dsn, DECISIONS, forecast_id) == 2
    assert count(dsn, DRAFTS, forecast_id) == 1
    assert count(dsn, AUDIT, forecast_id) == 2

    # Another dispatcher after a reload reads both revisions with their authors.
    reader = signed_in(dsn, "analyst-db")
    listed = ForecastDecisionList.model_validate(
        reader.get(f"/api/v1/forecasts/{forecast_id}/decisions").json()
    )
    assert [(item.revision, item.decision_code, item.actor_id) for item in listed.items] == [
        (2, "R2", "dispatcher-db"),
        (1, "R3", "dispatcher-db"),
    ]
    denied = reader.post(f"/api/v1/forecasts/{forecast_id}/decisions", json=body())
    assert (denied.status_code, denied.json()["detail"]) == (403, "forbidden_role")
    assert reader.get("/api/v1/forecasts/unknown-card/decisions").status_code == 404
    with psycopg.connect(dsn) as connection:
        latest = decisions_pg.latest_decisions(connection, [forecast_id, "unknown-card"])
    assert list(latest) == [forecast_id] and latest[forecast_id].revision == 2


def test_notification_is_stored_with_decision_and_audit(dsn, cards):
    forecast_id = f"synthetic-card-{uuid4()}"
    cards.add(forecast_id)
    client = signed_in(dsn, "dispatcher-db")
    path = f"/api/v1/forecasts/{forecast_id}/decisions"
    recipient = "дежурный энергетик (синтетика)"
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    late = client.post(path, json=body(notified_to=recipient, notified_at=future))
    assert (late.status_code, late.json()["detail"]) == (422, "notified_after_decision")
    assert count(dsn, DECISIONS, forecast_id) == 0
    assert count(dsn, AUDIT, forecast_id) == 0

    notified_at = datetime.now(UTC) - timedelta(minutes=5)
    request_id = f"req-notified-{uuid4()}"
    payload = body(notified_to=recipient, notified_at=notified_at.isoformat())
    created = client.post(path, json=payload, headers={"X-Request-ID": request_id})
    assert created.status_code == 201, created.text
    decision = ForecastDecisionSummary.model_validate(created.json())
    assert (decision.notified_to, decision.notified_at) == (recipient, notified_at)
    assert decision.notified_at <= decision.decided_at
    assert client.post(path, json=payload).json() == created.json()
    listed = ForecastDecisionList.model_validate(client.get(path).json())
    assert [(item.notified_to, item.notified_at) for item in listed.items] == [
        (recipient, notified_at)
    ]
    with psycopg.connect(dsn) as connection:
        details = connection.execute(
            """SELECT details FROM audit_events
               WHERE request_id = %s AND action = 'decision.created'""",
            (request_id,),
        ).fetchone()[0]
    assert details["notified_to"] == recipient
    assert datetime.fromisoformat(details["notified_at"]) == notified_at

    # The table refuses a notification after the decision or without its recipient.
    for recipient_value, at in ((recipient, "now() + interval '1 hour'"), (None, "now()")):
        with pytest.raises(psycopg.errors.CheckViolation):
            with psycopg.connect(dsn) as connection:
                connection.execute(
                    f"""INSERT INTO forecast_decisions
                        (decision_id, forecast_id, revision, decision_code, reason_code,
                         reason_text, verification_methods, dictionary_version, actor_id,
                         actor_role, notified_to, notified_at, idempotency_key,
                         payload_sha256, request_id)
                        VALUES (%s, %s, 99, 'R2', 'R2.1', 'синтетика', ARRAY['not_checked'],
                                'v', 'dispatcher-db', 'dispatcher', %s, {at},
                                %s, %s, 'req-direct-sql')""",
                    (uuid4(), forecast_id, recipient_value, f"direct-{uuid4()}", "0" * 64),
                )


def test_audit_failure_rolls_back_decision_and_draft(dsn, cards, monkeypatch):
    forecast_id = f"synthetic-card-{uuid4()}"
    cards.add(forecast_id)
    client = signed_in(dsn, "admin-db")

    def broken(connection, event):
        if event.action == "draft.created":
            raise psycopg.errors.CheckViolation("synthetic audit failure")
        return original(connection, event)

    original = decisions_pg.record_audit_event
    monkeypatch.setattr(decisions_pg, "record_audit_event", broken)
    response = client.post(f"/api/v1/forecasts/{forecast_id}/decisions", json=body())
    assert (response.status_code, response.json()["detail"]) == (
        503,
        "decisions_storage_unavailable",
    )
    for table in ("forecast_decisions", "work_order_drafts"):
        assert count(dsn, f"SELECT count(*) FROM {table} WHERE forecast_id = %s", forecast_id) == 0
    assert count(dsn, AUDIT, forecast_id) == 0


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_decisions_leave_one_revision(dsn, same_key):
    forecast_id = f"synthetic-card-{uuid4()}"
    barrier = Barrier(2)
    shared = f"synthetic-{uuid4()}"

    def decide(index: int):
        request = ForecastDecisionCreate.model_validate(
            body(shared if same_key else f"synthetic-{index}-{uuid4()}")
        )
        barrier.wait()
        try:
            return decisions_pg.create_decision(
                dsn,
                forecast_id=forecast_id,
                request=request,
                actor=DISPATCHER,
                actor_role="dispatcher",
                request_id=f"req-{index}-{uuid4()}",
            )
        except decisions_pg.DecisionRevisionConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, range(2)))
    stored = [item for item in results if isinstance(item, ForecastDecisionSummary)]
    conflicts = [item for item in results if isinstance(item, Exception)]
    assert (len(stored), len(conflicts)) == ((2, 0) if same_key else (1, 1))
    assert {item.revision for item in stored} == {1}
    assert count(dsn, DECISIONS, forecast_id) == 1
    assert count(dsn, AUDIT + " AND action = 'decision.created'", forecast_id) == 1


def test_history_is_append_only(dsn):
    forecast_id = f"synthetic-card-{uuid4()}"
    decisions_pg.create_decision(
        dsn,
        forecast_id=forecast_id,
        request=ForecastDecisionCreate.model_validate(body()),
        actor=DISPATCHER,
        actor_role="dispatcher",
        request_id=f"req-{uuid4()}",
    )
    for statement in (
        "UPDATE audit_events SET actor_id = 'someone-else' WHERE target_id = %s",
        "DELETE FROM audit_events WHERE target_id = %s",
        "UPDATE forecast_decisions SET actor_id = 'someone-else' WHERE forecast_id = %s",
        "DELETE FROM forecast_decisions WHERE forecast_id = %s",
    ):
        # The runtime role has no UPDATE/DELETE here; the owner hits the trigger (0014).
        refused = "permission denied|append-only"
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match=refused):
            with psycopg.connect(dsn) as connection:
                connection.execute(statement, (forecast_id,))
    with pytest.raises(ValueError, match="reason code"):
        decisions_pg.create_decision(
            dsn,
            forecast_id=forecast_id,
            request=ForecastDecisionCreate.model_validate(body(reason_code="R1.1")),
            actor=DISPATCHER,
            actor_role="dispatcher",
            request_id="req-invalid",
        )


def test_decision_on_unpublished_card_is_not_stored(dsn):
    # No publication in the scope: 503 forecast_not_implemented (unknown card with a
    # publication: 404, test_upload_publish_decide_db).
    forecast_id = f"synthetic-unpublished-{uuid4()}"
    client = signed_in(dsn, "dispatcher-db")
    response = client.post(f"/api/v1/forecasts/{forecast_id}/decisions", json=body())
    assert response.status_code in (404, 503)
    assert response.json()["detail"] in ("forecast_not_found", "forecast_not_implemented")
    assert count(dsn, DECISIONS, forecast_id) == 0


def test_review_note_author_comes_from_session(dsn, tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "load_received_batch.py"
    spec = importlib.util.spec_from_file_location("load_received_batch_b3", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stream = f"synthetic-b3-note-{uuid4()}"
    path = tmp_path / "batch.json"
    record = {
        "source_record_id": "r1",
        "channel_id": "channel-1",
        "sensor_type": "Газовый датчик",
        "value_raw": "Обнаружен газ",
        "alarm": False,
        "event_at": "2026-09-24T10:00:00+03:00",
    }
    path.write_text(json.dumps({"batch_id": "b3", "records": [record]}), encoding="utf-8")
    # The legacy loader runs as the owner on the stand (received-watcher).
    assert module.load(path, dsn=owner_dsn(dsn), stream_id=stream)["rows"] == 1
    client = signed_in(
        dsn,
        "dispatcher-db",
        mode="received",
        received_stream_id=stream,
        enable_local_reviews=True,
    )
    queue = client.get("/api/v1/attention", params={"view": "all"}).json()
    uid = queue["items"][0]["message"]["row_uid"]
    response = client.post(
        f"/api/v1/attention/{uid}/reviews",
        json={
            "idempotency_key": str(uuid4()),
            "expected_revision": 0,
            "view_as_of": queue["as_of"],
            "displayed_received_watermark": queue["received_watermark"],
            "displayed_snapshot_id": stream,
            "displayed_policy_version": queue["policy_version"],
            "action_text": "Проверили запись",
            "result_text": "Физический исход неизвестен",
            "reason_text": "Синтетическая проверка автора",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["actor_id"] == "dispatcher-db"
