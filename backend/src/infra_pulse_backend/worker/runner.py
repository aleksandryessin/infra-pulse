"""Processing of one import job: parse -> load -> recompute -> status.

Status path: ``queued -> parsing -> imported -> recomputing -> published``, or
``duplicate`` / ``failed`` with ``error_code``. A journal file whose every record was
already stored (no new row, no filled flag) is ``published`` with the current
``forecast_generation`` and no recompute, when that publication already covers the
stored records, the data time and the active channel reference (``UNCHANGED_NOTE``);
otherwise it is recomputed as any other file. Each arrow is committed, so a worker
restarted by lease expiry continues from the last committed status. The load of a
file (observations or reference rows, quarantine, counts, ``imported``) is a single
transaction: a crash inside it leaves no rows and no half-accepted file.

Every status change is written to ``audit_events`` (``import.status_changed``, G1,
ТЗ §11) in the same transaction as the change: actor ``worker:<worker id>``, no role,
request id ``job-<job>-<attempt>``, details ``from``/``to`` and the report counters
(never file content). The upload itself is ``import.uploaded`` by the user (HTTP).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NoReturn, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from infra_pulse_backend.ingestion.csv_source import FileRejected, Quarantined
from infra_pulse_backend.ingestion.imports_pg import stored_path
from infra_pulse_backend.ingestion.journal_csv import ParsedJournal, parse_journal
from infra_pulse_backend.ingestion.journal_json import parse_batch
from infra_pulse_backend.ingestion.load_pg import (
    NoValidRows,
    ReferenceMissing,
    load_journal,
    load_reference,
    save_quarantine,
)
from infra_pulse_backend.ingestion.reference_csv import (
    FORMAT_KIND,
    ParsedReference,
    parse_reference,
)
from infra_pulse_backend.storage.audit_pg import AuditEvent, record_audit_event
from infra_pulse_backend.worker.queue import (
    Claim,
    LeaseLost,
    claim_next,
    finish,
    hold,
    release_for_retry,
)

log = logging.getLogger("infra_pulse.worker")
# Journal formats load dispatch_observations and trigger the forecast recompute:
# the CSV upload (B1) and the observation API batch (B1x).
JOURNAL_FORMATS = ("journal_csv", "journal_json")
RECOMPUTE_MODULE = "infra_pulse_backend.operations.forecast_publish"


class PublishResult(Protocol):
    generation: int
    new_card_ids: Sequence[str]
    released_card_ids: Sequence[str]


Recompute = Callable[..., PublishResult | None]


def recompute_stub(
    connection: psycopg.Connection, *, data_as_of: datetime, import_id: str
) -> PublishResult | None:
    """Stand-in when ``operations.forecast_publish.recompute`` (B2) is not installed, and
    for tests: nothing is published and the import stays ``imported``."""
    return None


def resolve_recompute() -> Recompute:
    try:
        module = importlib.import_module(RECOMPUTE_MODULE)
    except ModuleNotFoundError as error:
        if error.name != RECOMPUTE_MODULE:
            raise
        return recompute_stub
    return module.recompute


@dataclass(frozen=True)
class WorkerConfig:
    dsn: str
    upload_dir: Path
    namespace_id: str
    stream_id: str
    worker_id: str
    lease_seconds: float = 300.0
    retry_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.namespace_id or not self.stream_id:
            raise ValueError("received namespace and stream are required")
        if not 1 <= self.lease_seconds <= 3600 or not 0 <= self.retry_seconds <= 3600:
            raise ValueError("lease must be 1–3600 s and retry delay 0–3600 s")


class _Stop(Exception):
    """The job reached its final state inside a step."""


def _log(**fields: object) -> None:
    log.info(json.dumps(fields, ensure_ascii=False, default=str))


# Report counters copied into the status audit row (no file content, no user data).
_AUDITED = (
    "error_code",
    "duplicate_of",
    "rows_total",
    "rows_accepted",
    "rows_duplicate",
    "rows_quarantined",
    "forecast_generation",
    "source_layout",
    "source_container",
    "alarm_not_provided",
)


def audit_status(
    connection: psycopg.Connection,
    *,
    import_id: str,
    actor: str,
    request: str,
    previous: str | None,
    status: str,
    details: dict | None = None,
) -> None:
    """``import.status_changed`` in the caller's transaction (ТЗ §11, G1)."""
    record_audit_event(
        connection,
        AuditEvent(
            action="import.status_changed",
            actor_id=actor[:256],
            target_kind="import_file",
            target_id=import_id,
            request_id=request[:128],
            outcome="failure" if status == "failed" else "success",
            details={"from": previous, "to": status, **(details or {})},
        ),
    )


def _update(connection: psycopg.Connection, claim: Claim, **fields: object) -> None:
    previous = None
    if "status" in fields:
        row = connection.execute(
            "SELECT status FROM import_files WHERE import_id = %s FOR UPDATE",
            (claim.import_id,),
        ).fetchone()
        previous = row[0] if row else None
    columns = ", ".join(f"{name} = %s" for name in fields)
    values = [Jsonb(item) if isinstance(item, dict | list) else item for item in fields.values()]
    connection.execute(
        f"UPDATE import_files SET {columns} WHERE import_id = %s", (*values, claim.import_id)
    )
    if "status" in fields and fields["status"] != previous:
        audit_status(
            connection,
            import_id=claim.import_id,
            actor=f"worker:{claim.worker_id}",
            request=f"job-{claim.job_id}-attempt-{claim.attempts}",
            previous=previous,
            status=str(fields["status"]),
            details={name: fields[name] for name in _AUDITED if fields.get(name) is not None},
        )


def _now(connection: psycopg.Connection) -> datetime:
    return connection.execute("SELECT clock_timestamp()").fetchone()[0]


def _timing(timings: list[dict], stage: str, seconds: float) -> list[dict]:
    kept = [item for item in timings if item["stage"] != stage]
    return [*kept, {"stage": stage, "seconds": round(max(seconds, 0.0), 4)}]


def _step(
    connection: psycopg.Connection, config: WorkerConfig, claim: Claim, **fields: object
) -> None:
    """Commit a status change only while this worker still owns the job."""
    with connection.transaction():
        hold(connection, claim, lease_seconds=config.lease_seconds)
        _update(connection, claim, **fields)


def _finish_import(
    connection: psycopg.Connection,
    config: WorkerConfig,
    claim: Claim,
    quarantine: list[Quarantined] | None = None,
    **fields: object,
) -> NoReturn:
    """Write a final status (failed / duplicate) and close the job in one transaction."""
    with connection.transaction():
        hold(connection, claim, lease_seconds=config.lease_seconds)
        if quarantine:
            save_quarantine(connection, claim.import_id, quarantine)
        _update(connection, claim, finished_at=_now(connection), **fields)
        finish(connection, claim)
    _log(import_id=claim.import_id, **{k: v for k, v in fields.items() if k in _LOGGED})
    raise _Stop


_LOGGED = {"status", "error_code", "duplicate_of", "rows_accepted", "rows_quarantined"}


def _fail(
    connection: psycopg.Connection,
    config: WorkerConfig,
    claim: Claim,
    code: str,
    detail: str,
    quarantine: list[Quarantined] | None = None,
    **fields: object,
) -> NoReturn:
    _finish_import(
        connection,
        config,
        claim,
        quarantine,
        status="failed",
        error_code=code,
        error_detail=detail[:200],
        **fields,
    )


def _read_import(connection: psycopg.Connection, import_id: str) -> dict:
    row = (
        connection.cursor(row_factory=dict_row)
        .execute(
            """SELECT import_id, format, file_name, sha256, stored_name, status, timings
               FROM import_files WHERE import_id = %s""",
            (import_id,),
        )
        .fetchone()
    )
    if row is None:
        raise LookupError(import_id)
    return row


def _parse(item: dict, data: bytes) -> ParsedJournal | ParsedReference:
    if item["format"] == "journal_csv":
        return parse_journal(data)
    if item["format"] == "journal_json":
        # The API stores an XML batch as <import>.xml and a JSON one as <import>.json.
        return parse_batch(data, "xml" if item["stored_name"].endswith(".xml") else "json")
    return parse_reference(data, FORMAT_KIND[item["format"]])


def _parse_and_load(
    connection: psycopg.Connection, config: WorkerConfig, claim: Claim, item: dict
) -> dict:
    """Stages parse and load. Returns the item when the journal awaits recompute."""
    timings = list(item["timings"])
    _step(connection, config, claim, status="parsing")
    original = connection.execute(
        """SELECT import_id FROM import_files
           WHERE format = %s AND sha256 = %s AND import_id <> %s
             AND status IN ('imported', 'recomputing', 'published')
           ORDER BY seq LIMIT 1""",
        (item["format"], item["sha256"], item["import_id"]),
    ).fetchone()
    if original is not None:
        # Same bytes as an accepted upload: nothing new, the copy is not kept.
        stored_path(config.upload_dir, item["stored_name"]).unlink(missing_ok=True)
        _finish_import(
            connection,
            config,
            claim,
            status="duplicate",
            duplicate_of=original[0],
            rows_total=0,
            rows_accepted=0,
            rows_duplicate=0,
            rows_quarantined=0,
        )

    try:
        data = stored_path(config.upload_dir, item["stored_name"]).read_bytes()
    except FileNotFoundError:
        _fail(connection, config, claim, "internal_error", "stored_file_missing")
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        _fail(connection, config, claim, "internal_error", "stored_file_changed")

    started = time.perf_counter()
    try:
        parsed = _parse(item, data)
    except FileRejected as error:
        _fail(connection, config, claim, error.code, str(error))
    del data
    timings = _timing(timings, "parse", time.perf_counter() - started)
    reasons = dict(Counter(row.reason for row in parsed.quarantined))
    duplicates = getattr(parsed, "duplicates", 0)
    source = _source_fields(item, parsed)

    if not parsed.rows:
        # No valid record: keep the quarantine report for the administrator.
        _fail(
            connection,
            config,
            claim,
            "no_valid_rows",
            "no valid records",
            parsed.quarantined,
            rows_total=len(parsed.quarantined) + duplicates,
            rows_accepted=0,
            rows_duplicate=duplicates,
            rows_quarantined=len(parsed.quarantined),
            technical_headers=parsed.technical_headers,
            quarantine_reasons=reasons,
            timings=timings,
            **source,
        )

    if isinstance(parsed, ParsedReference):
        started = time.perf_counter()
        with connection.transaction():
            hold(connection, claim, lease_seconds=config.lease_seconds)
            version_id = load_reference(
                connection, import_id=claim.import_id, parsed=parsed, sha256=item["sha256"]
            )
            timings = _timing(timings, "load", time.perf_counter() - started)
            _update(
                connection,
                claim,
                status="published",
                rows_total=parsed.records,
                rows_accepted=len(parsed.rows),
                rows_duplicate=duplicates,
                rows_quarantined=len(parsed.quarantined),
                technical_headers=parsed.technical_headers,
                reference_version=version_id,
                quarantine_reasons=reasons,
                timings=timings,
                finished_at=_now(connection),
                **source,
            )
            finish(connection, claim)
        _log(import_id=claim.import_id, status="published", reference_version=version_id)
        raise _Stop

    started = time.perf_counter()
    with connection.transaction():
        hold(connection, claim, lease_seconds=config.lease_seconds)
        try:
            loaded = load_journal(
                connection,
                import_id=claim.import_id,
                parsed=parsed,
                file_name=item["file_name"],
                sha256=item["sha256"],
                namespace_id=config.namespace_id,
                stream_id=config.stream_id,
            )
        except (ReferenceMissing, NoValidRows) as error:
            loaded, refused = None, error
        else:
            timings = _timing(timings, "load", time.perf_counter() - started)
            source = _source_fields(item, parsed, alarms_filled=loaded.alarms_filled)
            _update(
                connection,
                claim,
                status="imported",
                rows_total=loaded.rows_total,
                rows_accepted=loaded.rows_accepted,
                rows_duplicate=loaded.rows_duplicate,
                rows_quarantined=loaded.rows_quarantined,
                unknown_channels=loaded.unknown_channels,
                technical_headers=parsed.technical_headers,
                event_from=loaded.event_from,
                event_to=loaded.event_to,
                reference_version=loaded.reference_version,
                quarantine_reasons=loaded.quarantine_reasons,
                target_namespace=config.namespace_id,
                target_stream=config.stream_id,
                timings=timings,
                **source,
            )
    if loaded is None:
        # Both are raised before any write of the load (the transaction rolled back).
        if isinstance(refused, NoValidRows):
            _fail(
                connection,
                config,
                claim,
                "no_valid_rows",
                "no valid records",
                parsed.quarantined,
                rows_total=len(parsed.quarantined),
                rows_accepted=0,
                rows_duplicate=0,
                rows_quarantined=len(parsed.quarantined),
                technical_headers=parsed.technical_headers,
                quarantine_reasons=dict(Counter(row.reason for row in parsed.quarantined)),
                timings=timings,
                **_source_fields(item, parsed),
            )
        _fail(connection, config, claim, "reference_missing", "no channel reference loaded")
    _log(
        import_id=claim.import_id,
        status="imported",
        rows_accepted=loaded.rows_accepted,
        rows_duplicate=loaded.rows_duplicate,
        rows_quarantined=loaded.rows_quarantined,
        unknown_channels=loaded.unknown_channels,
        timings=timings,
    )
    # Records written by this load (new rows and filled flags). A worker restarted after
    # the load reads the item again without this key and recomputes as before.
    added = loaded.rows_accepted + loaded.alarms_filled
    return {**item, "status": "imported", "timings": timings, "added": added}


def _source_fields(
    item: dict, parsed: ParsedJournal | ParsedReference, *, alarms_filled: int = 0
) -> dict:
    """Recognised header and container, records without alarm, report lines (G1)."""
    container = getattr(parsed, "container", "csv")
    if item["format"] == "journal_json":
        layout = "api_batch"  # container: json or xml, as parse_batch recorded it
    elif isinstance(parsed, ParsedReference):
        layout = "reference"
    else:
        layout = parsed.layout
    notes = []
    missing = parsed.alarm_not_provided if isinstance(parsed, ParsedJournal) else 0
    if layout == "tz_appendix1":
        note = (
            f"Формат Приложения 1 ТЗ: признак тревожности источника не передан ({missing} записей)"
        )
        minute = parsed.minute_precision
        if minute and minute == len(parsed.rows):
            note += "; время с точностью до минуты"
        elif minute:
            note += f"; время с точностью до минуты ({minute} записей)"
        notes.append(note)
    if container == "xlsx":
        notes.append("XLSX: прочитан первый лист книги")
    if alarms_filled:
        notes.append(f"Признак тревожности дополнен у ранее принятых записей: {alarms_filled}")
    return {
        "source_layout": layout,
        "source_container": container,
        "alarm_not_provided": missing if layout != "reference" else None,
        "notes": notes,
    }


def _data_as_of(connection: psycopg.Connection, config: WorkerConfig) -> datetime | None:
    """Latest event time of the accepted journal files in the scope (not wall clock)."""
    return connection.execute(
        """SELECT max(event_to) FROM import_files
           WHERE format = ANY(%s) AND target_namespace = %s AND target_stream = %s
             AND status IN ('imported', 'recomputing', 'published')""",
        (list(JOURNAL_FORMATS), config.namespace_id, config.stream_id),
    ).fetchone()[0]


UNCHANGED_NOTE = "Новых записей нет: прогноз не пересчитывался"


def _unchanged_generation(
    connection: psycopg.Connection, config: WorkerConfig, data_as_of: datetime
) -> int | None:
    """The current publication when a recompute would publish the same (P2-5).

    Only for a load that wrote nothing: the scope was published, its data time is not
    earlier than the journal files, the detector has read every stored record and the
    layout follows the active channel reference. Otherwise None: recompute as usual.
    """
    row = connection.execute(
        """SELECT scope.generation
           FROM forecast_scopes AS scope
           JOIN dispatch_replay_snapshots AS stream
             ON stream.namespace_id = scope.namespace_id
            AND stream.snapshot_id = scope.snapshot_id
           WHERE scope.namespace_id = %s AND scope.snapshot_id = %s
             AND scope.generation >= 1 AND scope.data_as_of >= %s
             AND scope.detector_position >= stream.row_count
           FOR UPDATE OF scope""",
        (config.namespace_id, config.stream_id, data_as_of),
    ).fetchone()
    if row is None:
        return None
    stale_layout = connection.execute(
        """SELECT 1 FROM (
             SELECT version_id FROM ref_versions WHERE kind = 'channels'
             ORDER BY activation_seq DESC LIMIT 1
           ) AS active
           WHERE NOT EXISTS (
             SELECT 1 FROM forecast_channel_layout AS layout
             WHERE layout.reference_version = active.version_id
           )"""
    ).fetchone()
    return None if stale_layout else row[0]


def _recompute(
    connection: psycopg.Connection,
    config: WorkerConfig,
    claim: Claim,
    item: dict,
    recompute: Recompute,
) -> None:
    timings = list(item["timings"])
    _step(connection, config, claim, status="recomputing")
    data_as_of = _data_as_of(connection, config)
    started = time.perf_counter()
    try:
        with connection.transaction():
            hold(connection, claim, lease_seconds=config.lease_seconds)
            unchanged = (
                _unchanged_generation(connection, config, data_as_of)
                if item.get("added") == 0 and data_as_of is not None
                else None
            )
            if unchanged is not None:
                # Every record was already stored: the publication stays as it is.
                notes = connection.execute(
                    "SELECT notes FROM import_files WHERE import_id = %s", (claim.import_id,)
                ).fetchone()[0]
                _update(
                    connection,
                    claim,
                    status="published",
                    forecast_generation=unchanged,
                    notes=[*notes, UNCHANGED_NOTE][-10:],
                    finished_at=_now(connection),
                )
                finish(connection, claim)
                _log(import_id=claim.import_id, status="published", forecast_generation=unchanged)
                return
            result = (
                recompute(connection, data_as_of=data_as_of, import_id=claim.import_id)
                if data_as_of is not None
                else None
            )
            if result is None:
                # Nothing published: the stub (B2 not installed) or no accepted journal.
                _update(connection, claim, status="imported")
            else:
                stages = getattr(result, "timings", None) or {
                    "publish": time.perf_counter() - started
                }
                for stage, seconds in dict(stages).items():
                    if stage in ("detect", "score", "publish"):  # contract ImportStage
                        timings = _timing(timings, stage, float(seconds))
                _update(
                    connection,
                    claim,
                    status="published",
                    forecast_generation=result.generation,
                    new_card_ids=list(result.new_card_ids),
                    released_card_ids=list(result.released_card_ids),
                    timings=timings,
                    finished_at=_now(connection),
                )
            finish(connection, claim)
    except LeaseLost:
        raise
    except Exception as error:  # noqa: BLE001 - reported on the import; rows stay loaded
        log.exception("recompute failed for %s", claim.import_id)
        _fail(connection, config, claim, "recompute_failed", type(error).__name__)
    _log(
        import_id=claim.import_id,
        status="imported" if result is None else "published",
        forecast_generation=None if result is None else result.generation,
    )


def process(
    connection: psycopg.Connection,
    config: WorkerConfig,
    claim: Claim,
    recompute: Recompute,
) -> None:
    """Advance one claimed import from its last committed status to the end."""
    item = _read_import(connection, claim.import_id)
    try:
        if item["status"] in ("queued", "parsing"):
            item = _parse_and_load(connection, config, claim, item)
        if item["format"] in JOURNAL_FORMATS and item["status"] in ("imported", "recomputing"):
            _recompute(connection, config, claim, item, recompute)
            return
        with connection.transaction():
            finish(connection, claim)
    except _Stop:
        return


def run_once(config: WorkerConfig, recompute: Recompute | None = None) -> bool:
    """Claim and process one job. Returns False when nothing could be claimed."""
    recompute = recompute or resolve_recompute()
    with psycopg.connect(config.dsn, autocommit=True) as connection:
        claim, exhausted = claim_next(
            connection, worker_id=config.worker_id, lease_seconds=config.lease_seconds
        )
        for import_id in exhausted:
            with connection.transaction():
                row = connection.execute(
                    """WITH before AS (
                         SELECT status FROM import_files WHERE import_id = %s FOR UPDATE
                       )
                       UPDATE import_files SET status = 'failed',
                              error_code = 'internal_error',
                              error_detail = 'lease_expired', finished_at = clock_timestamp()
                       WHERE import_id = %s
                         AND status NOT IN ('published', 'duplicate', 'failed')
                       RETURNING (SELECT status FROM before)""",
                    (import_id, import_id),
                ).fetchone()
                if row is not None:
                    audit_status(
                        connection,
                        import_id=import_id,
                        actor=f"worker:{config.worker_id}",
                        request=f"lease-expired-{import_id}",
                        previous=row[0],
                        status="failed",
                        details={"error_code": "internal_error"},
                    )
            _log(import_id=import_id, status="failed", error_code="internal_error")
        if claim is None:
            return bool(exhausted)
        _log(import_id=claim.import_id, event="claimed", attempt=claim.attempts)
        try:
            process(connection, config, claim, recompute)
        except LeaseLost:
            _log(import_id=claim.import_id, event="lease_lost")
        except Exception as error:  # noqa: BLE001 - reported on the import, job retried
            log.exception("import %s failed", claim.import_id)
            _retry_or_fail(connection, config, claim, type(error).__name__)
        return True


def _retry_or_fail(
    connection: psycopg.Connection, config: WorkerConfig, claim: Claim, error: str
) -> None:
    try:
        with connection.transaction():
            hold(connection, claim, lease_seconds=config.lease_seconds)
            if release_for_retry(
                connection, claim, delay_seconds=config.retry_seconds, error=error
            ):
                requeued = connection.execute(
                    """UPDATE import_files SET status = 'queued'
                       WHERE import_id = %s AND status = 'parsing' RETURNING import_id""",
                    (claim.import_id,),
                ).fetchone()
                if requeued is not None:
                    audit_status(
                        connection,
                        import_id=claim.import_id,
                        actor=f"worker:{claim.worker_id}",
                        request=f"job-{claim.job_id}-attempt-{claim.attempts}",
                        previous="parsing",
                        status="queued",
                        details={"retry_error": error[:100]},
                    )
                _log(import_id=claim.import_id, event="retry", error=error)
                return
            _update(
                connection,
                claim,
                status="failed",
                error_code="internal_error",
                error_detail=error[:200],
                finished_at=_now(connection),
            )
            finish(connection, claim, failed=True)
            _log(import_id=claim.import_id, status="failed", error_code="internal_error")
    except (LeaseLost, psycopg.Error):
        # Another worker owns the job, or storage is down: the lease decides.
        log.exception("could not record the failure of %s", claim.import_id)
