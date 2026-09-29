"""Append-only local review notes and matching audit rows."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from infra_pulse_backend.operations.attention import POLICY_VERSION, explain_attention
from infra_pulse_core.contracts.attention import (
    ObservedMessage,
    ReviewJournalEntry,
    ReviewJournalList,
    ReviewNote,
    ReviewNoteCreate,
    ReviewNoteList,
)

LOCAL_ACTOR = "local-operator"

NOTE_FIELDS = (
    "note_id",
    "row_uid",
    "revision",
    "actor_id",
    "action_text",
    "result_text",
    "reason_text",
    "created_at",
    "view_as_of",
    "displayed_received_watermark",
    "policy_version",
    "attention_band",
    "reason_codes",
)

MESSAGE_FIELDS = (
    "row_uid",
    "source_event_id",
    "channel_id",
    "object_id",
    "sensor_type",
    "system_type",
    "value_raw",
    "value_numeric",
    "alarm",
    "event_at",
    "available_at",
    "availability_basis",
    "source_namespace",
    "snapshot_id",
    "source_file",
    "source_sha256",
    "record_ordinal",
    "reference_version",
    "quality_flags",
)


class RevisionConflict(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


def payload_hash(payload: ReviewNoteCreate) -> str:
    serializable = payload.model_dump(mode="json", exclude={"idempotency_key"})
    canonical = json.dumps(serializable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def read_review_notes(
    dsn: str,
    *,
    namespace_id: str,
    snapshot_id: str,
    row_uid: str,
    mode: Literal["replay", "received"] = "replay",
) -> ReviewNoteList:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        present = connection.execute(
            """SELECT 1 FROM dispatch_observations
               WHERE namespace_id = %s AND snapshot_id = %s AND row_uid = %s""",
            (namespace_id, snapshot_id, row_uid),
        ).fetchone()
        if present is None:
            raise LookupError("source observation not found")
        rows = connection.execute(
            """SELECT note_id, row_uid, revision, actor_id,
                      action_text, result_text, reason_text, created_at,
                      view_as_of, displayed_received_watermark,
                      policy_version, attention_band, reason_codes
               FROM replay_review_notes
               WHERE namespace_id = %s AND snapshot_id = %s AND row_uid = %s
               ORDER BY revision""",
            (namespace_id, snapshot_id, row_uid),
        ).fetchall()
    items = [ReviewNote.model_validate(row) for row in rows]
    return ReviewNoteList(
        mode=mode, row_uid=row_uid, revision=items[-1].revision if items else 0, items=items
    )


def read_review_journal(
    dsn: str,
    *,
    namespace_id: str,
    snapshot_id: str,
    offset: int,
    limit: int,
    row_uid: str | None = None,
    object_id: str | None = None,
    channel_id: str | None = None,
    review_watermark: int | None = None,
    mode: Literal["replay", "received"] = "replay",
) -> ReviewJournalList:
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("invalid review journal page")
    if review_watermark is not None and review_watermark < 0:
        raise ValueError("invalid review watermark")
    filters = []
    filter_params: list[str] = []
    if row_uid is not None:
        filters.append("AND n.row_uid = %s")
        filter_params.append(row_uid)
    if object_id == "__unknown__":
        filters.append("AND o.object_id IS NULL")
    elif object_id is not None:
        filters.append("AND o.object_id = %s")
        filter_params.append(object_id)
    if channel_id is not None:
        filters.append("AND o.channel_id = %s")
        filter_params.append(channel_id)
    source_join = """FROM replay_review_notes AS n
               JOIN dispatch_observations AS o
                 ON o.namespace_id = n.namespace_id
                AND o.snapshot_id = n.snapshot_id AND o.row_uid = n.row_uid"""
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        scope = connection.execute(
            """SELECT review_count FROM dispatch_replay_snapshots
               WHERE namespace_id = %s AND snapshot_id = %s AND scope_kind = %s""",
            (namespace_id, snapshot_id, mode),
        ).fetchone()
        if scope is None:
            raise LookupError("review scope not found")
        if review_watermark is not None and review_watermark > scope["review_count"]:
            raise ValueError("review watermark exceeds committed notes")
        effective_watermark = (
            scope["review_count"] if review_watermark is None else review_watermark
        )
        where = "AND n.review_position <= %s " + " ".join(filters)
        params = (namespace_id, snapshot_id, effective_watermark, *filter_params)
        total = connection.execute(
            f"""SELECT count(*) AS n {source_join}
               WHERE n.namespace_id = %s AND n.snapshot_id = %s {where}""",
            params,
        ).fetchone()["n"]
        rows = connection.execute(
            f"""SELECT n.note_id, n.row_uid, n.revision, n.actor_id,
                      n.action_text, n.result_text, n.reason_text, n.created_at,
                      n.view_as_of, n.displayed_received_watermark,
                      n.policy_version, n.attention_band, n.reason_codes,
                      o.source_event_id, o.channel_id, o.object_id,
                      o.sensor_type, o.system_type, o.value_raw, o.value_numeric,
                      o.alarm, o.event_at, o.available_at, o.availability_basis,
                      o.namespace_id AS source_namespace, o.snapshot_id,
                      o.source_file, o.source_sha256, o.record_ordinal,
                      o.reference_version, o.quality_flags
               {source_join}
               WHERE n.namespace_id = %s AND n.snapshot_id = %s {where}
               ORDER BY n.created_at DESC, n.note_id DESC
               LIMIT %s OFFSET %s""",
            (*params, limit, offset),
        ).fetchall()
    items = [
        ReviewJournalEntry(
            note=ReviewNote.model_validate({field: row[field] for field in NOTE_FIELDS}),
            message=ObservedMessage.model_validate({field: row[field] for field in MESSAGE_FIELDS}),
        )
        for row in rows
    ]
    return ReviewJournalList(
        mode=mode,
        items=items,
        offset=offset,
        total=total,
        review_watermark=effective_watermark,
    )


def create_review_note(
    dsn: str,
    *,
    namespace_id: str,
    snapshot_id: str,
    row_uid: str,
    payload: ReviewNoteCreate,
    mode: Literal["replay", "received"] = "replay",
    actor_id: str = LOCAL_ACTOR,
) -> ReviewNote:
    if payload.displayed_snapshot_id != snapshot_id:
        raise RevisionConflict("displayed snapshot changed")
    digest = payload_hash(payload)
    scope = (namespace_id, snapshot_id, row_uid)
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        source = connection.execute(
            """SELECT row_uid, available_at, received_position,
                      alarm, sensor_type, value_raw
               FROM dispatch_observations
               WHERE namespace_id = %s AND snapshot_id = %s AND row_uid = %s
               FOR UPDATE""",
            scope,
        ).fetchone()
        if source is None:
            raise LookupError("source observation not found")
        existing = connection.execute(
            """SELECT note_id, row_uid, revision, actor_id,
                      action_text, result_text, reason_text, created_at,
                      view_as_of, displayed_received_watermark,
                      policy_version, attention_band, reason_codes,
                      payload_sha256
               FROM replay_review_notes
               WHERE namespace_id = %s AND snapshot_id = %s AND row_uid = %s
                 AND idempotency_key = %s""",
            (*scope, payload.idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing.pop("payload_sha256") != digest:
                raise IdempotencyConflict("idempotency key reused with a different payload")
            return ReviewNote.model_validate(existing)
        if payload.displayed_policy_version != POLICY_VERSION:
            raise RevisionConflict("displayed policy changed")
        window = connection.execute(
            """SELECT window_start, window_end, row_count, scope_kind
               FROM dispatch_replay_snapshots
               WHERE namespace_id = %s AND snapshot_id = %s""",
            (namespace_id, snapshot_id),
        ).fetchone()
        if window is None or window["scope_kind"] != mode:
            raise LookupError("observation scope not found")
        if mode == "replay":
            if payload.displayed_received_watermark is not None:
                raise ValueError("received watermark is not valid for replay")
            valid_time = window["window_start"] <= payload.view_as_of <= window["window_end"]
        else:
            watermark = payload.displayed_received_watermark
            if (
                watermark is None
                or source["received_position"] is None
                or not source["received_position"] <= watermark <= window["row_count"]
            ):
                raise ValueError("source observation is outside the displayed received list")
            boundary = connection.execute(
                """SELECT available_at FROM dispatch_observations
                   WHERE namespace_id = %s AND snapshot_id = %s
                     AND received_position = %s""",
                (namespace_id, snapshot_id, watermark),
            ).fetchone()
            later_in_prefix = connection.execute(
                """SELECT 1 FROM dispatch_observations
                   WHERE namespace_id = %s AND snapshot_id = %s
                     AND received_position <= %s AND available_at > %s
                   LIMIT 1""",
                (namespace_id, snapshot_id, watermark, payload.view_as_of),
            ).fetchone()
            if (
                boundary is None
                or boundary["available_at"] > payload.view_as_of
                or later_in_prefix is not None
            ):
                raise ValueError("received list boundary is newer than the displayed time")
            valid_time = payload.view_as_of <= datetime.now(UTC) + timedelta(seconds=10)
        if not valid_time or source["available_at"] > payload.view_as_of:
            raise ValueError("source observation unavailable at the displayed time")
        band, reasons = explain_attention(
            alarm=source["alarm"],
            sensor_type=source["sensor_type"],
            value_raw=source["value_raw"],
        )
        latest = connection.execute(
            """SELECT COALESCE(max(revision), 0) AS revision
               FROM replay_review_notes
               WHERE namespace_id = %s AND snapshot_id = %s AND row_uid = %s""",
            scope,
        ).fetchone()["revision"]
        if latest != payload.expected_revision:
            raise RevisionConflict(
                f"expected revision {payload.expected_revision}, current {latest}"
            )
        position = connection.execute(
            """UPDATE dispatch_replay_snapshots
               SET review_count = review_count + 1
               WHERE namespace_id = %s AND snapshot_id = %s AND scope_kind = %s
               RETURNING review_count""",
            (namespace_id, snapshot_id, mode),
        ).fetchone()
        if position is None:
            raise LookupError("review scope not found")
        note_id = uuid4()
        note = connection.execute(
            """INSERT INTO replay_review_notes
               (note_id, namespace_id, snapshot_id, row_uid, revision,
                idempotency_key, payload_sha256, actor_id,
                action_text, result_text, reason_text,
                view_as_of, displayed_received_watermark,
                policy_version, attention_band, reason_codes,
                review_position)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s)
               RETURNING note_id, row_uid, revision, actor_id,
                         action_text, result_text, reason_text, created_at,
                         view_as_of, displayed_received_watermark,
                         policy_version, attention_band, reason_codes""",
            (
                note_id,
                *scope,
                latest + 1,
                payload.idempotency_key,
                digest,
                actor_id,
                payload.action_text,
                payload.result_text,
                payload.reason_text,
                payload.view_as_of,
                payload.displayed_received_watermark,
                POLICY_VERSION,
                band,
                Jsonb(reasons),
                position["review_count"],
            ),
        ).fetchone()
        connection.execute(
            """INSERT INTO replay_review_audit
               (audit_id, note_id, namespace_id, snapshot_id, row_uid,
                actor_id, action_kind, payload_sha256)
               VALUES (%s, %s, %s, %s, %s, %s, 'review_note_created', %s)""",
            (uuid4(), note_id, *scope, actor_id, digest),
        )
    return ReviewNote.model_validate(note)
