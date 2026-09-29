"""Open the same current enriched event relation as notebook 08, cells 2–3."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb

from infra_pulse_research.data.prepared import open_prepared


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def open_fire_source(
    root: Path, *, policy_overlay: bool = True
) -> tuple[duckdb.DuckDBPyConnection, dict]:
    root = Path(root).resolve()
    snapshot = root / "data-science/artifacts/curated-all-sensors-exclude-2021-q2"
    policy_path = root / "data-science/artifacts/alarm-spike-exclusions-v1/manifest.json"
    states = root / "data-science/artifacts/state-candidates-v1"
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if manifest["etl_version"] != "journal-curated-v4":
        raise ValueError("unexpected curated version")
    if policy["policy_version"] != "alarm-spike-exclusions-v1":
        raise ValueError("unexpected overlay version")
    con = open_prepared(snapshot, policy_manifest=policy_path if policy_overlay else None)
    if not policy_overlay:
        con.execute("CREATE VIEW working_events AS SELECT * FROM prepared_events")
        con.execute("""
            CREATE VIEW policy_days AS
            SELECT NULL::DATE AS day, NULL::VARCHAR AS sensor_type_scope WHERE false
        """)
    con.execute(f"""
        CREATE VIEW current_channels AS
        SELECT channel_id, name_raw AS sensor_name_current,
               tag_raw AS engineering_system_tag_current
        FROM read_parquet({_q(snapshot / "channels.parquet")})
    """)
    con.execute(f"""
        CREATE VIEW current_objects AS
        SELECT TRY_CAST("ид_объект" AS BIGINT) AS object_id,
               TRY_CAST("иерархия_уровень" AS INTEGER) AS object_hierarchy_level,
               TRY_CAST("родитель" AS BIGINT) AS parent_object_id,
               "вид_объекта" AS object_kind,
               "диспетчерское_название_объекта" AS object_dispatch_name
        FROM read_parquet({_q(snapshot / "objects.parquet")})
    """)
    con.execute(f"""
        CREATE VIEW current_states AS
        SELECT s.sensor_type, s.state_name, MIN(s.candidate_rows) AS candidate_rows,
               MIN(s.candidate_set_count) AS candidate_set_count,
               BOOL_AND(s.reference_alarm_consensus) AS reference_alarm_consensus,
               BOOL_OR(s.reference_alarm_conflict) AS reference_alarm_conflict,
               CASE WHEN MIN(s.candidate_set_count)=1 THEN MIN(r.state_set_id) END
                    AS candidate_set_id
        FROM read_parquet({_q(states / "exact_text_candidates.parquet")}) s
        LEFT JOIN read_parquet({_q(states / "source_rows.parquet")}) r
          ON s.sensor_type=r.sensor_type AND s.state_name=r.state_name
        GROUP BY s.sensor_type, s.state_name
    """)
    for table, key in (("current_channels", "channel_id"), ("current_objects", "object_id")):
        total, distinct = con.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT {key}) FROM {table}"
        ).fetchone()
        if total != distinct:
            raise ValueError(f"ambiguous current reference key: {table}")
    con.execute("""
        CREATE VIEW current_enriched_events AS
        SELECT e.*, c.sensor_name_current, c.engineering_system_tag_current,
               o.object_hierarchy_level, o.parent_object_id,
               o.object_kind, o.object_dispatch_name,
               c.channel_id IS NOT NULL AS channel_ref_matched,
               o.object_id IS NOT NULL AS object_ref_matched,
               s.state_name IS NOT NULL AS state_ref_exact_match,
               s.candidate_rows AS state_ref_rows,
               s.candidate_set_count AS state_ref_set_count,
               s.candidate_set_id AS state_ref_candidate_set_id,
               s.reference_alarm_consensus AS state_ref_alarm_consensus,
               s.reference_alarm_conflict AS state_ref_alarm_conflict,
               'current_snapshot_no_validity_dates' AS reference_basis
        FROM working_events e
        LEFT JOIN current_channels c ON e.channel_id=c.channel_id
        LEFT JOIN current_objects o ON e.object_id=o.object_id
        LEFT JOIN current_states s
          ON e.sensor_type=s.sensor_type AND e.value_raw=s.state_name
    """)
    coverage_field = "working_covered" if policy_overlay else "source_covered"
    con.execute(f"""
        CREATE VIEW coverage_days AS
        SELECT * EXCLUDE (working_covered), {coverage_field} AS working_covered
        FROM read_parquet({_q(snapshot / "coverage.parquet")})
    """)
    meta = {
        "snapshot_version": manifest["etl_version"],
        "snapshot_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "policy_version": policy["policy_version"] if policy_overlay else "none_sensitivity_only",
        "policy_manifest_sha256": (
            hashlib.sha256(policy_path.read_bytes()).hexdigest() if policy_overlay else None
        ),
        "timezone": manifest["timezone_assumption"],
        "working_curated_rows": policy["working_curated_rows"] if policy_overlay else None,
    }
    return con, meta
