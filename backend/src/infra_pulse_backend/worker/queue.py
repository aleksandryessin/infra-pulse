"""PostgreSQL job queue of the ingestion worker: ``FOR UPDATE SKIP LOCKED`` + lease.

Jobs are processed strictly in upload order: a later job is not claimed while an
earlier one is still pending or running under a live lease, because the rolling
forecast list depends on history order. Claims are serialized by a transaction
advisory lock. A job whose lease expired (the worker crashed or was restarted) is
claimed again; after ``max_attempts`` claims it is failed with ``internal_error``.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import tuple_row

CLAIM_LOCK = 0x1B1_1A0B  # pg_advisory_xact_lock key for queue claims (B1)


class LeaseLost(RuntimeError):
    """Another worker owns the job now; the current step must not write."""


@dataclass(frozen=True)
class Claim:
    job_id: int
    import_id: str
    attempts: int
    max_attempts: int
    worker_id: str


def claim_next(
    connection: psycopg.Connection, *, worker_id: str, lease_seconds: float
) -> tuple[Claim | None, list[str]]:
    """Claim the oldest pending job. Returns (claim, imports failed as exhausted)."""
    exhausted: list[str] = []
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (CLAIM_LOCK,))
        cursor = connection.cursor(row_factory=tuple_row)
        while True:
            head = cursor.execute(
                """SELECT job_id, import_id, state, attempts, max_attempts,
                          run_after > clock_timestamp() AS waiting,
                          lease_expires_at > clock_timestamp() AS leased
                   FROM jobs WHERE state IN ('queued', 'running')
                   ORDER BY job_id LIMIT 1
                   FOR UPDATE SKIP LOCKED"""
            ).fetchone()
            if head is None:
                return None, exhausted
            job_id, import_id, state, attempts, max_attempts, waiting, leased = head
            if state == "running" and leased:
                return None, exhausted
            if state == "queued" and waiting:
                return None, exhausted
            if state == "running" and attempts >= max_attempts:
                # The lease expired on the last allowed attempt: give up on it.
                connection.execute(
                    """UPDATE jobs SET state = 'failed', lease_owner = NULL,
                              lease_expires_at = NULL, updated_at = clock_timestamp(),
                              last_error = 'lease_expired'
                       WHERE job_id = %s""",
                    (job_id,),
                )
                exhausted.append(import_id)
                continue
            # A pending job with an earlier ID that another claimer holds locked
            # is skipped by SKIP LOCKED; this guard keeps the upload order.
            earlier = cursor.execute(
                """SELECT 1 FROM jobs
                   WHERE job_id < %s AND state IN ('queued', 'running') LIMIT 1""",
                (job_id,),
            ).fetchone()
            if earlier is not None:
                return None, exhausted
            connection.execute(
                """UPDATE jobs SET state = 'running', attempts = attempts + 1,
                          lease_owner = %s,
                          lease_expires_at = clock_timestamp() + %s * interval '1 second',
                          updated_at = clock_timestamp()
                   WHERE job_id = %s""",
                (worker_id, lease_seconds, job_id),
            )
            return Claim(job_id, import_id, attempts + 1, max_attempts, worker_id), exhausted


def hold(connection: psycopg.Connection, claim: Claim, *, lease_seconds: float) -> None:
    """Inside the caller's transaction: lock the job, check ownership, extend the lease.

    While the row is locked other claimers skip it, so a long load cannot be taken
    over halfway; ownership is re-checked before every write.
    """
    row = connection.execute(
        """UPDATE jobs SET lease_expires_at = clock_timestamp() + %s * interval '1 second',
                  updated_at = clock_timestamp()
           WHERE job_id = %s AND lease_owner = %s AND state = 'running'
           RETURNING job_id""",
        (lease_seconds, claim.job_id, claim.worker_id),
    ).fetchone()
    if row is None:
        raise LeaseLost(claim.import_id)


def finish(connection: psycopg.Connection, claim: Claim, *, failed: bool = False) -> None:
    connection.execute(
        """UPDATE jobs SET state = %s, lease_owner = NULL, lease_expires_at = NULL,
                  updated_at = clock_timestamp()
           WHERE job_id = %s AND lease_owner = %s""",
        ("failed" if failed else "done", claim.job_id, claim.worker_id),
    )


def release_for_retry(
    connection: psycopg.Connection, claim: Claim, *, delay_seconds: float, error: str
) -> bool:
    """Requeue after a transient error; False when attempts are exhausted."""
    if claim.attempts >= claim.max_attempts:
        return False
    row = connection.execute(
        """UPDATE jobs SET state = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                  run_after = clock_timestamp() + %s * interval '1 second',
                  last_error = %s, updated_at = clock_timestamp()
           WHERE job_id = %s AND lease_owner = %s
           RETURNING job_id""",
        (delay_seconds * claim.attempts, error[:200], claim.job_id, claim.worker_id),
    ).fetchone()
    if row is None:
        raise LeaseLost(claim.import_id)
    return True
