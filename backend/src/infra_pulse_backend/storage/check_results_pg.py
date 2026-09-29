"""Check results after a decision (C0.4, migration 0019) and their audit.

One POST is one transaction: a new revision in ``forecast_check_results`` and an
``audit_events`` row commit together. The author comes from the session. A per-card
advisory lock serialises writers: a repeated ``idempotency_key`` returns the stored
revision, a stale ``expected_revision`` raises ``CheckResultRevisionConflict``. The
result time must not be later than the record time (database clock). Whether the card's
event was registered (``event_cause`` is allowed) is decided by the caller from the
published forecast.
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
    ForecastCheckResult,
    ForecastCheckResultCreate,
    ForecastCheckResultList,
)

_COLUMNS = """forecast_id, revision, check_result, found, found_other_text, result_at,
              comment, event_cause, actor_id, actor_role, recorded_at, idempotency_key,
              payload_sha256"""


class CheckResultRevisionConflict(Exception):
    """The card has a newer check result than the one the dispatcher saw (409)."""


class IdempotencyKeyReused(Exception):
    """The key was already used for another payload or author (409)."""


class ResultAfterRecord(Exception):
    """``result_at`` is later than the record time (422)."""


def payload_digest(forecast_id: str, request: ForecastCheckResultCreate, actor_id: str) -> str:
    body = request.model_dump(mode="json", exclude={"idempotency_key"})
    canonical = json.dumps(
        {"forecast_id": forecast_id, "actor_id": actor_id, "request": body},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _result(row: dict[str, Any]) -> ForecastCheckResult:
    return ForecastCheckResult(
        forecast_id=row["forecast_id"],
        revision=row["revision"],
        check_result=row["check_result"],
        found=list(row["found"]),
        found_other_text=row["found_other_text"],
        result_at=row["result_at"],
        comment=row["comment"],
        event_cause=row["event_cause"],
        actor_id=row["actor_id"],
        actor_role=row["actor_role"],
        recorded_at=row["recorded_at"],
        simulated=False,
    )


def read_check_results(dsn: str, forecast_id: str) -> ForecastCheckResultList:
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        rows = connection.execute(
            f"""SELECT {_COLUMNS} FROM forecast_check_results
                WHERE forecast_id = %s ORDER BY revision DESC""",
            (forecast_id,),
        ).fetchall()
    return ForecastCheckResultList(forecast_id=forecast_id, items=[_result(r) for r in rows])


def latest_check_results(
    connection: psycopg.Connection,
    forecast_ids: Iterable[str],
    *,
    as_of: datetime | None = None,
) -> dict[str, ForecastCheckResult]:
    """Newest check result per card recorded at or before ``as_of`` (journal reads)."""
    ids = sorted(set(forecast_ids))
    if not ids:
        return {}
    with connection.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(
            f"""SELECT DISTINCT ON (forecast_id) {_COLUMNS} FROM forecast_check_results
                WHERE forecast_id = ANY(%s)
                  AND (%s::timestamptz IS NULL OR recorded_at <= %s::timestamptz)
                ORDER BY forecast_id, revision DESC""",
            (ids, as_of, as_of),
        ).fetchall()
    return {row["forecast_id"]: _result(row) for row in rows}


def create_check_result(
    dsn: str,
    *,
    forecast_id: str,
    request: ForecastCheckResultCreate,
    actor: Me,
    actor_role: str,
    request_id: str,
    client_address: str | None = None,
) -> ForecastCheckResult:
    """Store a check result revision and its audit row in one transaction."""
    if actor_role not in actor.roles:
        raise ValueError("acting role is not a role of the actor")
    digest = payload_digest(forecast_id, request, actor.subject_id)
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"infra_pulse.forecast_check_result:{forecast_id}",),
        )
        existing = connection.execute(
            f"""SELECT {_COLUMNS} FROM forecast_check_results
                WHERE forecast_id = %s AND idempotency_key = %s""",
            (forecast_id, request.idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != digest:
                raise IdempotencyKeyReused("idempotency key reused with another payload")
            return _result(existing)
        current = connection.execute(
            """SELECT COALESCE(max(revision), 0) AS revision
               FROM forecast_check_results WHERE forecast_id = %s""",
            (forecast_id,),
        ).fetchone()["revision"]
        if current != request.expected_revision:
            raise CheckResultRevisionConflict(
                f"expected revision {request.expected_revision}, current {current}"
            )
        recorded_at = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if request.result_at > recorded_at:
            raise ResultAfterRecord("result time is later than the record")
        check_result_id = uuid4()
        row = connection.execute(
            f"""INSERT INTO forecast_check_results
                (check_result_id, forecast_id, revision, check_result, found,
                 found_other_text, result_at, comment, event_cause, actor_id, actor_role,
                 idempotency_key, payload_sha256, request_id, recorded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_COLUMNS}""",
            (
                check_result_id,
                forecast_id,
                current + 1,
                request.check_result,
                list(request.found),
                request.found_other_text,
                request.result_at,
                request.comment,
                request.event_cause,
                actor.subject_id,
                actor_role,
                request.idempotency_key,
                digest,
                request_id,
                recorded_at,
            ),
        ).fetchone()
        record_audit_event(
            connection,
            AuditEvent(
                action="check_result.created",
                target_kind="forecast",
                target_id=forecast_id,
                details={
                    "check_result_id": str(check_result_id),
                    "revision": current + 1,
                    "check_result": request.check_result,
                    "found": list(request.found),
                    "event_cause": request.event_cause,
                    "result_at": request.result_at.isoformat(),
                },
                actor_id=actor.subject_id,
                actor_role=actor_role,
                request_id=request_id,
                client_address=client_address,
            ),
        )
    return _result(row)
