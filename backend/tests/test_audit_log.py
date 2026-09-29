"""Request audit policy without PostgreSQL (G1, ТЗ §11): a list replaces the sink.

The PostgreSQL path (rows in ``audit_events``, append-only, flush at shutdown) is in
``test_g1_import_audit_db.py``.
"""

from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from starlette.middleware import Middleware
from test_rbac_matrix import OPEN_CARD, csrf, login, make_client

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.api.audit_log import POLICY, Aggregator, AuditMiddleware, rule_for
from infra_pulse_backend.config import Settings
from infra_pulse_backend.storage.audit_pg import AuditEvent


def capture(app) -> list[AuditEvent]:
    """Swap the middleware's sink for a list before the first request builds the stack."""
    events: list[AuditEvent] = []
    for index, item in enumerate(app.user_middleware):
        if item.cls is AuditMiddleware:
            app.user_middleware[index] = Middleware(
                AuditMiddleware, **{**item.kwargs, "sink": events.extend}
            )
    return events


def test_policy_names_only_existing_routes_and_defaults_cover_new_ones():
    app = create_app(Settings(mode="fixture", _env_file=None))
    # app.routes holds included routers as wrappers; iterate the effective routes.
    routes = {
        (method, context.path)
        for context in iter_route_contexts(app.routes)
        for method in context.methods
    }
    assert set(POLICY) <= routes
    assert rule_for("GET", "/api/v1/some-new-view").action == "api.viewed"
    assert rule_for("POST", "/api/v1/some-new-action").action == "api.called"
    polls = {path for (method, path), rule in POLICY.items() if rule.kind == "poll"}
    assert {"/api/v1/forecast-state", "/api/v1/notifications"} <= polls


def test_views_are_rows_polls_and_repeats_are_counted():
    app = create_app(Settings(mode="fixture", db_dsn="postgresql://unused", _env_file=None))
    events = capture(app)
    with TestClient(app) as client:
        assert client.get(f"/api/v1/forecasts/{OPEN_CARD}").status_code == 200
        assert client.get(f"/api/v1/forecasts/{OPEN_CARD}").status_code == 200
        for _ in range(2):
            client.get("/api/v1/forecast-state")
        client.get("/health/live")
        exported = client.get("/api/v1/reports/journal.xlsx", params={"from": "2026-09-01"})
        rows = list(events)
    card = [event for event in rows if event.action == "forecast.card_viewed"]
    assert len(card) == 1
    assert (card[0].actor_id, card[0].actor_role, card[0].target_id) == (
        "local-operator",
        "dispatcher",
        OPEN_CARD,
    )
    assert card[0].client_address is None and "status" in card[0].details
    report = [event for event in rows if event.action == "report.exported"]
    assert len(report) == 1
    assert report[0].outcome == ("success" if exported.status_code < 400 else "failure")
    assert report[0].details["query"] == {"from": "2026-09-01"}
    # Shutdown drained the window: polls and the folded repeat as summaries.
    summaries = {
        (event.target_id, event.details["kind"]): event.details["count"]
        for event in events
        if event.action == "request.summary"
    }
    assert summaries == {
        ("/api/v1/forecast-state", "poll"): 2,
        ("/api/v1/forecasts/{forecast_id}", "repeat"): 1,
    }


def test_refused_requests_are_attributed_to_the_session():
    client, _store, _directory = make_client(db_dsn="postgresql://unused")
    events = capture(client.app)
    client.get("/api/v1/forecast-state")  # no session: nobody to attribute, no row
    assert login(client, "dispatcher").status_code == 200
    assert client.get("/api/v1/research-summary").status_code == 403
    decision = client.post(f"/api/v1/forecasts/{OPEN_CARD}/decisions", json={})
    assert decision.status_code == 403  # anti-CSRF header missing
    ok = client.post(f"/api/v1/forecasts/{OPEN_CARD}/decisions", json={}, headers=csrf(client))
    assert ok.status_code == 422
    denied = [event for event in events if event.action == "research.viewed"]
    assert [(e.outcome, e.actor_id, e.actor_role) for e in denied] == [
        ("denied", "dispatcher", None)
    ]
    # Only the access refusal is added; the domain route's 422 leaves no second event.
    rejected = [event for event in events if event.action == "request.rejected"]
    assert [(e.outcome, e.details["status"]) for e in rejected] == [("denied", 403)]
    assert all(event.actor_id == "dispatcher" for event in events)


def test_aggregator_window_and_fold():
    now = [0.0]
    aggregator = Aggregator(window_seconds=60, fold_seconds=30, clock=lambda: now[0])
    key = ("user", "GET", "/x", None, (), 2)
    assert aggregator.should_fold(key) is False
    now[0] = 10
    assert aggregator.should_fold(key) is True
    now[0] = 45
    assert aggregator.should_fold(key) is False  # 35 s after the last written row
    assert aggregator.due() == []
    now[0] = 61
    assert aggregator.due() == []  # nothing counted, nothing to write
