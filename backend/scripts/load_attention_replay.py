"""Load a bounded accepted curated window into PostgreSQL for historical replay.

Run separately from HTTP with the data and platform dependency groups. Historical
delivery time is unavailable: replay availability is explicitly simulated as
the source event time. This script never mutates the curated snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import psycopg
from psycopg.types.json import Jsonb

MSK = ZoneInfo("Europe/Moscow")
MIGRATION_DIR = Path(__file__).resolve().parents[1] / "migrations"

COLUMNS = (
    "row_uid",
    "event_id",
    "channel_id",
    "object_id",
    "sensor_type",
    "system_type",
    "value_raw",
    "value_numeric",
    "alarm",
    "event_ts_utc",
    "source_file",
    "source_sha256",
    "record_ordinal",
    "event_ts_local_raw",
    "reference_version",
    "is_epoch_placeholder",
    "sentinel_candidate",
    "rare_lexeme",
    "ts_group_distinct_values",
    "reference_status",
)

INSERT = """
INSERT INTO dispatch_observations (
    namespace_id, snapshot_id, row_uid, source_event_id, channel_id,
    object_id, sensor_type, system_type, value_raw, value_numeric,
    alarm, event_at, available_at, availability_basis, source_file,
    source_sha256, record_ordinal, event_local_raw, reference_version,
    quality_flags
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s
)
ON CONFLICT (namespace_id, snapshot_id, row_uid) DO NOTHING
"""

ROSTER_INSERT = """
INSERT INTO dispatch_replay_channel_roster
    (namespace_id, snapshot_id, channel_id, object_id, system_type,
     sensor_type, reference_sha256)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (namespace_id, snapshot_id, channel_id) DO NOTHING
"""


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("window bounds must include a timezone")
    return parsed.astimezone(UTC)


def replay_identity(manifest_bytes: bytes, month: str, start: datetime, end: datetime) -> tuple:
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    key = f"{manifest_sha}|{month}|{start.isoformat()}|{end.isoformat()}"
    return manifest_sha, hashlib.sha256(key.encode()).hexdigest()


def row_for_insert(row: tuple, namespace_id: str, snapshot_id: str) -> tuple:
    data = dict(zip(COLUMNS, row, strict=True))
    event_at = data["event_ts_utc"].replace(tzinfo=UTC)
    flags = []
    for source, label in (
        ("is_epoch_placeholder", "epoch_placeholder"),
        ("sentinel_candidate", "sentinel_candidate"),
        ("rare_lexeme", "rare_lexeme"),
    ):
        if data[source]:
            flags.append(label)
    if (data["ts_group_distinct_values"] or 0) > 1:
        flags.append("same_timestamp_conflict")
    if data["reference_status"] != "matched":
        flags.append("reference_unmatched")
    numeric = data["value_numeric"]
    if numeric is not None and not math.isfinite(numeric):
        flags.append("nonfinite_numeric")
        numeric = None
    return (
        namespace_id,
        snapshot_id,
        data["row_uid"],
        str(data["event_id"]) if data["event_id"] is not None else None,
        str(data["channel_id"]),
        str(data["object_id"]) if data["object_id"] is not None else None,
        data["sensor_type"],
        data["system_type"],
        data["value_raw"],
        numeric,
        data["alarm"],
        event_at,
        event_at,  # simulated replay availability, never observed delivery
        "simulated",
        data["source_file"],
        data["source_sha256"],
        data["record_ordinal"],
        data["event_ts_local_raw"],
        data["reference_version"],
        Jsonb(flags),
    )


def load(
    snapshot: Path,
    *,
    month: str,
    start: datetime,
    end: datetime,
    dsn: str,
    namespace_id: str = "local-replay",
    batch_size: int = 1000,
) -> dict:
    if not start < end or batch_size < 1:
        raise ValueError("invalid replay window or batch size")
    if (
        start.astimezone(MSK).strftime("%Y-%m") != month
        or (end - timedelta(microseconds=1)).astimezone(MSK).strftime("%Y-%m") != month
    ):
        raise ValueError("window must fall within the chosen curated local month")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    manifest_bytes = (snapshot / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("publication_status") != "accepted":
        raise ValueError("only an accepted curated snapshot may be loaded")
    source = snapshot / "curated" / f"month={month}" / "events.parquet"
    if not source.is_file():
        raise FileNotFoundError(source)
    relative_source = f"curated/month={month}/events.parquet"
    expected_sha = manifest.get("output_sha256", {}).get(relative_source)
    if expected_sha is None:
        raise ValueError("curated month hash missing from accepted manifest")
    with source.open("rb") as stream:
        actual_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual_sha != expected_sha:
        raise ValueError("curated month hash differs from accepted manifest")
    reference_source = snapshot / "channels.parquet"
    expected_reference_sha = manifest.get("output_sha256", {}).get("channels.parquet")
    if expected_reference_sha is None or not reference_source.is_file():
        raise ValueError("accepted channel reference is missing")
    with reference_source.open("rb") as stream:
        actual_reference_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual_reference_sha != expected_reference_sha:
        raise ValueError("channel reference hash differs from accepted manifest")
    manifest_sha, snapshot_id = replay_identity(manifest_bytes, month, start, end)

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='1GB'")
    columns = ", ".join(COLUMNS)
    result = con.execute(
        f"""SELECT {columns} FROM read_parquet(?)
            WHERE event_ts_utc >= ? AND event_ts_utc < ?
            ORDER BY event_ts_utc, row_uid""",
        [str(source), start.replace(tzinfo=None), end.replace(tzinfo=None)],
    )
    source_count = 0
    source_alarms = 0
    roster_source_count = 0
    try:
        with psycopg.connect(dsn) as connection:
            # Migrations are idempotent; the importer never drops tables.
            for migration in sorted(MIGRATION_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                connection.execute(migration.read_text(encoding="utf-8"))
            connection.execute(
                """INSERT INTO dispatch_replay_snapshots
                   (namespace_id, snapshot_id, manifest_sha256, window_start,
                    window_end, row_count, alarm_count)
                   VALUES (%s, %s, %s, %s, %s, 0, 0)
                   ON CONFLICT (namespace_id, snapshot_id) DO NOTHING""",
                (namespace_id, snapshot_id, manifest_sha, start, end),
            )
            existing = connection.execute(
                """SELECT manifest_sha256, window_start, window_end
                   FROM dispatch_replay_snapshots
                   WHERE namespace_id = %s AND snapshot_id = %s FOR UPDATE""",
                (namespace_id, snapshot_id),
            ).fetchone()
            if existing != (manifest_sha, start, end):
                raise ValueError("replay snapshot identity collision")
            connection.execute(
                """CREATE TEMP TABLE expected_replay_alarms
                   (row_uid text PRIMARY KEY) ON COMMIT DROP"""
            )
            with connection.cursor() as cursor:
                while rows := result.fetchmany(batch_size):
                    payload = [row_for_insert(row, namespace_id, snapshot_id) for row in rows]
                    source_count += len(payload)
                    source_alarms += sum(bool(row[10]) for row in payload)
                    cursor.executemany(INSERT, payload)
                    cursor.executemany(
                        """INSERT INTO expected_replay_alarms (row_uid)
                           VALUES (%s) ON CONFLICT DO NOTHING""",
                        [(row[2],) for row in payload if row[10]],
                    )
            loaded_count, loaded_alarms = connection.execute(
                """SELECT count(*), count(*) FILTER (WHERE alarm)
                   FROM dispatch_observations
                   WHERE namespace_id = %s AND snapshot_id = %s""",
                (namespace_id, snapshot_id),
            ).fetchone()
            if (loaded_count, loaded_alarms) != (source_count, source_alarms):
                raise ValueError("source/replay count mismatch; transaction rolled back")
            alarm_mismatch = connection.execute(
                """SELECT EXISTS (
                     (SELECT row_uid FROM expected_replay_alarms
                      EXCEPT SELECT row_uid FROM dispatch_observations
                      WHERE namespace_id = %s AND snapshot_id = %s AND alarm)
                     UNION ALL
                     (SELECT row_uid FROM dispatch_observations
                      WHERE namespace_id = %s AND snapshot_id = %s AND alarm
                      EXCEPT SELECT row_uid FROM expected_replay_alarms)
                   )""",
                (namespace_id, snapshot_id, namespace_id, snapshot_id),
            ).fetchone()[0]
            if alarm_mismatch:
                raise ValueError("source/replay alarm IDs differ; transaction rolled back")
            roster = con.execute(
                """SELECT channel_id, object_id, system_type, sensor_type
                   FROM read_parquet(?) ORDER BY channel_id""",
                [str(reference_source)],
            )
            with connection.cursor() as cursor:
                while rows := roster.fetchmany(batch_size):
                    payload = [
                        (
                            namespace_id,
                            snapshot_id,
                            str(channel_id),
                            str(object_id) if object_id is not None else None,
                            system_type,
                            sensor_type,
                            actual_reference_sha,
                        )
                        for channel_id, object_id, system_type, sensor_type in rows
                    ]
                    roster_source_count += len(payload)
                    cursor.executemany(ROSTER_INSERT, payload)
            loaded_roster_count = connection.execute(
                """SELECT count(*) FROM dispatch_replay_channel_roster
                   WHERE namespace_id = %s AND snapshot_id = %s
                     AND reference_sha256 = %s""",
                (namespace_id, snapshot_id, actual_reference_sha),
            ).fetchone()[0]
            if not roster_source_count or loaded_roster_count != roster_source_count:
                raise ValueError("source/replay roster mismatch; transaction rolled back")
            connection.execute(
                """UPDATE dispatch_replay_snapshots
                   SET row_count = %s, alarm_count = %s
                   WHERE namespace_id = %s AND snapshot_id = %s""",
                (loaded_count, loaded_alarms, namespace_id, snapshot_id),
            )
    finally:
        con.close()
    return {
        "namespace_id": namespace_id,
        "snapshot_id": snapshot_id,
        "manifest_sha256": manifest_sha,
        "source_file_sha256": actual_sha,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "source_rows": source_count,
        "loaded_rows": loaded_count,
        "source_alarms": source_alarms,
        "loaded_alarms": loaded_alarms,
        "reference_sha256": actual_reference_sha,
        "roster_channels": loaded_roster_count,
        "availability_basis": "simulated_event_time",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--start", required=True, help="aware ISO timestamp, inclusive")
    parser.add_argument("--end", required=True, help="aware ISO timestamp, exclusive")
    parser.add_argument("--namespace", default="local-replay")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    dsn = os.environ.get("INFRA_REPLAY_DSN")
    if not dsn:
        parser.error("INFRA_REPLAY_DSN is required")
    report = load(
        args.snapshot,
        month=args.month,
        start=parse_utc(args.start),
        end=parse_utc(args.end),
        dsn=dsn,
        namespace_id=args.namespace,
        batch_size=args.batch_size,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
