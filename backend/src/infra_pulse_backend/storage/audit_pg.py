"""Append-only audit journal ``audit_events`` (SEC-03, migration 0014). **Owned by B3.**

``record_audit_event`` writes inside the caller's transaction, so an action and its
audit row commit or roll back together (API-03). Writers: sessions (``auth.*``),
decisions and drafts (``storage/decisions_pg.py``) and the upload path (B1,
``import.uploaded`` in ``ingestion/imports_pg.create_import``; the router
``api/imports.py`` builds the event from ``auth_deps``)::

    with psycopg.connect(dsn) as connection:          # one transaction
        ...insert import_files and jobs...
        record_audit_event(connection, AuditEvent(
            action="import.uploaded", actor_id=actor.subject_id,
            actor_role=acting_role(actor, "import"), target_kind="import_file",
            target_id=import_id, request_id=request_id(request),
            client_address=client_address(request),
            details={"sha256": digest, "format": fmt, "size_bytes": size},
        ))

Details never contain passwords, session or anti-CSRF tokens.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

AuditOutcome = Literal["success", "denied", "failure"]
_ACTION = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*$")
_KIND = re.compile(r"^[a-z][a-z_]*$")


@dataclass(frozen=True)
class AuditEvent:
    action: str
    actor_id: str
    target_kind: str
    request_id: str
    target_id: str | None = None
    actor_role: str | None = None
    outcome: AuditOutcome = "success"
    client_address: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _ACTION.fullmatch(self.action):
            raise ValueError("audit action must look like area.verb")
        if not _KIND.fullmatch(self.target_kind):
            raise ValueError("invalid audit target kind")
        if not 1 <= len(self.actor_id) <= 256 or not 1 <= len(self.request_id) <= 128:
            raise ValueError("audit actor and request id are required")


def record_audit_event(connection: psycopg.Connection, event: AuditEvent) -> UUID:
    """Insert one audit row in the current transaction of ``connection``."""
    audit_id = uuid4()
    connection.execute(
        """INSERT INTO audit_events
           (audit_id, action, outcome, actor_id, actor_role, target_kind,
            target_id, request_id, client_address, details)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            audit_id,
            event.action,
            event.outcome,
            event.actor_id,
            event.actor_role,
            event.target_kind,
            event.target_id,
            event.request_id,
            event.client_address[:64] if event.client_address else None,
            Jsonb(dict(event.details)),
        ),
    )
    return audit_id
