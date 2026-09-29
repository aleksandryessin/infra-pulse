"""Dispatcher decisions, unsent work-order drafts and their audit (API-03, API-04, SEC-03).

**Owned by B3.** One POST is one transaction: decision revision, draft «не отправлен»
for R3/R4 and ``audit_events`` rows commit together or not at all. The author comes
from the session (``Me``), never from the request body. A per-card advisory lock
serialises writers: a repeated ``idempotency_key`` returns the stored decision without
new rows, and a stale ``expected_revision`` raises ``DecisionRevisionConflict``.
«Кому и когда сообщено» (``notified_to``/``notified_at``, contract C0.1) is stored with
the revision and in its audit row; ``notified_at`` later than the decision time (the
database clock inside the transaction) raises ``NotifiedAfterDecision``.
C0.4 (migration 0019): R3 «Сообщено энергетику» stores ``awaiting_result_until`` and R1
«Под наблюдением» ``watch_until``; a deadline not later than the decision time raises
``DeadlineBeforeDecision``.
"""

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from infra_pulse_backend.storage.audit_pg import AuditEvent, record_audit_event
from infra_pulse_core.contracts.auth import Me
from infra_pulse_core.contracts.forecast import (
    ForecastDecisionCreate,
    ForecastDecisionList,
    ForecastDecisionSummary,
)

# Codes R1–R7 with reasons R<n>.<m> (technologist card v2 §7). The server checks the
# code/reason pairing only; the full reason list is not part of the contract yet.
DICTIONARY_VERSION = "technologist-dictionary-v1"
DRAFT_CODES = frozenset({"R3", "R4"})

_COLUMNS = """d.decision_id, d.forecast_id, d.revision, d.decision_code,
               d.reason_code, d.reason_text, d.verification_methods,
               d.dictionary_version, d.actor_id, d.actor_role, d.decided_at,
               d.notified_to, d.notified_at, d.idempotency_key, d.payload_sha256,
               d.awaiting_result_until, d.watch_until,
               w.draft_id, w.status AS draft_status"""
_FROM = """FROM forecast_decisions AS d
           LEFT JOIN work_order_drafts AS w ON w.decision_id = d.decision_id"""
_SELECT = f"SELECT {_COLUMNS} {_FROM}"


class DecisionRevisionConflict(Exception):
    """The card has a newer decision than the one the dispatcher saw (409)."""


class IdempotencyKeyReused(Exception):
    """The key was already used for another payload or author (409)."""


class NotifiedAfterDecision(Exception):
    """``notified_at`` is later than the decision time (422)."""


class DeadlineBeforeDecision(Exception):
    """``awaiting_result_until`` or ``watch_until`` is not later than the decision (422)."""


def reason_matches_code(request: ForecastDecisionCreate) -> bool:
    """Reason codes belong to their decision: R3 -> R3.1, R3.2, ..."""
    prefix, _, number = request.reason_code.partition(".")
    return prefix == request.decision_code and number.isdigit()


def payload_digest(forecast_id: str, request: ForecastDecisionCreate, actor_id: str) -> str:
    body = request.model_dump(mode="json", exclude={"idempotency_key"})
    canonical = json.dumps(
        {"forecast_id": forecast_id, "actor_id": actor_id, "request": body},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _summary(row: dict[str, Any]) -> ForecastDecisionSummary:
    draft_id = row["draft_id"]
    return ForecastDecisionSummary(
        decision_code=row["decision_code"],
        reason_code=row["reason_code"],
        reason_text=row["reason_text"],
        dictionary_version=row["dictionary_version"],
        actor_id=row["actor_id"],
        actor_role=row["actor_role"],
        decided_at=row["decided_at"],
        revision=row["revision"],
        simulated=False,
        verification_methods=list(row["verification_methods"]),
        notified_to=row["notified_to"],
        notified_at=row["notified_at"],
        awaiting_result_until=row.get("awaiting_result_until"),
        watch_until=row.get("watch_until"),
        draft_id=str(draft_id) if draft_id is not None else None,
        draft_status=row["draft_status"] if draft_id is not None else None,
    )


def read_decisions(dsn: str, forecast_id: str) -> ForecastDecisionList:
    """All revisions of one card, newest first, with authors."""
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        rows = connection.execute(
            f"{_SELECT} WHERE d.forecast_id = %s ORDER BY d.revision DESC",
            (forecast_id,),
        ).fetchall()
    return ForecastDecisionList(forecast_id=forecast_id, items=[_summary(row) for row in rows])


def latest_decisions(
    connection: psycopg.Connection,
    forecast_ids: Iterable[str],
    *,
    as_of: datetime | None = None,
) -> dict[str, ForecastDecisionSummary]:
    """Newest decision per card, for journal/card reads (B2) in their own transaction.

    With ``as_of`` only decisions saved at or before it count: the journal passes its
    ``records_as_of`` (wall clock), not the data moment of the scope.
    """
    ids = sorted(set(forecast_ids))
    if not ids:
        return {}
    with connection.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(
            f"""SELECT DISTINCT ON (d.forecast_id) {_COLUMNS} {_FROM}
                WHERE d.forecast_id = ANY(%s)
                  AND (%s::timestamptz IS NULL OR d.decided_at <= %s::timestamptz)
                ORDER BY d.forecast_id, d.revision DESC""",
            (ids, as_of, as_of),
        ).fetchall()
    return {row["forecast_id"]: _summary(row) for row in rows}


def create_decision(
    dsn: str,
    *,
    forecast_id: str,
    request: ForecastDecisionCreate,
    actor: Me,
    actor_role: str,
    request_id: str,
    client_address: str | None = None,
) -> ForecastDecisionSummary:
    """Store a decision, its draft and audit rows in one transaction."""
    if not reason_matches_code(request):
        raise ValueError("reason code does not belong to the decision code")
    if actor_role not in actor.roles:
        raise ValueError("acting role is not a role of the actor")
    digest = payload_digest(forecast_id, request, actor.subject_id)
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"infra_pulse.forecast_decision:{forecast_id}",),
        )
        existing = connection.execute(
            f"{_SELECT} WHERE d.forecast_id = %s AND d.idempotency_key = %s",
            (forecast_id, request.idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != digest:
                raise IdempotencyKeyReused("idempotency key reused with another payload")
            return _summary(existing)
        current = connection.execute(
            """SELECT COALESCE(max(revision), 0) AS revision
               FROM forecast_decisions WHERE forecast_id = %s""",
            (forecast_id,),
        ).fetchone()["revision"]
        if current != request.expected_revision:
            raise DecisionRevisionConflict(
                f"expected revision {request.expected_revision}, current {current}"
            )
        # One clock for the stored decision time and the notification check.
        decided_at = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if request.notified_at is not None and request.notified_at > decided_at:
            raise NotifiedAfterDecision("notification is later than the decision")
        for deadline in (request.awaiting_result_until, request.watch_until):
            if deadline is not None and deadline <= decided_at:
                raise DeadlineBeforeDecision("a decision deadline is not after the decision")
        decision_id = uuid4()
        row = connection.execute(
            """INSERT INTO forecast_decisions
               (decision_id, forecast_id, revision, decision_code, reason_code,
                reason_text, verification_methods, dictionary_version, actor_id,
                actor_role, notified_to, notified_at, idempotency_key,
                payload_sha256, request_id, decided_at, awaiting_result_until, watch_until)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING decision_id, forecast_id, revision, decision_code,
                         reason_code, reason_text, verification_methods,
                         dictionary_version, actor_id, actor_role, decided_at,
                         notified_to, notified_at, idempotency_key, payload_sha256,
                         awaiting_result_until, watch_until,
                         NULL::uuid AS draft_id, NULL::text AS draft_status""",
            (
                decision_id,
                forecast_id,
                current + 1,
                request.decision_code,
                request.reason_code,
                request.reason_text,
                list(request.verification_methods),
                DICTIONARY_VERSION,
                actor.subject_id,
                actor_role,
                request.notified_to,
                request.notified_at,
                request.idempotency_key,
                digest,
                request_id,
                decided_at,
                request.awaiting_result_until,
                request.watch_until,
            ),
        ).fetchone()
        audit = {
            "actor_id": actor.subject_id,
            "actor_role": actor_role,
            "request_id": request_id,
            "client_address": client_address,
        }
        record_audit_event(
            connection,
            AuditEvent(
                action="decision.created",
                target_kind="forecast",
                target_id=forecast_id,
                details={
                    "decision_id": str(decision_id),
                    "revision": current + 1,
                    "decision_code": request.decision_code,
                    "reason_code": request.reason_code,
                    "notified_to": request.notified_to,
                    "notified_at": (
                        request.notified_at.isoformat() if request.notified_at else None
                    ),
                    "awaiting_result_until": (
                        request.awaiting_result_until.isoformat()
                        if request.awaiting_result_until
                        else None
                    ),
                    "watch_until": (
                        request.watch_until.isoformat() if request.watch_until else None
                    ),
                },
                **audit,
            ),
        )
        if request.decision_code in DRAFT_CODES:
            draft = connection.execute(
                """INSERT INTO work_order_drafts
                   (draft_id, decision_id, forecast_id, decision_code, note, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING draft_id, status""",
                (
                    uuid4(),
                    decision_id,
                    forecast_id,
                    request.decision_code,
                    request.draft_note,
                    actor.subject_id,
                ),
            ).fetchone()
            row |= {"draft_id": draft["draft_id"], "draft_status": draft["status"]}
            record_audit_event(
                connection,
                AuditEvent(
                    action="draft.created",
                    target_kind="work_order_draft",
                    target_id=str(draft["draft_id"]),
                    details={"decision_id": str(decision_id), "status": draft["status"]},
                    **audit,
                ),
            )
    return _summary(row)
