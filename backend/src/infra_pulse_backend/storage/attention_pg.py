"""Bounded PostgreSQL reads for locally available dispatcher observations."""

from datetime import datetime
from typing import Literal

import psycopg
from psycopg.rows import dict_row

from infra_pulse_backend.operations.attention import POLICY_VERSION, WATCH_PAIRS
from infra_pulse_core.contracts.attention import (
    AttentionEntry,
    AttentionList,
    ChannelAttentionList,
    ChannelAttentionSummary,
    ObjectAttentionList,
    ObjectAttentionSummary,
    ObservedMessage,
    SourceAlarmObject,
    SourceAlarmWindow,
)

WATCH_CLAUSE = "(sensor_type, value_raw) IN (" + ", ".join(["(%s, %s)"] * len(WATCH_PAIRS)) + ")"
WATCH_PARAMS = tuple(value for pair in WATCH_PAIRS for value in pair)


def read_channel_pickets(connection: psycopg.Connection, channel_ids: list[str]) -> dict:
    """Picket, name and line group of each channel from the channel layout (migration 0013,
    B2; name and group — F6): channels without a layout row keep ``unknown`` and no name."""
    if not channel_ids:
        return {}
    exists = connection.execute(
        "SELECT to_regclass('forecast_channel_layout') IS NOT NULL AS found"
    ).fetchone()
    if not (exists["found"] if isinstance(exists, dict) else exists[0]):
        return {}
    rows = connection.execute(
        """SELECT channel_id, picket_form, picket_from, picket_to, picket_basis, name,
                  feeder_kind
           FROM forecast_channel_layout
           WHERE channel_id = ANY(%s)""",
        (list(set(channel_ids)),),
    ).fetchall()
    out = {}
    for row in rows:
        values = (
            row
            if isinstance(row, dict)
            else dict(
                zip(
                    (
                        "channel_id",
                        "picket_form",
                        "picket_from",
                        "picket_to",
                        "picket_basis",
                        "name",
                        "feeder_kind",
                    ),
                    row,
                    strict=True,
                )
            )
        )
        known = {"channel_name": values["name"], "feeder_kind": values["feeder_kind"]}
        if values["picket_form"] != "unknown":
            known.update(
                picket_form=values["picket_form"],
                picket_from=values["picket_from"],
                picket_to=values["picket_to"] if values["picket_form"] == "range" else None,
                picket_basis=values["picket_basis"],
            )
        out[values["channel_id"]] = known
    return out


def _table_exists(connection: psycopg.Connection, name: str) -> bool:
    found = connection.execute("SELECT to_regclass(%s) IS NOT NULL AS found", (name,)).fetchone()
    return bool(found["found"] if isinstance(found, dict) else found[0])


def read_object_names(connection: psycopg.Connection, object_ids: list) -> dict[str, str]:
    """Dispatcher names of objects (C0.4): the forecast object table (B2) or, without it,
    the active objects reference (B1). Unknown objects stay without a name."""
    ids = sorted({object_id for object_id in object_ids if object_id is not None})
    if not ids:
        return {}
    if _table_exists(connection, "forecast_objects"):
        rows = connection.execute(
            """SELECT object_id, object_name FROM forecast_objects
               WHERE object_id = ANY(%s) AND object_name IS NOT NULL""",
            (ids,),
        ).fetchall()
    elif _table_exists(connection, "ref_objects"):
        rows = connection.execute(
            """SELECT o.object_id, o.name FROM ref_objects AS o
               WHERE o.object_id = ANY(%s) AND o.version_id = (
                 SELECT version_id FROM ref_versions WHERE kind = 'objects'
                 ORDER BY activation_seq DESC LIMIT 1)""",
            (ids,),
        ).fetchall()
    else:
        return {}
    pairs = (tuple(row.values()) if isinstance(row, dict) else tuple(row) for row in rows)
    return {object_id: name for object_id, name in pairs}


def read_attention_page(
    dsn: str,
    *,
    namespace_id: str,
    snapshot_id: str,
    as_of: datetime,
    mode: Literal["replay", "received"] = "replay",
    offset: int,
    limit: int,
    object_id: str | None = None,
    channel_id: str | None = None,
    row_uid: str | None = None,
    view: Literal["attention", "all"] = "attention",
    local_note: Literal["any", "present", "absent"] = "any",
    alarm: bool | None = None,
    received_watermark: int | None = None,
    after_received_watermark: int | None = None,
    event_at: datetime | None = None,
    event_from: datetime | None = None,
) -> AttentionList:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if event_from is not None and (event_from.tzinfo is None or event_from.utcoffset() is None):
        raise ValueError("event_from must be timezone-aware")
    if event_at is not None and (event_at.tzinfo is None or event_at.utcoffset() is None):
        raise ValueError("event_at must be timezone-aware")
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("invalid attention page")
    if local_note not in ("any", "present", "absent"):
        raise ValueError("invalid local note filter")

    if received_watermark is not None and (mode != "received" or received_watermark < 0):
        raise ValueError("invalid received watermark")
    if after_received_watermark is not None and (
        mode != "received" or after_received_watermark < 0
    ):
        raise ValueError("invalid received lower watermark")
    scope = (namespace_id, snapshot_id, as_of)
    object_clause = ""
    if object_id == "__unknown__":
        object_clause = " AND object_id IS NULL"
    elif object_id is not None:
        object_clause = " AND object_id = %s"
        scope = (*scope, object_id)
    if channel_id is not None:
        object_clause += " AND channel_id = %s"
        scope = (*scope, channel_id)
    if row_uid is not None:
        object_clause += " AND row_uid = %s"
        scope = (*scope, row_uid)
    if alarm is not None:
        object_clause += " AND alarm = %s"
        scope = (*scope, alarm)
    if event_at is not None:
        object_clause += " AND event_at = %s"
        scope = (*scope, event_at)
    if event_from is not None:
        # «Сейчас» (F6): только записи за окно до среза, total — в этом окне.
        object_clause += " AND event_at >= %s"
        scope = (*scope, event_from)
    note_exists = """EXISTS (
        SELECT 1 FROM replay_review_notes AS note
        WHERE note.namespace_id = dispatch_observations.namespace_id
          AND note.snapshot_id = dispatch_observations.snapshot_id
          AND note.row_uid = dispatch_observations.row_uid
    )"""
    note_clause = (
        f" AND {note_exists}"
        if local_note == "present"
        else f" AND NOT {note_exists}"
        if local_note == "absent"
        else ""
    )
    lane = f"CASE WHEN alarm THEN 0 WHEN {WATCH_CLAUSE} THEN 1 ELSE 2 END"
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = connection.execute(
            """SELECT row_count FROM dispatch_replay_snapshots
               WHERE namespace_id = %s AND snapshot_id = %s AND scope_kind = %s""",
            (namespace_id, snapshot_id, mode),
        ).fetchone()
        if snapshot is None:
            raise LookupError("replay snapshot not loaded")
        effective_watermark = None
        received_clause = ""
        if mode == "received":
            if received_watermark is not None and received_watermark > snapshot["row_count"]:
                raise ValueError("received watermark exceeds committed rows")
            effective_watermark = (
                snapshot["row_count"] if received_watermark is None else received_watermark
            )
            received_clause = " AND received_position <= %s"
            bounds = (effective_watermark,)
            if after_received_watermark is not None:
                if after_received_watermark > effective_watermark:
                    raise ValueError("received lower watermark exceeds committed upper watermark")
                received_clause += " AND received_position > %s"
                bounds = (*bounds, after_received_watermark)
            scope = (namespace_id, snapshot_id, as_of, *bounds, *scope[3:])
        view_clause = " AND (alarm OR " + WATCH_CLAUSE + ")" if view == "attention" else ""
        view_params = WATCH_PARAMS if view == "attention" else ()
        filtered_clause = f"{received_clause}{object_clause}{view_clause}{note_clause}"
        scoped = f"""FROM dispatch_observations
                WHERE namespace_id = %s AND snapshot_id = %s
                  AND available_at <= %s{received_clause}{object_clause}"""
        if local_note == "any":
            # Audit 29.09: every count of the page in one pass over the scope, by the band
            # of each record (0 source alarm, 1 text candidate, 2 other), instead of one
            # pass for `all_records_total` and another for the page counts.
            bands = {
                row["lane"]: row["n"]
                for row in connection.execute(
                    f"SELECT {lane} AS lane, count(*) AS n {scoped} GROUP BY 1",
                    (*WATCH_PARAMS, *scope),
                ).fetchall()
            }
            all_records_total = sum(bands.values())
            counts = {
                "source_alarm_count": bands.get(0, 0),
                "watch_text_count": bands.get(1, 0),
            }
            count = (
                all_records_total
                if view == "all"
                else counts["source_alarm_count"] + counts["watch_text_count"]
            )
        else:
            # The note filter is checked only for the records of the view.
            all_records_total = connection.execute(
                f"SELECT count(*) AS n {scoped}", scope
            ).fetchone()["n"]
            counts = connection.execute(
                f"""SELECT count(*) AS n,
                           count(*) FILTER (WHERE alarm) AS source_alarm_count,
                           count(*) FILTER (WHERE alarm IS NOT TRUE AND {WATCH_CLAUSE})
                             AS watch_text_count
                    FROM dispatch_observations
                    WHERE namespace_id = %s AND snapshot_id = %s
                      AND available_at <= %s{filtered_clause}""",
                (*WATCH_PARAMS, *scope, *view_params),
            ).fetchone()
            count = counts["n"]
        columns = f"""row_uid, source_event_id, channel_id, object_id,
                      sensor_type, system_type, value_raw, value_numeric,
                      alarm, event_at, available_at, availability_basis,
                      namespace_id AS source_namespace, snapshot_id,
                      source_file, source_sha256, record_ordinal,
                      reference_version, quality_flags, {lane} AS lane"""
        if view == "all" and not object_clause:
            # Whole scope in receipt order: one ordered index range per value of the source
            # flag (queue index: alarm, available_at DESC, row_uid), merged, instead of
            # sorting every record of the scope for one page.
            branches = " UNION ALL ".join(
                f"""(SELECT {columns} FROM dispatch_observations
                   WHERE namespace_id = %s AND snapshot_id = %s
                     AND available_at <= %s{filtered_clause} AND {flag}
                   ORDER BY available_at DESC, row_uid LIMIT %s)"""
                for flag in ("alarm", "NOT alarm", "alarm IS NULL")
            )
            rows = connection.execute(
                f"""SELECT * FROM ({branches}) AS page
                   ORDER BY available_at DESC, row_uid
                   LIMIT %s OFFSET %s""",
                (*((*WATCH_PARAMS, *scope, offset + limit) * 3), limit, offset),
            ).fetchall()
        else:
            order_clause = (
                "lane, available_at DESC, namespace_id, snapshot_id, row_uid"
                if view == "attention"
                else "available_at DESC, namespace_id, snapshot_id, row_uid"
            )
            rows = connection.execute(
                f"""SELECT {columns}
                   FROM dispatch_observations
                   WHERE namespace_id = %s AND snapshot_id = %s
                     AND available_at <= %s{filtered_clause}
                   ORDER BY {order_clause}
                   LIMIT %s OFFSET %s""",
                (*WATCH_PARAMS, *scope, *view_params, limit, offset),
            ).fetchall()
        latest_reviews = {}
        if rows:
            reviews = connection.execute(
                """SELECT DISTINCT ON (row_uid) row_uid, revision, created_at
                   FROM replay_review_notes
                   WHERE namespace_id = %s AND snapshot_id = %s
                     AND row_uid = ANY(%s)
                   ORDER BY row_uid, revision DESC""",
                (namespace_id, snapshot_id, [row["row_uid"] for row in rows]),
            ).fetchall()
            latest_reviews = {row["row_uid"]: row for row in reviews}
        pickets = read_channel_pickets(connection, [row["channel_id"] for row in rows])
        names = read_object_names(connection, [row["object_id"] for row in rows])

    entries = []
    for index, row in enumerate(rows, start=offset + 1):
        lane = row.pop("lane")
        message = ObservedMessage.model_validate(
            {
                **row,
                **pickets.get(row["channel_id"], {}),
                "object_name": names.get(row["object_id"]),
            }
        )
        band = ("source_alarm", "watch_text", "chronological")[lane]
        entries.append(
            AttentionEntry(
                message=message,
                review_order=index,
                attention_band=band,
                reason_codes=["source_alarm_true", "received_order"]
                if band == "source_alarm"
                else ["exact_text_candidate", "received_order"]
                if band == "watch_text"
                else ["received_order"],
                policy_version=POLICY_VERSION,
                local_review_revision=latest_reviews.get(message.row_uid, {}).get("revision", 0),
                local_last_review_at=latest_reviews.get(message.row_uid, {}).get("created_at"),
            )
        )
    return AttentionList(
        mode=mode,
        view=view,
        as_of=as_of,
        received_watermark=effective_watermark,
        received_after_watermark=after_received_watermark,
        policy_version=POLICY_VERSION,
        items=entries,
        offset=offset,
        total=count,
        all_records_total=all_records_total,
        source_alarm_count=counts["source_alarm_count"],
        watch_text_count=counts["watch_text_count"],
        chronological_count=count - counts["source_alarm_count"] - counts["watch_text_count"],
    )


MESSAGE_COLUMNS = """row_uid, source_event_id, channel_id, object_id,
       sensor_type, system_type, value_raw, value_numeric,
       alarm, event_at, available_at, availability_basis,
       namespace_id AS source_namespace, snapshot_id,
       source_file, source_sha256, record_ordinal,
       reference_version, quality_flags"""


def read_source_alarm_window(
    dsn: str,
    *,
    namespace_id: str,
    snapshot_id: str,
    as_of: datetime,
    event_from: datetime,
    mode: Literal["replay", "received"] = "replay",
    received_watermark: int | None = None,
    latest_limit: int = 10,
    object_limit: int = 100,
) -> SourceAlarmWindow:
    """«Сейчас» (F-04, audit 29.09): source alarms with event time from ``event_from``.

    Server aggregates over the whole window — records, objects, lines and records on the
    lines of the object's scheme — plus the latest records by event time. The browser no
    longer pages the window by receipt time: a file received later with older events
    pushed the newest events of the seeded history out of the first thousand records.
    """
    for name, value in (("as_of", as_of), ("event_from", event_from)):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
    if received_watermark is not None and (mode != "received" or received_watermark < 0):
        raise ValueError("invalid received watermark")
    if not 1 <= latest_limit <= 50 or not 1 <= object_limit <= 500:
        raise ValueError("invalid source alarm window page")
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = connection.execute(
            """SELECT row_count FROM dispatch_replay_snapshots
               WHERE namespace_id = %s AND snapshot_id = %s AND scope_kind = %s""",
            (namespace_id, snapshot_id, mode),
        ).fetchone()
        if snapshot is None:
            raise LookupError("replay snapshot not loaded")
        effective_watermark = None
        received_clause = ""
        scope: tuple = (namespace_id, snapshot_id, as_of, event_from)
        if mode == "received":
            if received_watermark is not None and received_watermark > snapshot["row_count"]:
                raise ValueError("received watermark exceeds committed rows")
            effective_watermark = (
                snapshot["row_count"] if received_watermark is None else received_watermark
            )
            received_clause = " AND observation.received_position <= %s"
            scope = (*scope, effective_watermark)
        # Window by event time: the event-time index (0020) reads only the window.
        window = f"""observation.namespace_id = %s AND observation.snapshot_id = %s
                 AND observation.available_at <= %s AND observation.alarm
                 AND observation.event_at >= %s{received_clause}"""
        # A record is on the scheme when its channel is a power line (feeder) of the same
        # object in the channel layout and the forecast detector knows it — the rule of
        # the scheme (`api/forecast_db._object_layout`).
        scheme_line = (
            """EXISTS (
                 SELECT 1 FROM forecast_channel_layout AS line
                 JOIN forecast_detector_state AS known
                   ON known.namespace_id = observation.namespace_id
                  AND known.snapshot_id = observation.snapshot_id
                  AND known.channel_id = line.channel_id
                 WHERE line.channel_id = observation.channel_id
                   AND line.object_id = observation.object_id
                   AND line.role = 'feeder')"""
            if _table_exists(connection, "forecast_channel_layout")
            and _table_exists(connection, "forecast_detector_state")
            else "FALSE"
        )
        groups = connection.execute(
            f"""SELECT observation.object_id,
                       count(*) AS record_count,
                       count(DISTINCT observation.channel_id) AS channel_count,
                       count(*) FILTER (WHERE {scheme_line}) AS scheme_record_count,
                       min(observation.event_at) AS first_event_at,
                       max(observation.event_at) AS last_event_at
                FROM dispatch_observations AS observation
                WHERE {window}
                GROUP BY observation.object_id
                ORDER BY max(observation.event_at) DESC, observation.object_id NULLS LAST""",
            scope,
        ).fetchall()
        objects = [group for group in groups if group["object_id"] is not None]
        shown = objects[:object_limit]
        last_messages = (
            connection.execute(
                f"""SELECT DISTINCT ON (observation.object_id) {MESSAGE_COLUMNS}
                    FROM dispatch_observations AS observation
                    WHERE {window} AND observation.object_id = ANY(%s)
                    ORDER BY observation.object_id, observation.event_at DESC,
                             observation.row_uid DESC""",
                (*scope, [group["object_id"] for group in shown]),
            ).fetchall()
            if shown
            else []
        )
        latest = connection.execute(
            f"""SELECT {MESSAGE_COLUMNS}
                FROM dispatch_observations AS observation
                WHERE {window}
                ORDER BY observation.event_at DESC, observation.row_uid DESC
                LIMIT %s""",
            (*scope, latest_limit),
        ).fetchall()
        rows = [*last_messages, *latest]
        pickets = read_channel_pickets(connection, [row["channel_id"] for row in rows])
        names = read_object_names(connection, [row["object_id"] for row in rows])

    def message(row: dict) -> ObservedMessage:
        return ObservedMessage.model_validate(
            {
                **row,
                **pickets.get(row["channel_id"], {}),
                "object_name": names.get(row["object_id"]),
            }
        )

    last_by_object = {row["object_id"]: message(row) for row in last_messages}
    return SourceAlarmWindow(
        mode=mode,
        as_of=as_of,
        received_watermark=effective_watermark,
        event_from=event_from,
        record_count=sum(group["record_count"] for group in groups),
        object_count=len(objects),
        without_object_count=sum(
            group["record_count"] for group in groups if group["object_id"] is None
        ),
        objects=[
            SourceAlarmObject(
                **group,
                object_name=names.get(group["object_id"]),
                last_message=last_by_object[group["object_id"]],
            )
            for group in shown
        ],
        latest=[message(row) for row in latest],
    )


def read_object_summaries(
    dsn: str,
    *,
    namespace_id: str,
    snapshot_id: str,
    as_of: datetime,
    mode: Literal["replay", "received"] = "replay",
    received_watermark: int | None = None,
    candidate_kind: Literal["all", "alarm", "text", "candidate"] = "all",
    offset: int = 0,
    limit: int = 25,
) -> ObjectAttentionList:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if received_watermark is not None and (mode != "received" or received_watermark < 0):
        raise ValueError("invalid received watermark")
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("invalid object page")
    object_filters = {
        "all": "TRUE",
        "alarm": "source_alarm_count > 0",
        "text": "candidate_count > source_alarm_count",
        "candidate": "candidate_count > 0",
    }
    if candidate_kind not in object_filters:
        raise ValueError("invalid object candidate filter")
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = connection.execute(
            """SELECT row_count FROM dispatch_replay_snapshots
               WHERE namespace_id = %s AND snapshot_id = %s AND scope_kind = %s""",
            (namespace_id, snapshot_id, mode),
        ).fetchone()
        if snapshot is None:
            raise LookupError("replay snapshot not loaded")
        effective_watermark = None
        received_clause = ""
        scope = (namespace_id, snapshot_id, as_of)
        if mode == "received":
            if received_watermark is not None and received_watermark > snapshot["row_count"]:
                raise ValueError("received watermark exceeds committed rows")
            effective_watermark = (
                snapshot["row_count"] if received_watermark is None else received_watermark
            )
            received_clause = " AND received_position <= %s"
            scope = (*scope, effective_watermark)
        result = connection.execute(
            f"""WITH grouped AS MATERIALIZED (
                 SELECT object_id, count(DISTINCT channel_id) AS channel_count,
                        count(*) AS record_count,
                        count(*) FILTER (WHERE alarm OR {WATCH_CLAUSE}) AS candidate_count,
                        count(*) FILTER (WHERE alarm) AS source_alarm_count,
                        max(available_at) AS last_available_at,
                        max(available_at) FILTER (WHERE alarm) AS last_source_alarm_at,
                        max(available_at) FILTER (WHERE alarm IS NOT TRUE AND {WATCH_CLAUSE})
                          AS last_watch_text_at
                 FROM dispatch_observations
                 WHERE namespace_id = %s AND snapshot_id = %s
                   AND available_at <= %s{received_clause}
                 GROUP BY object_id
               ), filtered AS MATERIALIZED (
                 SELECT * FROM grouped WHERE {object_filters[candidate_kind]}
               ), page AS (
                 SELECT * FROM filtered
                 ORDER BY (candidate_count > 0) DESC,
                        GREATEST(last_source_alarm_at, last_watch_text_at) DESC NULLS LAST,
                        last_available_at DESC, object_id NULLS LAST
                 LIMIT %s OFFSET %s
               )
               SELECT (SELECT count(*) FROM filtered) AS total,
                      COALESCE((SELECT jsonb_agg(to_jsonb(page) ORDER BY
                        (page.candidate_count > 0) DESC,
                        GREATEST(page.last_source_alarm_at, page.last_watch_text_at)
                          DESC NULLS LAST,
                        page.last_available_at DESC, page.object_id NULLS LAST)
                        FROM page), '[]'::jsonb) AS items""",
            (*WATCH_PARAMS, *WATCH_PARAMS, *scope, limit, offset),
        ).fetchone()
    assert result is not None
    items = [
        ObjectAttentionSummary(
            **row,
            attention_band=(
                "source_alarm"
                if row["source_alarm_count"] > 0
                else "watch_text"
                if row["candidate_count"] > 0
                else "chronological"
            ),
        )
        for row in result["items"]
    ]
    return ObjectAttentionList(
        mode=mode,
        as_of=as_of,
        received_watermark=effective_watermark,
        items=items,
        total=result["total"],
        offset=offset,
        limit=limit,
        candidate_kind=candidate_kind,
    )


def read_channel_summaries(
    dsn: str,
    *,
    namespace_id: str,
    snapshot_id: str,
    as_of: datetime,
    object_id: str,
    channel_id: str | None = None,
    candidate_kind: Literal["all", "alarm", "text", "candidate"] = "all",
    mode: Literal["replay", "received"] = "replay",
    received_watermark: int | None = None,
    offset: int = 0,
    limit: int = 25,
) -> ChannelAttentionList:
    """Page by object/system/channel; the selected row is never a state estimate."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("invalid channel page")
    if received_watermark is not None and (mode != "received" or received_watermark < 0):
        raise ValueError("invalid received watermark")
    predicates = {
        "all": "TRUE",
        "alarm": "source_alarm_count > 0",
        "text": "candidate_count > source_alarm_count",
        "candidate": "candidate_count > 0",
    }
    if candidate_kind not in predicates:
        raise ValueError("invalid channel candidate kind")
    group_predicate = predicates[candidate_kind]
    object_clause = "object_id IS NULL" if object_id == "__unknown__" else "object_id = %s"
    channel_clause = " AND channel_id = %s" if channel_id is not None else ""
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        loaded = connection.execute(
            """SELECT row_count FROM dispatch_replay_snapshots
               WHERE namespace_id = %s AND snapshot_id = %s AND scope_kind = %s""",
            (namespace_id, snapshot_id, mode),
        ).fetchone()
        if loaded is None:
            raise LookupError("observation scope not loaded")
        effective_watermark = None
        received_clause = ""
        scope = (namespace_id, snapshot_id, as_of)
        if mode == "received":
            if received_watermark is not None and received_watermark > loaded["row_count"]:
                raise ValueError("received watermark exceeds committed rows")
            effective_watermark = (
                loaded["row_count"] if received_watermark is None else received_watermark
            )
            received_clause = " AND received_position <= %s"
            scope = (*scope, effective_watermark)
        if object_id != "__unknown__":
            scope = (*scope, object_id)
        if channel_id is not None:
            scope = (*scope, channel_id)
        total = connection.execute(
            f"""SELECT count(*) AS n FROM (
                 SELECT count(*) FILTER (WHERE alarm OR {WATCH_CLAUSE}) AS candidate_count,
                        count(*) FILTER (WHERE alarm) AS source_alarm_count
                 FROM dispatch_observations
                 WHERE namespace_id = %s AND snapshot_id = %s
                   AND available_at <= %s{received_clause} AND {object_clause}{channel_clause}
                 GROUP BY object_id, system_type, channel_id
               ) AS channels WHERE {group_predicate}""",
            (*WATCH_PARAMS, *scope),
        ).fetchone()["n"]
        rows = connection.execute(
            f"""WITH groups AS (
                 SELECT object_id, system_type, channel_id,
                        count(*) AS record_count,
                        count(*) FILTER (WHERE alarm OR {WATCH_CLAUSE}) AS candidate_count,
                        count(*) FILTER (WHERE alarm) AS source_alarm_count,
                        max(available_at) AS last_available_at,
                        max(available_at) FILTER (WHERE alarm) AS last_source_alarm_at,
                        max(available_at) FILTER (WHERE alarm IS NOT TRUE AND {WATCH_CLAUSE})
                          AS last_watch_text_at,
                        max(event_at) AS latest_source_event_at
                 FROM dispatch_observations
                 WHERE namespace_id = %s AND snapshot_id = %s
                   AND available_at <= %s{received_clause} AND {object_clause}{channel_clause}
                 GROUP BY object_id, system_type, channel_id
               ), page AS (
                 SELECT * FROM groups
                 WHERE {group_predicate}
                 ORDER BY (candidate_count > 0) DESC,
                          GREATEST(last_source_alarm_at, last_watch_text_at) DESC NULLS LAST,
                          last_available_at DESC,
                          system_type NULLS LAST, channel_id
                 LIMIT %s OFFSET %s
               )
               SELECT page.object_id, page.system_type, page.channel_id,
                      page.record_count, page.candidate_count, page.source_alarm_count,
                      page.latest_source_event_at, page.last_source_alarm_at,
                      page.last_watch_text_at,
                      binding.multiple_object_ids_seen,
                      selected.last_received_group_count,
                      selected.row_uid, selected.source_event_id,
                      selected.sensor_type, selected.value_raw, selected.value_numeric,
                      selected.alarm, selected.event_at, selected.available_at,
                      selected.availability_basis,
                      selected.namespace_id AS source_namespace, selected.snapshot_id,
                      selected.source_file, selected.source_sha256,
                      selected.record_ordinal, selected.reference_version,
                      selected.quality_flags
               FROM page
               JOIN LATERAL (
                 SELECT observation.*,
                        count(*) OVER () AS last_received_group_count
                 FROM dispatch_observations AS observation
                 WHERE observation.namespace_id = %s AND observation.snapshot_id = %s
                   AND observation.object_id IS NOT DISTINCT FROM page.object_id
                   AND observation.system_type IS NOT DISTINCT FROM page.system_type
                   AND observation.channel_id = page.channel_id
                   AND observation.available_at = page.last_available_at
                   {("AND observation.received_position <= %s" if mode == "received" else "")}
                 ORDER BY observation.event_at DESC, observation.row_uid DESC
                 LIMIT 1
               ) AS selected ON true
               JOIN LATERAL (
                 SELECT EXISTS (
                   SELECT 1 FROM dispatch_observations AS other
                   WHERE other.namespace_id = %s AND other.snapshot_id = %s
                     AND other.channel_id = page.channel_id
                     AND other.available_at <= %s
                     {("AND other.received_position <= %s" if mode == "received" else "")}
                     AND other.object_id IS DISTINCT FROM page.object_id
                 ) AS multiple_object_ids_seen
               ) AS binding ON true
               ORDER BY (page.candidate_count > 0) DESC,
                        GREATEST(page.last_source_alarm_at,
                                 page.last_watch_text_at) DESC NULLS LAST,
                        page.last_available_at DESC,
                        page.system_type NULLS LAST, page.channel_id""",
            (
                *WATCH_PARAMS,
                *WATCH_PARAMS,
                *scope,
                limit,
                offset,
                namespace_id,
                snapshot_id,
                *((effective_watermark,) if mode == "received" else ()),
                namespace_id,
                snapshot_id,
                as_of,
                *((effective_watermark,) if mode == "received" else ()),
            ),
        ).fetchall()
    items = []
    for row in rows:
        message = ObservedMessage.model_validate(
            {field: row[field] for field in ObservedMessage.model_fields if field in row}
        )
        items.append(
            ChannelAttentionSummary(
                object_id=row["object_id"],
                system_type=row["system_type"],
                channel_id=row["channel_id"],
                record_count=row["record_count"],
                candidate_count=row["candidate_count"],
                source_alarm_count=row["source_alarm_count"],
                last_source_alarm_at=row["last_source_alarm_at"],
                last_watch_text_at=row["last_watch_text_at"],
                last_received_message=message,
                last_received_group_count=row["last_received_group_count"],
                latest_source_event_at=row["latest_source_event_at"],
                source_time_regressed=message.event_at < row["latest_source_event_at"],
                multiple_object_ids_seen=row["multiple_object_ids_seen"],
            )
        )
    return ChannelAttentionList(
        mode=mode,
        as_of=as_of,
        received_watermark=effective_watermark,
        object_id=None if object_id == "__unknown__" else object_id,
        items=items,
        offset=offset,
        total=total,
    )
