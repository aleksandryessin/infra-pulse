"""Server-side role matrix over every route of the app, and the directory login flow.

B3 criteria 1–2 (SEC-02, SEC-03): every ``app.routes`` entry has an expected permission;
no session is 401, a role outside ``PERMISSIONS`` is 403; ``/health/*`` is open and the
API schema is closed in ldap mode; ``INFRA_PUBLIC_DOCS`` opens only the documentation
(``/api/docs``, ``/api/openapi.json``), and the schema's ``security`` follows the matrix.
Login sets HttpOnly/Secure/SameSite=Lax cookies with an expiry, POST needs the anti-CSRF
header, failed logins are throttled and an unavailable directory is 503. An in-memory
store and a fake directory replace PostgreSQL and lldap here; the PostgreSQL store is
tested in ``test_decisions_db.py`` and the LDAP adapter in ``test_auth_ldap.py``.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from http.cookies import SimpleCookie

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.api.auth_deps import AuthRuntime
from infra_pulse_backend.api.docs import DOCS_PATHS
from infra_pulse_backend.auth.ldap import DirectoryUnavailable, DirectoryUser, InvalidCredentials
from infra_pulse_backend.auth.sessions import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthStorageUnavailable,
    NewSession,
    Session,
    token_digest,
)
from infra_pulse_backend.auth.throttle import LoginThrottle
from infra_pulse_backend.config import Settings
from infra_pulse_backend.storage.audit_pg import AuditEvent
from infra_pulse_core.contracts.auth import PERMISSIONS, Me

MSK = timezone(timedelta(hours=3))
OPEN = "open"
SESSION = "session"  # any role with a live session
ROLES = ("dispatcher", "analyst", "admin")
PASSWORD = "synthetic-password-1"
OPEN_CARD = "synthetic-feeders-14d-011"
# Served with their permission but kept out of the public schema: the starter per-channel
# risks (fixture only; the product forecast is /api/v1/forecasts, audit I7 of 29.09.2026).
UNDOCUMENTED = {("GET", "/api/v1/risks")}

# Every route of the app. A new route fails test_every_route_is_classified until it
# is added here with its permission. Permissions themselves come from the contract
# (PERMISSIONS); «Исследование» and quality ratios need "research" (C0.1).
EXPECTED: dict[tuple[str, str], str] = {
    ("GET", "/health/live"): OPEN,
    ("GET", "/health/ready"): OPEN,
    ("POST", "/api/v1/auth/login"): OPEN,
    ("POST", "/api/v1/auth/logout"): OPEN,
    ("GET", "/api/v1/auth/me"): SESSION,
    ("GET", "/api/v1/capabilities"): "read",
    ("GET", "/api/v1/risks"): "read",
    ("GET", "/api/v1/forecasts"): "read",
    ("GET", "/api/v1/forecasts/{forecast_id}"): "read",
    ("GET", "/api/v1/forecast-journal"): "read",
    ("GET", "/api/v1/forecast-journal/summary"): "read",
    ("GET", "/api/v1/forecast-journal/quality"): "research",
    ("GET", "/api/v1/forecast-state"): "read",
    ("GET", "/api/v1/registries/recurring"): "read",
    ("GET", "/api/v1/schemes"): "read",
    ("GET", "/api/v1/schemes/{object_id}"): "read",
    ("GET", "/api/v1/forecasts/{forecast_id}/decisions"): "read",
    ("POST", "/api/v1/forecasts/{forecast_id}/decisions"): "decide",
    # C0.4: check result after a decision.
    ("GET", "/api/v1/forecasts/{forecast_id}/check-results"): "read",
    ("POST", "/api/v1/forecasts/{forecast_id}/check-result"): "decide",
    ("POST", "/api/v1/imports"): "import",
    ("GET", "/api/v1/imports"): "import",
    ("GET", "/api/v1/imports/{import_id}"): "import",
    ("GET", "/api/v1/research-summary"): "research",
    ("GET", "/api/v1/attention"): "read",
    ("GET", "/api/v1/attention/source-alarms"): "read",
    ("GET", "/api/v1/attention/objects"): "read",
    ("GET", "/api/v1/attention/channels"): "read",
    ("GET", "/api/v1/attention/coverage/objects"): "read",
    ("GET", "/api/v1/attention/coverage/channels"): "read",
    ("GET", "/api/v1/review-journal"): "read",
    ("GET", "/api/v1/attention/{row_uid}/reviews"): "read",
    ("POST", "/api/v1/attention/{row_uid}/reviews"): "decide",
    # C0.2: streaming ingest (integration token or admin), notifications, reports.
    ("POST", "/api/v1/observations"): "ingest",
    ("GET", "/api/v1/observations/{import_id}"): "ingest",
    ("GET", "/api/v1/notifications"): "read",
    ("GET", "/api/v1/reports/monthly"): "report",
    ("GET", "/api/v1/reports/monthly.xlsx"): "report",
    ("GET", "/api/v1/reports/journal.xlsx"): "report",
}
PATH_VALUES = {
    "forecast_id": OPEN_CARD,
    "object_id": "synthetic-object-19",
    "import_id": "synthetic-import-001",
    "row_uid": "synthetic-row",
}
DECISION = {
    "decision_code": "R3",
    "reason_code": "R3.1",
    "reason_text": "устойчивая или повторная потеря связи",
    "verification_methods": ["remote_poll"],
    "idempotency_key": "synthetic-key-rbac-1",
    "expected_revision": 1,
    "draft_note": "осмотр фидеров",
    "notified_to": "дежурный энергетик (синтетика)",
    "notified_at": "2026-09-24T09:00:00+03:00",
    "awaiting_result_until": "2026-09-26T17:00:00+03:00",
}
CHECK_RESULT = {
    "check_result": "no_violation",
    "result_at": "2026-09-24T09:30:00+03:00",
    "idempotency_key": "synthetic-key-rbac-check-1",
    "expected_revision": 1,
}
BODIES = {
    ("POST", "/api/v1/forecasts/{forecast_id}/decisions"): {"json": DECISION},
    ("POST", "/api/v1/forecasts/{forecast_id}/check-result"): {"json": CHECK_RESULT},
    ("POST", "/api/v1/imports"): {"files": {"file": ("x.csv", b"a,b\n", "text/csv")}},
    ("POST", "/api/v1/attention/{row_uid}/reviews"): {"json": {}},
    ("POST", "/api/v1/observations"): {
        "json": {
            "batch_id": "synthetic-rbac-1",
            "records": [
                {
                    "event_id": "1",
                    "channel_id": "2",
                    "event_at": "2026-09-24T08:00:00+03:00",
                    "alarm": False,
                    "value": "28",
                }
            ],
        }
    },
    ("GET", "/api/v1/notifications"): {"params": {"since": "2026-09-24T00:00:00+03:00"}},
    ("GET", "/api/v1/attention/source-alarms"): {
        "params": {"event_from": "2026-09-23T09:55:00+03:00"}
    },
    ("GET", "/api/v1/reports/monthly"): {"params": {"month": "2026-09"}},
    ("GET", "/api/v1/reports/monthly.xlsx"): {"params": {"month": "2026-09"}},
    ("GET", "/api/v1/reports/journal.xlsx"): {
        "params": {"issued_from": "2026-09-01", "issued_to": "2026-09-30"}
    },
}


class MemoryAuthStore:
    """AuthStore stand-in with the same expiry/revocation rules as PgAuthStore."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.events: list[AuditEvent] = []
        self.available = True

    def _check(self) -> None:
        if not self.available:
            raise AuthStorageUnavailable("memory store switched off")

    def open_session(self, new, *, ttl, event, replaced_token=None) -> Session:
        self._check()
        if replaced_token and token_digest(replaced_token) in self.sessions:
            self.sessions[token_digest(replaced_token)]["revoked"] = True
        session = Session(
            session_id=new.session_id,
            subject_id=new.subject_id,
            display_name=new.display_name,
            roles=new.roles,
            # Like PostgreSQL with TimeZone=Europe/Moscow: aware, but not UTC.
            expires_at=(datetime.now(UTC) + ttl).astimezone(MSK),
            csrf_sha256=token_digest(new.csrf_token),
        )
        self.sessions[token_digest(new.token)] = {"session": session, "revoked": False}
        self.events.append(event)
        return session

    def find_session(self, token: str) -> Session | None:
        self._check()
        record = self.sessions.get(token_digest(token))
        if record is None or record["revoked"]:
            return None
        session = record["session"]
        return session if session.expires_at > datetime.now(UTC) else None

    def close_session(self, token: str, event: AuditEvent) -> Session | None:
        session = self.find_session(token)
        if session is not None:
            self.sessions[token_digest(token)]["revoked"] = True
            self.events.append(event)
        return session

    def record(self, event: AuditEvent) -> None:
        self._check()
        self.events.append(event)

    def actions(self) -> list[str]:
        return [event.action for event in self.events]


class FakeDirectory:
    def __init__(self, users: dict[str, set[str]]) -> None:
        self.users = users
        self.available = True
        self.calls = 0

    def authenticate(self, username: str, password: str) -> DirectoryUser:
        self.calls += 1
        if not self.available:
            raise DirectoryUnavailable("fake directory switched off")
        if username not in self.users or password != PASSWORD:
            raise InvalidCredentials
        return DirectoryUser(username, f"Тест {username}", frozenset(self.users[username]))


USERS = {
    "dispatcher": {"dispatcher"},
    "analyst": {"analyst"},
    "admin": {"admin"},
    "shift-lead": {"dispatcher", "analyst", "lldap_password_manager"},
    "outsider": {"lldap_strict_readonly"},
}


def make_client(
    *, max_failures: int = 5, directory: FakeDirectory | None = None, **settings
) -> tuple[TestClient, MemoryAuthStore, FakeDirectory]:
    app = create_app(Settings(mode="fixture", auth_mode="ldap", _env_file=None, **settings))
    store = MemoryAuthStore()
    directory = directory or FakeDirectory(USERS)
    app.state.auth_runtime = AuthRuntime(
        store=store, directory=directory, throttle=LoginThrottle(max_failures, 300)
    )
    # https: the Secure cookies are sent back only over TLS.
    return TestClient(app, base_url="https://testserver"), store, directory


def login(client: TestClient, username: str, password: str = PASSWORD, **headers):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers={CSRF_HEADER: "login", **headers},
    )


def csrf(client: TestClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies[CSRF_COOKIE]}


def call(client: TestClient, method: str, template: str, *, with_csrf: bool = True):
    kwargs = dict(BODIES.get((method, template), {}))
    if method != "GET" and with_csrf and CSRF_COOKIE in client.cookies:
        kwargs["headers"] = csrf(client)
    return client.request(method, template.format(**PATH_VALUES), **kwargs)


@pytest.fixture(scope="module")
def clients() -> dict[str | None, TestClient]:
    shared = {}
    for role in ROLES:
        client, _, _ = make_client()
        assert login(client, role).status_code == 200
        shared[role] = client
    shared[None], _, _ = make_client()
    return shared


def test_every_route_is_classified():
    app = create_app(Settings(mode="fixture", auth_mode="ldap", _env_file=None))
    # app.routes holds included routers as wrappers; iterate the effective routes.
    contexts = list(iter_route_contexts(app.routes))
    routes = {(method, context.path) for context in contexts for method in context.methods}
    assert routes == set(EXPECTED)
    # ldap mode serves no schema or Swagger routes at all.
    assert all(isinstance(context.route, APIRoute) for context in contexts)


@pytest.mark.parametrize("key", sorted(EXPECTED), ids=lambda key: f"{key[0]} {key[1]}")
def test_role_matrix(clients, key):
    method, template = key
    rule = EXPECTED[key]
    anonymous = call(clients[None], method, template)
    if rule == OPEN:
        assert anonymous.status_code not in (401, 403), anonymous.text
        return
    assert (anonymous.status_code, anonymous.json()["detail"]) == (401, "not_authenticated")
    for role in ROLES:
        response = call(clients[role], method, template)
        if rule == SESSION or role in PERMISSIONS[rule]:
            assert response.status_code not in (401, 403), (role, response.text)
        else:
            assert (response.status_code, response.json()["detail"]) == (403, "forbidden_role"), (
                role
            )


def test_matrix_rows_follow_contract_permissions():
    assert PERMISSIONS["decide"] == {"dispatcher", "admin"}
    assert PERMISSIONS["import"] == {"admin"}
    assert PERMISSIONS["read"] == set(ROLES)
    assert PERMISSIONS["research"] == {"analyst", "admin"}
    assert {rule for rule in EXPECTED.values()} <= {OPEN, SESSION, *PERMISSIONS}


def test_health_is_open_and_schema_closed_in_ldap_mode():
    client, _, _ = make_client()
    assert client.get("/health/live").json()["status"] == "alive"
    root = ("/docs", "/redoc", "/openapi.json", "/api/redoc")
    for path in (*root, *sorted(DOCS_PATHS)):
        assert client.get(path).status_code == 404, path
    documented, _, _ = make_client(public_docs=True)
    assert documented.get("/api/openapi.json").status_code == 200
    assert documented.get("/api/docs").status_code == 200
    local = TestClient(create_app(Settings(mode="fixture", _env_file=None)))
    assert local.get("/api/docs").status_code == 200
    assert local.get("/api/openapi.json").status_code == 200
    for path in root:
        assert documented.get(path).status_code == 404, path
        assert local.get(path).status_code == 404, path


def test_public_docs_open_only_the_documentation():
    """INFRA_PUBLIC_DOCS adds anonymous GET routes of the documentation and nothing else."""
    client, _, _ = make_client(public_docs=True)
    contexts = list(iter_route_contexts(client.app.routes))
    routes = {(method, context.path) for context in contexts for method in context.methods}
    added = routes - set(EXPECTED)
    assert {path for _, path in added} == DOCS_PATHS
    assert {method for method, _ in added} <= {"GET", "HEAD"}
    for _, path in sorted(added):
        assert client.get(path).status_code == 200, path
    for (method, template), rule in EXPECTED.items():
        if rule == OPEN:
            continue
        response = call(client, method, template)
        assert (response.status_code, response.json()["detail"]) == (401, "not_authenticated"), (
            method,
            template,
        )
    schema = client.get("/api/openapi.json").json()
    documented = {(method.upper(), path) for path, ops in schema["paths"].items() for method in ops}
    assert documented == set(EXPECTED) - UNDOCUMENTED


def test_schema_security_follows_the_matrix():
    """Every protected operation names its credentials, so «Authorize» in Swagger works."""
    schema = create_app(
        Settings(mode="fixture", auth_mode="ldap", public_docs=True, _env_file=None)
    ).openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["integrationToken"] | {"description": ""} == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "ipk_...",
        "description": "",
    }
    assert (schemes["sessionCookie"]["in"], schemes["sessionCookie"]["name"]) == (
        "cookie",
        SESSION_COOKIE,
    )
    assert (schemes["csrfToken"]["in"], schemes["csrfToken"]["name"]) == ("header", CSRF_HEADER)
    for (method, path), rule in EXPECTED.items():
        if (method, path) in UNDOCUMENTED:
            assert path not in schema["paths"], path
            continue
        security = schema["paths"][path][method.lower()].get("security")
        session = {"sessionCookie": []} | ({"csrfToken": []} if method == "POST" else {})
        if rule == OPEN:
            expected = None
        elif rule == "ingest":
            expected = [{"integrationToken": []}, session]
        else:
            expected = [session]
        assert security == expected, (method, path)


def cookie_attributes(response, name: str) -> dict[str, str]:
    for header in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        if name in cookie:
            morsel = cookie[name]
            flags = {key: str(morsel[key]) for key in morsel.keys() if morsel[key]}
            flags["value"] = morsel.value
            return flags
    raise AssertionError(f"cookie {name} not set")


def test_login_sets_hardened_cookies_and_session_identity():
    client, store, _ = make_client(session_ttl_minutes=60)
    response = login(client, "shift-lead", **{"X-Request-ID": "req-proxy-000001"})
    assert response.status_code == 200
    me = Me.model_validate(response.json())
    assert (me.subject_id, me.auth_source, me.roles) == (
        "shift-lead",
        "ldap",
        ["dispatcher", "analyst"],
    )
    session = cookie_attributes(response, SESSION_COOKIE)
    assert session["httponly"] and session["secure"] and session["path"] == "/"
    assert session["samesite"].lower() == "lax"
    assert 3500 <= int(session["max-age"]) <= 3600
    token = cookie_attributes(response, CSRF_COOKIE)
    assert "httponly" not in token and token["secure"] and token["samesite"].lower() == "lax"
    assert session["value"] != token["value"]
    current = Me.model_validate(client.get("/api/v1/auth/me").json())
    assert current.session_expires_at == me.session_expires_at
    assert me.session_expires_at - datetime.now(UTC) <= timedelta(minutes=60)
    [event] = store.events
    assert (event.action, event.actor_id, event.actor_role, event.request_id) == (
        "auth.login",
        "shift-lead",
        "dispatcher",
        "req-proxy-000001",
    )
    assert event.target_id == str(next(iter(store.sessions.values()))["session"].session_id)
    assert token["value"] not in repr(store.events) and session["value"] not in repr(store.events)


def test_post_requires_session_bound_csrf_header():
    client, _, _ = make_client()
    assert login(client, "dispatcher").status_code == 200
    path = f"/api/v1/forecasts/{OPEN_CARD}/decisions"
    missing = client.post(path, json=DECISION)
    assert (missing.status_code, missing.json()["detail"]) == (403, "csrf_failed")
    forged = client.post(path, json=DECISION, headers={CSRF_HEADER: "forged-token-value"})
    assert (forged.status_code, forged.json()["detail"]) == (403, "csrf_failed")
    created = client.post(path, json=DECISION, headers=csrf(client))
    assert created.status_code == 201
    author = (created.json()["actor_id"], created.json()["actor_role"])
    assert author == ("dispatcher", "dispatcher")
    spoofed = client.post(path, json=DECISION | {"actor_id": "admin"}, headers=csrf(client))
    assert spoofed.status_code == 422
    unauthenticated_login = client.post(
        "/api/v1/auth/login", json={"username": "dispatcher", "password": PASSWORD}
    )
    assert (unauthenticated_login.status_code, unauthenticated_login.json()["detail"]) == (
        403,
        "csrf_header_required",
    )


def test_reason_code_must_belong_to_decision():
    client, _, _ = make_client()
    assert login(client, "admin").status_code == 200
    response = client.post(
        f"/api/v1/forecasts/{OPEN_CARD}/decisions",
        json=DECISION | {"reason_code": "R2.1"},
        headers=csrf(client),
    )
    assert (response.status_code, response.json()["detail"]) == (422, "reason_code_mismatch")
    path = f"/api/v1/forecasts/{OPEN_CARD}/decisions"
    admin = client.post(path, json=DECISION, headers=csrf(client))
    assert admin.json()["actor_role"] == "admin"


def test_logout_revokes_session_and_clears_cookies():
    client, store, _ = make_client()
    assert login(client, "analyst").status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 403
    token = client.cookies[SESSION_COOKIE]
    response = client.post("/api/v1/auth/logout", headers=csrf(client))
    assert response.status_code == 204
    assert int(cookie_attributes(response, SESSION_COOKIE)["max-age"]) == 0
    assert store.actions() == ["auth.login", "auth.logout"]
    replayed = TestClient(client.app, base_url="https://testserver")
    replayed.cookies.set(SESSION_COOKIE, token)
    after = replayed.get("/api/v1/auth/me")
    assert (after.status_code, after.json()["detail"]) == (401, "session_expired")
    assert (
        TestClient(client.app, base_url="https://testserver")
        .post("/api/v1/auth/logout")
        .status_code
        == 204
    )


def test_expired_session_is_401():
    client, store, _ = make_client()
    assert login(client, "analyst").status_code == 200
    record = next(iter(store.sessions.values()))
    record["session"] = replace(record["session"], expires_at=datetime.now(UTC))
    response = client.get("/api/v1/forecasts")
    assert (response.status_code, response.json()["detail"]) == (401, "session_expired")


def test_failed_logins_are_throttled_before_the_directory():
    client, store, directory = make_client(max_failures=3)
    for _ in range(3):
        failed = login(client, "dispatcher", "wrong-password")
        assert (failed.status_code, failed.json()["detail"]) == (401, "invalid_credentials")
    assert login(client, "nobody").status_code == 401
    calls = directory.calls
    blocked = login(client, "dispatcher")
    assert (blocked.status_code, blocked.json()["detail"]) == (429, "login_throttled")
    assert int(blocked.headers["retry-after"]) > 0
    assert directory.calls == calls
    assert SESSION_COOKIE not in client.cookies
    assert store.actions().count("auth.login_failed") == 4
    assert login(client, "analyst").status_code == 200


def test_throttle_window_and_success_reset():
    now = [0.0]
    throttle = LoginThrottle(2, 60, clock=lambda: now[0])
    throttle.failure("User", "10.0.0.1")
    throttle.failure("user", "10.0.0.1")
    assert throttle.retry_after("USER", "10.0.0.2") == 61
    now[0] = 30.0
    assert 0 < throttle.retry_after("user", None) <= 31
    now[0] = 61.0
    assert throttle.retry_after("user", "10.0.0.1") == 0
    for index in range(8):
        throttle.failure(f"user-{index}", "10.0.0.9")
    assert throttle.retry_after("fresh-user", "10.0.0.9") > 0
    throttle.failure("user-x", None)
    throttle.success("user-x")
    assert throttle.retry_after("user-x", None) == 0


def test_directory_outage_and_missing_role_never_open_a_session():
    directory = FakeDirectory(USERS)
    client, store, _ = make_client(directory=directory)
    directory.available = False
    down = login(client, "dispatcher")
    assert (down.status_code, down.json()["detail"]) == (503, "directory_unavailable")
    assert "set-cookie" not in down.headers
    directory.available = True
    denied = login(client, "outsider")
    assert (denied.status_code, denied.json()["detail"]) == (403, "no_role")
    assert "set-cookie" not in denied.headers
    assert store.actions() == ["auth.login_denied"]
    assert not store.sessions


def test_storage_outage_fails_closed():
    client, store, _ = make_client()
    assert login(client, "dispatcher").status_code == 200
    store.available = False
    response = client.get("/api/v1/forecasts")
    assert (response.status_code, response.json()["detail"]) == (503, "auth_storage_unavailable")
    fresh, fresh_store, _ = make_client()
    fresh_store.available = False
    assert login(fresh, "dispatcher").status_code == 503
    assert SESSION_COOKIE not in fresh.cookies


def test_unconfigured_runtime_is_503_not_open():
    app = create_app(Settings(mode="fixture", auth_mode="ldap", _env_file=None))
    client = TestClient(app, base_url="https://testserver")
    response = login(client, "dispatcher")
    assert (response.status_code, response.json()["detail"]) == (503, "directory_not_configured")
    client.cookies.set(SESSION_COOKIE, "made-up-token")
    me = client.get("/api/v1/auth/me")
    assert (me.status_code, me.json()["detail"]) == (503, "auth_storage_not_configured")
    configured = create_app(
        Settings(
            mode="fixture",
            auth_mode="ldap",
            ldap_url="ldap://directory.invalid:3890",
            ldap_base_dn="dc=infrapulse,dc=test",
            _env_file=None,
        )
    )
    no_storage = TestClient(configured, base_url="https://testserver")
    stored = login(no_storage, "dispatcher")
    assert (stored.status_code, stored.json()["detail"]) == (503, "auth_storage_not_configured")


def test_relogin_replaces_the_previous_session():
    client, store, _ = make_client()
    assert login(client, "dispatcher").status_code == 200
    first = client.cookies[SESSION_COOKIE]
    assert login(client, "dispatcher").status_code == 200
    assert store.find_session(first) is None
    assert store.find_session(client.cookies[SESSION_COOKIE]) is not None


def test_session_tokens_are_random_and_only_digests_are_kept():
    one = NewSession.issue("u", "U", ["analyst"])
    two = NewSession.issue("u", "U", ["analyst"])
    assert len({one.token, one.csrf_token, two.token, two.csrf_token}) == 4
    assert len(token_digest(one.token)) == 64
    with pytest.raises(ValueError):
        NewSession.issue("u", "U", [])


def test_capabilities_report_authentication():
    client, _, _ = make_client()
    assert login(client, "analyst").status_code == 200
    assert client.get("/api/v1/capabilities").json()["authentication_ready"] is True
    local = TestClient(create_app(Settings(mode="fixture", _env_file=None)))
    assert local.get("/api/v1/capabilities").json()["authentication_ready"] is False
