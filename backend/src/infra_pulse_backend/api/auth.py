"""Login, logout and the current identity (SEC-02, SEC-03). **Owned by package B3.**

``auth_mode=dev_stub`` (local, tunnel-only): login is not required; ``/auth/login``
returns the local operator without a session and ``/auth/logout`` does nothing.

``auth_mode=ldap``: ``POST /auth/login`` needs a non-empty ``X-CSRF-Token`` header
(any value before a session exists), binds to the directory as the user and maps
groups to roles. Success sets the session and anti-CSRF cookies and audits
``auth.login``. Errors: 401 ``invalid_credentials``, 403 ``no_role`` or
``csrf_header_required``, 429 ``login_throttled`` with ``Retry-After``, 503
``directory_unavailable`` / ``directory_not_configured`` / ``auth_storage_*``.
``POST /auth/logout`` revokes the session (with ``X-CSRF-Token``), audits
``auth.logout``, clears both cookies and is 204 even without a session.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from infra_pulse_backend.api.auth_deps import (
    LOCAL_OPERATOR,
    auth_runtime,
    client_address,
    current_actor,
    current_session,
    request_id,
    require_csrf,
    session_store,
)
from infra_pulse_backend.auth.ldap import DirectoryUnavailable, InvalidCredentials, roles_for_groups
from infra_pulse_backend.auth.sessions import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthStorageUnavailable,
    NewSession,
    Session,
)
from infra_pulse_backend.config import Settings
from infra_pulse_backend.storage.audit_pg import AuditEvent
from infra_pulse_core.contracts.auth import LoginRequest, Me

logger = logging.getLogger(__name__)


def set_session_cookies(response: Response, new: NewSession, session: Session) -> None:
    # The database returns its session time zone; cookie dates are always GMT.
    expires = session.expires_at.astimezone(UTC)
    max_age = max(1, int((expires - datetime.now(UTC)).total_seconds()))
    common = {"max_age": max_age, "expires": expires, "path": "/", "secure": True}
    response.set_cookie(SESSION_COOKIE, new.token, httponly=True, samesite="lax", **common)
    response.set_cookie(CSRF_COOKIE, new.csrf_token, httponly=False, samesite="lax", **common)


def clear_session_cookies(response: Response) -> None:
    for name, http_only in ((SESSION_COOKIE, True), (CSRF_COOKIE, False)):
        response.delete_cookie(name, path="/", secure=True, httponly=http_only, samesite="lax")


def build_router(config: Settings) -> APIRouter:
    router = APIRouter()
    ttl = timedelta(minutes=config.session_ttl_minutes)

    def event(request: Request, action: str, actor_id: str, **fields) -> AuditEvent:
        return AuditEvent(
            action=action,
            actor_id=actor_id,
            target_kind="session",
            request_id=request_id(request),
            client_address=client_address(request),
            **fields,
        )

    def record_quietly(request: Request, audit: AuditEvent) -> None:
        """Failed attempts are audited best-effort; the answer does not depend on it."""
        store = auth_runtime(request).store
        if store is None:
            return
        try:
            store.record(audit)
        except AuthStorageUnavailable as error:
            logger.warning("login audit not stored: %s", error)

    @router.post("/api/v1/auth/login", response_model=Me)
    def login(payload: LoginRequest, request: Request, response: Response) -> Me:
        if config.auth_mode == "dev_stub":
            return LOCAL_OPERATOR
        if not request.headers.get(CSRF_HEADER):
            raise HTTPException(status_code=403, detail="csrf_header_required")
        runtime = auth_runtime(request)
        if runtime.directory is None:
            raise HTTPException(status_code=503, detail="directory_not_configured")
        store = session_store(request)
        username = payload.username.lower()
        address = client_address(request)
        wait = runtime.throttle.retry_after(username, address)
        if wait:
            raise HTTPException(
                status_code=429, detail="login_throttled", headers={"Retry-After": str(wait)}
            )
        try:
            user = runtime.directory.authenticate(username, payload.password.get_secret_value())
        except InvalidCredentials as error:
            runtime.throttle.failure(username, address)
            record_quietly(
                request, event(request, "auth.login_failed", username, outcome="failure")
            )
            raise HTTPException(status_code=401, detail="invalid_credentials") from error
        except DirectoryUnavailable as error:
            logger.warning("directory unavailable: %s", error)
            raise HTTPException(status_code=503, detail="directory_unavailable") from error
        roles = roles_for_groups(user.groups)
        if not roles:
            record_quietly(
                request,
                event(request, "auth.login_denied", user.subject_id, outcome="denied"),
            )
            raise HTTPException(status_code=403, detail="no_role")
        runtime.throttle.success(username)
        new = NewSession.issue(user.subject_id, user.display_name, roles)
        audit = event(
            request,
            "auth.login",
            user.subject_id,
            actor_role=roles[0],
            target_id=str(new.session_id),
            details={"roles": list(roles), "ttl_minutes": config.session_ttl_minutes},
        )
        try:
            session = store.open_session(
                new, ttl=ttl, event=audit, replaced_token=request.cookies.get(SESSION_COOKIE)
            )
        except AuthStorageUnavailable as error:
            raise HTTPException(status_code=503, detail="auth_storage_unavailable") from error
        set_session_cookies(response, new, session)
        return session.me()

    @router.post("/api/v1/auth/logout", status_code=204)
    def logout(request: Request) -> Response:
        response = Response(status_code=204)
        if config.auth_mode == "dev_stub":
            return response
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            try:
                session = current_session(request)
            except HTTPException as error:
                if error.status_code != 401:
                    raise
                session = None
            if session is not None:
                require_csrf(request, session)
                audit = event(
                    request,
                    "auth.logout",
                    session.subject_id,
                    actor_role=session.roles[0],
                    target_id=str(session.session_id),
                )
                try:
                    session_store(request).close_session(token, audit)
                except AuthStorageUnavailable as error:
                    raise HTTPException(
                        status_code=503, detail="auth_storage_unavailable"
                    ) from error
        clear_session_cookies(response)
        return response

    @router.get("/api/v1/auth/me", response_model=Me)
    def me(actor: Annotated[Me, Depends(current_actor)]) -> Me:
        return actor

    return router
