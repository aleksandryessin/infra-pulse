"""Audit of user requests: ТЗ §11 «журналирование всех действий пользователей» (G1).

Domain actions already write their own ``audit_events`` row in the same transaction
as the change (login, logout, decision and draft, upload, API batch, integration
tokens; import status changes by the worker). This ASGI middleware adds every other
request of an identified caller, through ``storage.audit_pg.record_audit_event``:

* **view / export** — one row per request: action from ``POLICY`` (e.g.
  ``forecast.card_viewed``, ``report.exported``), target kind and ID from the path,
  outcome by status (2xx/3xx success, 401/403 denied, other failure). The same view
  (actor, route, target, query, status class) repeated within ``fold_seconds`` —
  the UI's 30 s auto-refresh or the upload page's 2 s status poll — is counted into
  the summary below instead of a row per request;
* **poll** — timers of the UI and integrations (``/forecast-state``,
  ``/notifications``, ``/auth/me``, ``/capabilities``, batch status): never a row per
  request; counted per (actor, role, route) and written as one
  ``request.summary`` row per window (``kind``, ``count``, statuses, first and last
  time). The window is flushed by the first request after it ends and at shutdown;
  counts of an unfinished window are lost if the process is killed;
* **self** — routes that audit themselves in their own transaction (login, logout,
  decision, upload, API batch): the middleware never adds a second event for the same
  action; only an access refusal of an identified caller (403: role or anti-CSRF) is
  ``request.rejected``. Validation, conflict, limit and outage refusals of these routes
  leave no row, as designed by B1/B1x («refusals leave no import, file or audit row»);
* **skip** — ``/health/*`` (no user).

Requests without an identified caller (no session: 401, open routes) are not
attributed to anyone and are not written here; failed logins are audited by
``api/auth.py``. Rows carry the actor, acting role, route template, path IDs,
status, duration and the query string's keys and values (filters and periods, cut
to 64 characters); request bodies, cookies, tokens, headers and the client address
are never written.

The row is written after the response has been sent, so the answer does not wait for
it; if PostgreSQL is unavailable the loss is logged (``infra_pulse.audit``) and the
request itself is not failed. Without ``INFRA_DB_DSN`` (fixture and scaffold modes)
nothing is written.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import parse_qsl
from uuid import uuid4

import psycopg
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from infra_pulse_backend.api.auth_deps import _REQUEST_ID
from infra_pulse_backend.storage.audit_pg import AuditEvent, record_audit_event
from infra_pulse_core.contracts.auth import PERMISSIONS

log = logging.getLogger("infra_pulse.audit")

Kind = Literal["view", "export", "poll", "self", "action", "skip"]
DB_ROLES = frozenset({"dispatcher", "analyst", "admin", "integration"})


@dataclass(frozen=True)
class Rule:
    kind: Kind
    action: str = "api.viewed"
    target_kind: str = "route"
    # Path parameter that names the target; None: the route template is the target.
    target_param: str | None = None


def _view(action: str, target_kind: str, param: str | None = None) -> Rule:
    return Rule("view", action, target_kind, param)


POLL = Rule("poll", "request.summary")
SELF = Rule("self", "request.rejected")
SKIP = Rule("skip")

# Explicit policy per route (method, template). A route missing here falls back to
# DEFAULT_GET / DEFAULT_WRITE, so a new route is audited before it is classified.
POLICY: dict[tuple[str, str], Rule] = {
    ("GET", "/health/live"): SKIP,
    ("GET", "/health/ready"): SKIP,
    ("POST", "/api/v1/auth/login"): SELF,
    ("POST", "/api/v1/auth/logout"): SELF,
    ("GET", "/api/v1/auth/me"): POLL,
    ("GET", "/api/v1/capabilities"): POLL,
    ("GET", "/api/v1/forecast-state"): POLL,
    ("GET", "/api/v1/notifications"): POLL,
    ("GET", "/api/v1/observations/{import_id}"): POLL,
    ("GET", "/api/v1/risks"): _view("risk.list_viewed", "risk_list"),
    ("GET", "/api/v1/forecasts"): _view("forecast.list_viewed", "forecast_list"),
    ("GET", "/api/v1/forecasts/{forecast_id}"): _view(
        "forecast.card_viewed", "forecast_card", "forecast_id"
    ),
    ("GET", "/api/v1/forecasts/{forecast_id}/decisions"): _view(
        "decision.history_viewed", "forecast_card", "forecast_id"
    ),
    ("POST", "/api/v1/forecasts/{forecast_id}/decisions"): SELF,
    ("GET", "/api/v1/forecast-journal"): _view("journal.viewed", "forecast_journal"),
    ("GET", "/api/v1/forecast-journal/summary"): _view(
        "journal.summary_viewed", "forecast_journal"
    ),
    ("GET", "/api/v1/forecast-journal/quality"): _view(
        "research.quality_viewed", "forecast_journal"
    ),
    ("GET", "/api/v1/registries/recurring"): _view("registry.viewed", "registry"),
    ("GET", "/api/v1/schemes"): _view("scheme.list_viewed", "scheme"),
    ("GET", "/api/v1/schemes/{object_id}"): _view("scheme.viewed", "scheme", "object_id"),
    ("GET", "/api/v1/research-summary"): _view("research.viewed", "research_summary"),
    ("POST", "/api/v1/imports"): SELF,
    ("GET", "/api/v1/imports"): _view("import.list_viewed", "import_file"),
    ("GET", "/api/v1/imports/{import_id}"): _view(
        "import.report_viewed", "import_file", "import_id"
    ),
    ("POST", "/api/v1/observations"): SELF,
    ("GET", "/api/v1/attention"): _view("source_journal.viewed", "attention"),
    ("GET", "/api/v1/attention/source-alarms"): _view("source_journal.window_viewed", "attention"),
    ("GET", "/api/v1/attention/objects"): _view("source_journal.objects_viewed", "attention"),
    ("GET", "/api/v1/attention/channels"): _view("source_journal.channels_viewed", "attention"),
    ("GET", "/api/v1/attention/coverage/objects"): _view(
        "source_journal.coverage_viewed", "attention"
    ),
    ("GET", "/api/v1/attention/coverage/channels"): _view(
        "source_journal.coverage_viewed", "attention"
    ),
    ("GET", "/api/v1/review-journal"): _view("review_note.journal_viewed", "review_note"),
    ("GET", "/api/v1/attention/{row_uid}/reviews"): _view(
        "review_note.list_viewed", "observation", "row_uid"
    ),
    # Local notes keep their own replay_review_audit; the common journal gets a row too.
    ("POST", "/api/v1/attention/{row_uid}/reviews"): Rule(
        "action", "review_note.created", "observation", "row_uid"
    ),
    ("GET", "/api/v1/reports/monthly"): _view("report.viewed", "report"),
    ("GET", "/api/v1/reports/monthly.xlsx"): Rule("export", "report.exported", "report"),
    ("GET", "/api/v1/reports/journal.xlsx"): Rule("export", "report.exported", "report"),
}
DEFAULT_GET = Rule("view", "api.viewed", "route")
DEFAULT_WRITE = Rule("action", "api.called", "route")


def rule_for(method: str, path: str) -> Rule:
    found = POLICY.get((method, path))
    if found is not None:
        return found
    return DEFAULT_GET if method in ("GET", "HEAD") else DEFAULT_WRITE


def outcome_of(status: int) -> Literal["success", "denied", "failure"]:
    if status < 400:
        return "success"
    return "denied" if status in (401, 403) else "failure"


def _query(scope: Scope) -> dict[str, str]:
    raw = scope.get("query_string", b"").decode("latin-1")
    pairs = parse_qsl(raw, keep_blank_values=True)[:20]
    return {key[:64]: value[:64] for key, value in pairs}


@dataclass
class _Tally:
    count: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    first_at: datetime | None = None
    last_at: datetime | None = None
    last_request_id: str | None = None


@dataclass(frozen=True)
class _Key:
    kind: str  # "poll" or "repeat"
    actor_id: str
    actor_role: str | None
    method: str
    route: str
    target_id: str | None


class Aggregator:
    """Counts polls and folded repeats; returns summary events when a window ends."""

    def __init__(self, window_seconds: float, fold_seconds: float, clock=time.monotonic):
        self.window = window_seconds
        self.fold = fold_seconds
        self.clock = clock
        self.lock = threading.Lock()
        self.started = clock()
        self.tallies: dict[_Key, _Tally] = {}
        self.last_logged: dict[tuple, float] = {}

    def should_fold(self, key: tuple) -> bool:
        """True when the same view was written less than ``fold`` seconds ago."""
        now = self.clock()
        with self.lock:
            last = self.last_logged.get(key)
            if last is not None and now - last < self.fold:
                return True
            self.last_logged[key] = now
            if len(self.last_logged) > 50_000:  # bounded memory; oldest forgotten
                cutoff = now - self.fold
                self.last_logged = {k: v for k, v in self.last_logged.items() if v >= cutoff}
            return False

    def add(self, key: _Key, status: int, at: datetime, request_id: str) -> None:
        with self.lock:
            tally = self.tallies.setdefault(key, _Tally())
            tally.count += 1
            code = str(status)
            tally.statuses[code] = tally.statuses.get(code, 0) + 1
            tally.first_at = tally.first_at or at
            tally.last_at = at
            tally.last_request_id = request_id

    def due(self) -> list[AuditEvent]:
        if self.clock() - self.started < self.window:
            return []
        return self.drain()

    def drain(self) -> list[AuditEvent]:
        with self.lock:
            tallies, self.tallies = self.tallies, {}
            self.started = self.clock()
        return [self._event(key, tally) for key, tally in tallies.items()]

    def _event(self, key: _Key, tally: _Tally) -> AuditEvent:
        statuses = tally.statuses
        failed = sum(n for code, n in statuses.items() if int(code) >= 400)
        return AuditEvent(
            action="request.summary",
            actor_id=key.actor_id,
            actor_role=key.actor_role,
            target_kind="route",
            target_id=key.route[:256],
            request_id=(tally.last_request_id or uuid4().hex)[:128],
            outcome="success" if not failed else "failure",
            details={
                "kind": key.kind,
                "method": key.method,
                "target_id": key.target_id,
                "count": tally.count,
                "statuses": statuses,
                "first_at": tally.first_at.isoformat() if tally.first_at else None,
                "last_at": tally.last_at.isoformat() if tally.last_at else None,
                "window_seconds": self.window,
            },
        )


class PgAuditSink:
    """Writes events with ``record_audit_event``, one short transaction per call."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def write(self, events: Iterable[AuditEvent]) -> None:
        events = list(events)
        if not events:
            return
        with psycopg.connect(self.dsn, connect_timeout=3) as connection:
            for event in events:
                record_audit_event(connection, event)


Sink = Callable[[list[AuditEvent]], None]


class AuditMiddleware:
    """Pure ASGI middleware: the row is written after the response has been sent."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        sink: Sink | None,
        window_seconds: float = 900.0,
        fold_seconds: float = 300.0,
    ) -> None:
        self.app = app
        self.sink = sink
        self.aggregator = Aggregator(window_seconds, fold_seconds)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, self._lifespan_receive(receive), send)
            return
        if scope["type"] != "http" or self.sink is None:
            await self.app(scope, receive, send)
            return
        scope.setdefault("state", {})
        started = time.perf_counter()
        status = 500

        async def send_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_status)
        finally:
            events = self.events_for(scope, status, time.perf_counter() - started)
            events.extend(self.aggregator.due())
            if events:
                await self._write(events)

    def _lifespan_receive(self, receive: Receive) -> Receive:
        async def wrapped() -> Message:
            message = await receive()
            if message["type"] == "lifespan.shutdown" and self.sink is not None:
                await self._write(self.aggregator.drain())
            return message

        return wrapped

    async def _write(self, events: list[AuditEvent]) -> None:
        try:
            await run_in_threadpool(self.sink, events)
        except Exception as error:  # noqa: BLE001 - audit loss is logged, answer is sent
            log.warning("request audit not stored (%d rows): %s", len(events), error)

    def events_for(self, scope: Scope, status: int, seconds: float) -> list[AuditEvent]:
        state = scope.get("state") or {}
        actor = state.get("audit_actor")
        route = scope.get("route")
        if actor is None or route is None:
            return []
        method = scope.get("method", "GET")
        template = getattr(route, "path", scope.get("path", ""))
        rule = rule_for(method, template)
        # Domain-audited routes: never a second event for the same action; only an
        # access refusal (role, anti-CSRF) is added. Validation, conflict and outage
        # refusals leave no row there, as the domain designed it (B1/B1x).
        if rule.kind == "skip" or (rule.kind == "self" and status not in (401, 403)):
            return []
        role = _acting_role(actor, state.get("audit_permission"), status)
        path_params = scope.get("path_params") or {}
        target_id = str(path_params[rule.target_param])[:256] if rule.target_param else None
        request_id = str(state.get("request_id") or _header_request_id(scope))[:128]
        at = datetime.now(UTC)
        if rule.kind == "poll":
            key = _Key("poll", actor.subject_id, role, method, template, target_id)
            self.aggregator.add(key, status, at, request_id)
            return []
        query = _query(scope)
        if rule.kind in ("view", "export"):
            fold_key = (
                actor.subject_id,
                method,
                template,
                target_id,
                tuple(sorted(query.items())),
                status // 100,
            )
            if self.aggregator.should_fold(fold_key):
                key = _Key("repeat", actor.subject_id, role, method, template, target_id)
                self.aggregator.add(key, status, at, request_id)
                return []
        details: dict[str, object] = {
            "method": method,
            "route": template,
            "status": status,
            "duration_ms": round(seconds * 1000, 1),
        }
        if query:
            details["query"] = query
        # No client address: only the actor identifies a person in these rows.
        return [
            AuditEvent(
                action=rule.action,
                actor_id=actor.subject_id,
                actor_role=role,
                target_kind=rule.target_kind,
                target_id=target_id if rule.target_param else template[:256],
                request_id=request_id,
                outcome=outcome_of(status),
                details=details,
            )
        ]


def _header_request_id(scope: Scope) -> str:
    """The proxy's ``X-Request-ID`` when well-formed (same rule as ``auth_deps``)."""
    for name, value in scope.get("headers") or ():
        if name == b"x-request-id":
            text = value.decode("latin-1")
            if _REQUEST_ID.fullmatch(text):
                return text
    return uuid4().hex


def _acting_role(actor, permission: str | None, status: int) -> str | None:
    """The role that granted the permission; None when it was refused or unknown."""
    roles = [role for role in actor.roles if role in DB_ROLES]
    if permission is not None:
        allowed = PERMISSIONS.get(permission, frozenset())
        granted = [role for role in roles if role in allowed]
        return granted[0] if granted else None
    return roles[0] if roles else None


__all__ = [
    "POLICY",
    "Aggregator",
    "AuditMiddleware",
    "PgAuditSink",
    "Rule",
    "outcome_of",
    "rule_for",
]
