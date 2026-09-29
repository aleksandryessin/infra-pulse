"""Lazy, event-grain view of an accepted research snapshot.

The view contains no target or model features. Split reservations are metadata
for later point-in-time builders, not row-wise random train/test assignments.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import duckdb


def _quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def window_crosses_policy_exclusion(
    rules: list[dict], *, sensor_type: str, start: dt.date, end: dt.date
) -> bool:
    """Whether a half-open date window crosses a sensor-applicable overlay barrier.

    Future labels also need the base source-coverage and split-boundary checks.
    """
    if start >= end:
        raise ValueError("window must be nonempty")
    return any(
        (rule["sensor_type"] is None or rule["sensor_type"] == sensor_type)
        and start < dt.date.fromisoformat(rule["end"])
        and dt.date.fromisoformat(rule["start"]) < end
        for rule in rules
    )


def open_prepared(
    snapshot: Path,
    *,
    policy_manifest: Path | None = None,
    threads: int = 2,
    memory_limit: str = "4GB",
) -> duckdb.DuckDBPyConnection:
    snapshot = Path(snapshot).resolve()
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest["publication_status"] != "accepted":
        raise ValueError("snapshot is not an accepted ETL publication")
    if not (snapshot / "curated").is_dir():
        raise FileNotFoundError(snapshot / "curated")
    split_cases = []
    for name, (start, end) in manifest["split_reservations"].items():
        split_cases.append(
            f"WHEN local_day >= DATE {_quote(start)} AND local_day < DATE {_quote(end)} "
            f"THEN {_quote(name)}"
        )
    con = duckdb.connect()
    con.execute(f"SET threads={int(threads)}")
    con.execute(f"SET memory_limit={_quote(memory_limit)}")
    files = snapshot / "curated/month=*/events.parquet"
    con.execute(f"""
        CREATE VIEW prepared_events AS
        WITH source AS (
          SELECT *, TRY_CAST(substr(event_ts_local_raw, 1, 10) AS DATE) AS local_day
          FROM read_parquet({_quote(files)}, union_by_name=true)
        )
        SELECT * EXCLUDE (local_day),
          CASE WHEN is_epoch_placeholder THEN 'temporal_unusable'
               {" ".join(split_cases)} ELSE 'outside_reservation' END AS split_reservation
        FROM source
    """)
    if policy_manifest is not None:
        policy_path = Path(policy_manifest).resolve()
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        with (snapshot / "manifest.json").open("rb") as source:
            actual_hash = hashlib.file_digest(source, "sha256").hexdigest()
        if policy["base_manifest_sha256"] != actual_hash:
            con.close()
            raise ValueError("policy overlay targets a different prepared snapshot")
        if policy["status"] != "accepted_research_overlay_not_training_approval":
            con.close()
            raise ValueError("policy overlay was not published")
        checks = []
        for rule in policy["rules"]:
            sensor_check = (
                ""
                if rule["sensor_type"] is None
                else f" AND COALESCE(sensor_type = {_quote(rule['sensor_type'])}, false)"
            )
            checks.append(
                "COALESCE(("
                f"local_day >= DATE {_quote(rule['start'])} "
                f"AND local_day < DATE {_quote(rule['end'])}{sensor_check}"
                "), false)"
            )
        predicate = " OR ".join(checks) if checks else "false"
        con.execute(f"""
            CREATE VIEW prepared_events_policy AS
            SELECT * EXCLUDE (local_day), ({predicate}) AS training_policy_excluded
            FROM (
              SELECT *, TRY_CAST(substr(event_ts_local_raw, 1, 10) AS DATE) AS local_day
              FROM prepared_events
            )
        """)
        con.execute(
            "CREATE VIEW working_events AS SELECT * FROM prepared_events_policy "
            "WHERE NOT training_policy_excluded"
        )
        con.execute(
            "CREATE VIEW policy_excluded_events AS SELECT * FROM prepared_events_policy "
            "WHERE training_policy_excluded"
        )
        con.execute(
            f"CREATE VIEW policy_days AS SELECT * FROM read_parquet("
            f"{_quote(policy_path.parent / 'policy_days.parquet')})"
        )
    return con
