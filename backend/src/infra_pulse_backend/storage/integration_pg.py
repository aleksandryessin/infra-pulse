"""Integration tokens and the batch register of the observation API (B1x, migration 0015).

Tokens: issued, listed and revoked by the administrator CLI
(``python -m infra_pulse_backend.admin token ...``); each action is written to
``audit_events`` in the same transaction. Only the SHA-256 of a token is stored.

Batches: ``POST /api/v1/observations`` stores the body as a file and calls
``register_batch``, which inserts ``import_files`` (``journal_json``), the worker job,
``observation_batches`` and the ``observations.received`` audit row in one
transaction, then wakes the worker. ``(client_id, batch_id)`` is the idempotency key:
the same key and the same bytes return the stored import; other bytes are a conflict.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from infra_pulse_backend.auth.sessions import token_digest
from infra_pulse_backend.auth.tokens import NAME_PATTERN, new_token, new_token_id
from infra_pulse_backend.ingestion.imports_pg import NOTIFY_CHANNEL, insert_import
from infra_pulse_backend.storage.audit_pg import AuditEvent, record_audit_event

BATCH_FORMAT = "journal_json"
_TOKEN_COLUMNS = "token_id, name, created_by, created_at, revoked_at, revoked_by, last_used_at"


@dataclass(frozen=True)
class TokenInfo:
    token_id: str
    name: str
    created_by: str
    created_at: datetime
    revoked_at: datetime | None
    revoked_by: str | None
    last_used_at: datetime | None


def _token(row: dict) -> TokenInfo:
    return TokenInfo(**{name: row[name] for name in TokenInfo.__dataclass_fields__})


def _admin_event(
    action: str, *, actor: str, request_id: str, target_id: str | None, details: dict
) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor_id=actor,
        actor_role="admin",
        target_kind="integration_token",
        target_id=target_id,
        request_id=request_id,
        details=details,
    )


def create_token(dsn: str, *, name: str, actor: str, request_id: str) -> tuple[str, TokenInfo]:
    """Issue a token; returns (token shown once, stored row). Audits the issue."""
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("token name must match [a-z0-9][a-z0-9._-]{0,63}")
    token, token_id = new_token(), new_token_id()
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        row = connection.execute(
            f"""INSERT INTO integration_tokens (token_id, name, token_sha256, created_by)
                VALUES (%s, %s, %s, %s) RETURNING {_TOKEN_COLUMNS}""",
            (token_id, name, token_digest(token), actor),
        ).fetchone()
        record_audit_event(
            connection,
            _admin_event(
                "integration_token.created",
                actor=actor,
                request_id=request_id,
                target_id=token_id,
                details={"name": name},
            ),
        )
    return token, _token(row)


def list_tokens(
    dsn: str, *, actor: str, request_id: str, include_revoked: bool = False
) -> list[TokenInfo]:
    """Tokens newest first (no secrets: only IDs, names and times). Audits the listing."""
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        rows = connection.execute(
            f"""SELECT {_TOKEN_COLUMNS} FROM integration_tokens
                WHERE %s OR revoked_at IS NULL
                ORDER BY created_at DESC, token_id""",
            (include_revoked,),
        ).fetchall()
        record_audit_event(
            connection,
            _admin_event(
                "integration_token.listed",
                actor=actor,
                request_id=request_id,
                target_id=None,
                details={"count": len(rows), "include_revoked": include_revoked},
            ),
        )
    return [_token(row) for row in rows]


def revoke_tokens(
    dsn: str,
    *,
    actor: str,
    request_id: str,
    token_id: str | None = None,
    name: str | None = None,
) -> list[TokenInfo]:
    """Revoke one token by ID or every live token of a name; effective immediately.

    Returns the tokens revoked now (already revoked ones are not repeated). Each
    revocation gets its own audit row in the same transaction.
    """
    if (token_id is None) == (name is None):
        raise ValueError("revoke by token ID or by name")
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        rows = connection.execute(
            f"""UPDATE integration_tokens
                SET revoked_at = clock_timestamp(), revoked_by = %s
                WHERE revoked_at IS NULL
                  AND ((%s::text IS NOT NULL AND token_id = %s::text)
                       OR (%s::text IS NOT NULL AND name = %s::text))
                RETURNING {_TOKEN_COLUMNS}""",
            (actor, token_id, token_id, name, name),
        ).fetchall()
        for row in rows:
            record_audit_event(
                connection,
                _admin_event(
                    "integration_token.revoked",
                    actor=actor,
                    request_id=request_id,
                    target_id=row["token_id"],
                    details={"name": row["name"]},
                ),
            )
    return [_token(row) for row in rows]


def find_token(dsn: str, token_id: str) -> TokenInfo | None:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        row = connection.execute(
            f"SELECT {_TOKEN_COLUMNS} FROM integration_tokens WHERE token_id = %s", (token_id,)
        ).fetchone()
    return _token(row) if row else None


@dataclass(frozen=True)
class StoredBatch:
    import_id: str
    sha256: str


class BatchRaced(Exception):
    """A concurrent request registered the same batch first; nothing was written."""


def find_batch(dsn: str, *, client_id: str, batch_id: str) -> StoredBatch | None:
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """SELECT import_id, sha256 FROM observation_batches
               WHERE client_id = %s AND batch_id = %s""",
            (client_id, batch_id),
        ).fetchone()
    return StoredBatch(*row) if row else None


def batch_owner(dsn: str, import_id: str) -> str | None:
    """Client that posted the batch of ``import_id`` (``None``: not an API batch)."""
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            "SELECT client_id FROM observation_batches WHERE import_id = %s", (import_id,)
        ).fetchone()
    return row[0] if row else None


def register_batch(
    dsn: str,
    *,
    client_id: str,
    batch_id: str,
    token_id: str | None,
    records: int,
    import_id: str,
    file_name: str,
    stored_name: str,
    sha256: str,
    size_bytes: int,
    store_seconds: float,
    audit: AuditEvent,
) -> dict:
    """Queue a stored batch: import, job, batch register and audit in one transaction.

    Returns the ``import_files`` row. Raises ``BatchRaced`` (after rolling back) when
    the key was registered concurrently; the caller then answers from ``find_batch``.
    """
    if audit.action != "observations.received" or audit.target_id != import_id:
        raise ValueError("the batch audit event must target this import")
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        row = insert_import(
            connection,
            import_id=import_id,
            format=BATCH_FORMAT,
            file_name=file_name,
            stored_name=stored_name,
            sha256=sha256,
            size_bytes=size_bytes,
            uploaded_by=client_id,
            store_seconds=store_seconds,
        )
        registered = connection.execute(
            """INSERT INTO observation_batches
               (client_id, batch_id, sha256, import_id, token_id, records)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (client_id, batch_id) DO NOTHING
               RETURNING import_id""",
            (client_id, batch_id, sha256, import_id, token_id, records),
        ).fetchone()
        if registered is None:
            raise BatchRaced(batch_id)  # the connection block rolls back
        record_audit_event(connection, audit)
        connection.execute(f"NOTIFY {NOTIFY_CHANNEL}")
    return row
