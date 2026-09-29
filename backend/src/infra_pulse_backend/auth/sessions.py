"""Server-side sessions for directory logins (SEC-02) with login/logout audit (SEC-03).

The browser holds two random tokens in ``Secure; SameSite=Lax`` cookies with the same
lifetime as the server row:

* ``__Host-infrapulse-session`` — ``HttpOnly``; identifies the session;
* ``__Host-infrapulse-csrf`` — readable by the SPA, echoed in ``X-CSRF-Token`` on every
  POST (double submit bound to the session: the server compares with a stored digest).

PostgreSQL (migration 0014) keeps only SHA-256 digests of both tokens, the subject,
display name and roles resolved at login. Logout revokes the row; group changes in the
directory take effect at the next login.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from infra_pulse_backend.storage.audit_pg import AuditEvent, record_audit_event
from infra_pulse_core.contracts.auth import Me, Role

SESSION_COOKIE = "__Host-infrapulse-session"
CSRF_COOKIE = "__Host-infrapulse-csrf"
CSRF_HEADER = "X-CSRF-Token"
# Expired or revoked rows are kept this long for investigation, then removed at login.
SESSION_RETENTION = timedelta(days=30)


class AuthStorageUnavailable(RuntimeError):
    """Session storage cannot be reached; requests fail closed with 503."""


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Session:
    session_id: UUID
    subject_id: str
    display_name: str
    roles: tuple[Role, ...]
    expires_at: datetime
    csrf_sha256: str

    def me(self) -> Me:
        return Me(
            subject_id=self.subject_id,
            display_name=self.display_name,
            roles=list(self.roles),
            auth_source="ldap",
            session_expires_at=self.expires_at,
        )

    def csrf_matches(self, header_value: str | None) -> bool:
        if not header_value:
            return False
        return secrets.compare_digest(token_digest(header_value), self.csrf_sha256)


@dataclass(frozen=True)
class NewSession:
    """Tokens for the cookies; only their digests leave this object."""

    session_id: UUID
    token: str
    csrf_token: str
    subject_id: str
    display_name: str
    roles: tuple[Role, ...]

    @classmethod
    def issue(cls, subject_id: str, display_name: str, roles: list[Role]) -> "NewSession":
        if not roles:
            raise ValueError("a session needs at least one role")
        return cls(
            session_id=uuid4(),
            token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            subject_id=subject_id,
            display_name=display_name,
            roles=tuple(roles),
        )


class AuthStore(Protocol):
    def open_session(
        self,
        new: NewSession,
        *,
        ttl: timedelta,
        event: AuditEvent,
        replaced_token: str | None = None,
    ) -> Session: ...

    def find_session(self, token: str) -> Session | None: ...

    def close_session(self, token: str, event: AuditEvent) -> Session | None: ...

    def record(self, event: AuditEvent) -> None: ...


def _session(row: dict[str, Any]) -> Session:
    return Session(
        session_id=row["session_id"],
        subject_id=row["subject_id"],
        display_name=row["display_name"],
        roles=tuple(row["roles"]),
        expires_at=row["expires_at"],
        csrf_sha256=row["csrf_sha256"],
    )


_RETURNING = "session_id, subject_id, display_name, roles, expires_at, csrf_sha256"


class PgAuthStore:
    """Sessions and auth audit in PostgreSQL; the database clock decides expiry."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5)

    def open_session(
        self,
        new: NewSession,
        *,
        ttl: timedelta,
        event: AuditEvent,
        replaced_token: str | None = None,
    ) -> Session:
        try:
            with self._connect() as connection:
                if replaced_token:
                    connection.execute(
                        """UPDATE auth_sessions SET revoked_at = clock_timestamp()
                           WHERE token_sha256 = %s AND revoked_at IS NULL""",
                        (token_digest(replaced_token),),
                    )
                row = connection.execute(
                    f"""INSERT INTO auth_sessions
                        (session_id, token_sha256, csrf_sha256, subject_id,
                         display_name, roles, expires_at)
                        VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp() + %s)
                        RETURNING {_RETURNING}""",
                    (
                        new.session_id,
                        token_digest(new.token),
                        token_digest(new.csrf_token),
                        new.subject_id,
                        new.display_name,
                        list(new.roles),
                        ttl,
                    ),
                ).fetchone()
                record_audit_event(connection, event)
                connection.execute(
                    """DELETE FROM auth_sessions
                       WHERE expires_at < clock_timestamp() - %s""",
                    (SESSION_RETENTION,),
                )
        except psycopg.Error as error:
            raise AuthStorageUnavailable("session storage unavailable") from error
        return _session(row)

    def find_session(self, token: str) -> Session | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    f"""SELECT {_RETURNING} FROM auth_sessions
                        WHERE token_sha256 = %s AND revoked_at IS NULL
                          AND expires_at > clock_timestamp()""",
                    (token_digest(token),),
                ).fetchone()
        except psycopg.Error as error:
            raise AuthStorageUnavailable("session storage unavailable") from error
        return _session(row) if row else None

    def close_session(self, token: str, event: AuditEvent) -> Session | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    f"""UPDATE auth_sessions SET revoked_at = clock_timestamp()
                        WHERE token_sha256 = %s AND revoked_at IS NULL
                          AND expires_at > clock_timestamp()
                        RETURNING {_RETURNING}""",
                    (token_digest(token),),
                ).fetchone()
                if row is not None:
                    record_audit_event(connection, event)
        except psycopg.Error as error:
            raise AuthStorageUnavailable("session storage unavailable") from error
        return _session(row) if row else None

    def record(self, event: AuditEvent) -> None:
        try:
            with self._connect() as connection:
                record_audit_event(connection, event)
        except psycopg.Error as error:
            raise AuthStorageUnavailable("audit storage unavailable") from error
