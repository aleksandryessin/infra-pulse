"""Request identity and permission checks shared by all routers (SEC-02, SEC-03).

``auth_mode=dev_stub`` (C0): every caller is the local operator with all roles;
allowed only locally or on a tunnel-only stand. ``auth_mode=ldap`` (B3): the caller is
the server-side session named by the ``__Host-infrapulse-session`` cookie; no session
is 401, a role outside ``PERMISSIONS[permission]`` is 403, and an unsafe method needs
``X-CSRF-Token`` equal to the session's anti-CSRF token (403 ``csrf_failed``). Storage
or directory outages fail closed with 503. Routers use only the two dependencies.

B1x: in either mode a request with ``Authorization: Bearer ipk_...`` is an external
system (``auth/tokens.py``): the caller is ``integration:<name>`` with the single role
``integration``; an unknown or revoked token is 401 ``invalid_token``. No cookie is
involved, so the anti-CSRF check does not apply to bearer requests.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, HTTPException, Request

from infra_pulse_backend.auth.ldap import Directory, LdapDirectory
from infra_pulse_backend.auth.sessions import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthStorageUnavailable,
    AuthStore,
    PgAuthStore,
    Session,
)
from infra_pulse_backend.auth.throttle import LoginThrottle
from infra_pulse_backend.auth.tokens import (
    IntegrationIdentity,
    PgTokenStore,
    TokenRateLimiter,
    TokenStore,
    bearer_token,
)
from infra_pulse_backend.config import Settings
from infra_pulse_core.contracts.auth import PERMISSIONS, Me

logger = logging.getLogger(__name__)

LOCAL_OPERATOR = Me(
    subject_id="local-operator",
    display_name="Демонстрационный пользователь",
    roles=["dispatcher", "analyst", "admin"],
    auth_source="dev_stub",
)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_RUNTIME_LOCK = Lock()


@dataclass
class AuthRuntime:
    """Session store, directory and throttle of one app (``app.state.auth_runtime``)."""

    store: AuthStore | None
    directory: Directory | None
    throttle: LoginThrottle
    # B1x: bearer tokens of external systems and their per-token request limit.
    tokens: TokenStore | None = None
    token_limiter: TokenRateLimiter | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "AuthRuntime":
        store = tokens = None
        if settings.db_dsn is not None:
            store = PgAuthStore(settings.db_dsn.get_secret_value())
            tokens = PgTokenStore(settings.db_dsn.get_secret_value())
        directory = None
        if settings.ldap_url and settings.ldap_base_dn:
            directory = LdapDirectory(
                settings.ldap_url,
                settings.ldap_base_dn,
                timeout=settings.ldap_timeout_seconds,
            )
        return cls(
            store=store,
            directory=directory,
            throttle=LoginThrottle(settings.login_max_failures, settings.login_window_seconds),
            tokens=tokens,
            token_limiter=TokenRateLimiter(settings.integration_requests_per_minute),
        )


def settings_of(request: Request) -> Settings | None:
    return getattr(request.app.state, "settings", None)


def auth_runtime(request: Request) -> AuthRuntime:
    """The app's auth runtime, created on first use (tests assign their own)."""
    state = request.app.state
    runtime = getattr(state, "auth_runtime", None)
    if runtime is None:
        with _RUNTIME_LOCK:
            runtime = getattr(state, "auth_runtime", None)
            if runtime is None:
                runtime = AuthRuntime.from_settings(settings_of(request) or Settings())
                state.auth_runtime = runtime
    return runtime


def session_store(request: Request) -> AuthStore:
    store = auth_runtime(request).store
    if store is None:
        raise HTTPException(status_code=503, detail="auth_storage_not_configured")
    return store


def request_id(request: Request) -> str:
    """Request ID for audit: the proxy's ``X-Request-ID`` when well-formed, else new."""
    cached = getattr(request.state, "request_id", None)
    if cached:
        return cached
    header = request.headers.get("x-request-id", "")
    value = header if _REQUEST_ID.fullmatch(header) else uuid4().hex
    request.state.request_id = value
    return value


def client_address(request: Request) -> str | None:
    """Client address as seen by uvicorn (proxy headers trusted only on the stand)."""
    return request.client.host if request.client else None


def acting_role(actor: Me, permission: str) -> str:
    """The first of the actor's roles that grants ``permission`` (audit and decisions)."""
    allowed = PERMISSIONS[permission]
    return next(role for role in actor.roles if role in allowed)


def current_session(request: Request) -> Session:
    """The live directory session of the request, else 401 (503 if storage is down)."""
    cached = getattr(request.state, "auth_session", None)
    if cached is not None:
        return cached
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="not_authenticated")
    try:
        session = session_store(request).find_session(token)
    except AuthStorageUnavailable as error:
        logger.warning("session lookup failed: %s", error)
        raise HTTPException(status_code=503, detail="auth_storage_unavailable") from error
    if session is None:
        raise HTTPException(status_code=401, detail="session_expired")
    request.state.auth_session = session
    return session


def require_csrf(request: Request, session: Session) -> None:
    if not session.csrf_matches(request.headers.get(CSRF_HEADER)):
        raise HTTPException(status_code=403, detail="csrf_failed")


def current_integration(request: Request) -> IntegrationIdentity | None:
    """Token identity of a bearer request already authenticated by ``current_actor``."""
    return getattr(request.state, "integration", None)


def _bearer_actor(request: Request, token: str) -> Me:
    tokens = auth_runtime(request).tokens
    if tokens is None:
        raise HTTPException(status_code=503, detail="auth_storage_not_configured")
    try:
        identity = tokens.authenticate(token) if token else None
    except AuthStorageUnavailable as error:
        logger.warning("token lookup failed: %s", error)
        raise HTTPException(status_code=503, detail="auth_storage_unavailable") from error
    if identity is None:
        raise HTTPException(
            status_code=401, detail="invalid_token", headers={"WWW-Authenticate": "Bearer"}
        )
    request.state.integration = identity
    return Me(
        subject_id=identity.actor_id,
        display_name=f"Интеграция {identity.name}",
        roles=["integration"],
        auth_source="token",
    )


def current_actor(request: Request) -> Me:
    """Identity of the caller. dev_stub: the local operator; ldap: the session owner.

    A bearer token (B1x) takes precedence in both modes and never falls back to them.
    The identity is also left in ``request.state.audit_actor`` for the request audit
    (``api/audit_log.py``, G1), before the anti-CSRF check so a rejected write is
    attributed to its session.
    """
    token = bearer_token(request.headers.get("authorization"))
    if token is not None:
        actor = _bearer_actor(request, token)
        request.state.audit_actor = actor
        return actor
    settings = settings_of(request)
    if settings is None or settings.auth_mode == "dev_stub":
        request.state.audit_actor = LOCAL_OPERATOR
        return LOCAL_OPERATOR
    session = current_session(request)
    actor = session.me()
    request.state.audit_actor = actor
    if request.method in UNSAFE_METHODS:
        require_csrf(request, session)
    return actor


def require_permission(permission: str) -> Callable[..., Me]:
    """Dependency: the caller has one of the roles allowed for ``permission``, else 403."""
    allowed = PERMISSIONS[permission]

    def dependency(request: Request, actor: Annotated[Me, Depends(current_actor)]) -> Me:
        # The request audit names the acting role for this permission (G1).
        request.state.audit_permission = permission
        if not allowed & set(actor.roles):
            raise HTTPException(status_code=403, detail="forbidden_role")
        return actor

    # Read by api/docs.py: the OpenAPI `security` of each route follows its permission.
    dependency.permission = permission  # type: ignore[attr-defined]
    return dependency
