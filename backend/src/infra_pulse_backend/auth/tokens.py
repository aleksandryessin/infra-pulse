"""Bearer tokens of external systems for the observation API (package B1x).

``Authorization: Bearer ipk_<random>`` identifies an integration issued with
``python -m infra_pulse_backend.admin token create --name <name>``. PostgreSQL
(migration 0015) keeps only the SHA-256 of the token; every request looks the digest
up and stamps ``last_used_at`` in the same statement, so a revocation applies to the
next request. The caller becomes ``integration:<name>`` with the single role
``integration`` (permission ``ingest`` only). A bearer request carries no cookie, so
the anti-CSRF check of directory sessions does not apply to it.

The per-token request limit is in-process state like the login throttle: the API runs
as one uvicorn process on the stand and a restart resets the counters.
"""

from __future__ import annotations

import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

import psycopg
from psycopg.rows import dict_row

from infra_pulse_backend.auth.sessions import AuthStorageUnavailable, token_digest

TOKEN_PREFIX = "ipk_"
# ipk_ + 43 URL-safe characters (32 random bytes); the pattern leaves room for rotation.
TOKEN_PATTERN = re.compile(r"^ipk_[A-Za-z0-9_-]{32,128}$")
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ACTOR_PREFIX = "integration:"
_BEARER = re.compile(r"^Bearer[ ]+(\S+)[ ]*$", re.IGNORECASE)
_PRUNE_AT = 10_000


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_token_id() -> str:
    return f"tok-{secrets.token_hex(6)}"


def actor_id(name: str) -> str:
    return f"{ACTOR_PREFIX}{name}"


def bearer_token(authorization: str | None) -> str | None:
    """The token of an ``Authorization: Bearer`` header; ``""`` for a malformed header.

    ``None`` means the request did not use bearer authentication at all.
    """
    if authorization is None:
        return None
    match = _BEARER.fullmatch(authorization.strip())
    if match is None:
        return "" if authorization.strip().lower().startswith("bearer") else None
    return match.group(1)


@dataclass(frozen=True)
class IntegrationIdentity:
    token_id: str
    name: str

    @property
    def actor_id(self) -> str:
        return actor_id(self.name)


class TokenStore(Protocol):
    def authenticate(self, token: str) -> IntegrationIdentity | None: ...


class PgTokenStore:
    """Token lookup in PostgreSQL; the database clock stamps ``last_used_at``."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def authenticate(self, token: str) -> IntegrationIdentity | None:
        if not TOKEN_PATTERN.fullmatch(token):
            return None
        try:
            with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5) as db:
                row = db.execute(
                    """UPDATE integration_tokens SET last_used_at = clock_timestamp()
                       WHERE token_sha256 = %s AND revoked_at IS NULL
                       RETURNING token_id, name""",
                    (token_digest(token),),
                ).fetchone()
        except psycopg.Error as error:
            raise AuthStorageUnavailable("token storage unavailable") from error
        return IntegrationIdentity(row["token_id"], row["name"]) if row else None


class TokenRateLimiter:
    """Sliding one-minute window of requests per token (429 above the limit)."""

    def __init__(
        self,
        per_minute: int,
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_minute < 1:
            raise ValueError("the request limit must be positive")
        self.limit = per_minute
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return hits

    def acquire(self, key: str) -> int:
        """Count one request. Returns 0 when allowed, else seconds until the next slot."""
        now = self._clock()
        with self._lock:
            if len(self._hits) >= _PRUNE_AT:
                for other in list(self._hits):
                    if not self._recent(other, now):
                        del self._hits[other]
            hits = self._recent(key, now)
            if len(hits) >= self.limit:
                return int(hits[0] + self.window - now) + 1
            hits.append(now)
            return 0
