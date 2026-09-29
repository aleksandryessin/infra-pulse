"""Bounded SQL extraction, complete 90-day inputs, and separate future labels."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from infra_pulse_research.data.prepared import open_prepared
from infra_pulse_research.episodes.protocol import (
    ACTIVATIONS,
    FAMILIES,
    FEATURES,
    derive_events,
    episode_heads,
    sessions,
)
from infra_pulse_research.sequence.data import Prepared
from infra_pulse_research.sequence.io import ROOT, identity
from infra_pulse_research.sequence.training import digest, write_json


def code_identity():
    meta = identity()
    meta["code_sha256"].update(
        {str(p.relative_to(ROOT)): digest(p) for p in Path(__file__).parent.glob("*.py")}
    )
    meta.update(
        feature_version="daily-90-v1",
        label_version="registered-episodes-v1",
        policy_version="new-episode-list-v1",
    )
    return meta


def literal(v):
    return "'" + str(v).replace("'", "''") + "'"


def extract(config, output, *, resume=False):
    """One SQL pass to disk-backed normalized timestamps; no giant Python raw table."""
    output.mkdir(parents=True, exist_ok=resume)
    snapshot, overlay = ROOT / config["snapshot"], ROOT / config["overlay"]
    con = open_prepared(
        snapshot, policy_manifest=overlay, memory_limit="2GB", threads=config["training"]["threads"]
    )
    con.execute(f"SET temp_directory={literal(output / 'spill')}")
    con.execute("SET preserve_insertion_order=false")
    first = pd.Timestamp(min(f["train_start"] for f in config["folds"])) - pd.Timedelta(days=365)
    end = max(f["test_end"] for f in config["folds"])
    types = sorted(set(FAMILIES) | set(ACTIVATIONS))
    try:
        if resume:
            stage = json.loads((output / "stage.json").read_text())
            if stage["config"] != config or stage["snapshot_manifest_sha256"] != digest(
                snapshot / "manifest.json"
            ):
                raise ValueError("stage config/source mismatch")
            for name, checksum in stage["artifacts"].items():
                if digest(output / name) != checksum:
                    raise ValueError("stage artifact mismatch")
            runs = pd.read_parquet(output / "runs.parquet")
            daily = pd.read_parquet(output / "daily.parquet")
        else:
            print("extract: aggregating channel timestamps", flush=True)
            # Current-reference membership is an explicit limitation. Conflicts fail closed.
            ref = con.execute(
                f"SELECT channel_id, object_id, sensor_type FROM read_parquet("
                f"{literal(snapshot / 'channels.parquet')})"
            ).df()
            if ref.channel_id.duplicated().any():
                raise ValueError("ambiguous current channel reference")
            con.register("reference", ref)
            mismatch = con.execute(f"""SELECT count(*) FROM working_events e JOIN reference c
              USING(channel_id) WHERE e.sensor_type IN ({",".join(map(literal, types))})
              AND e.object_id IS DISTINCT FROM c.object_id
              AND substr(e.event_ts_local_raw,1,10) >= {literal(first.date())}
              AND substr(e.event_ts_local_raw,1,10) < {literal(end)}""").fetchone()[0]
            if mismatch:
                raise ValueError("source/reference object assignment mismatch")
            con.execute(f"ATTACH {literal(output / 'staging.duckdb')} AS staging")
            query = f"""SELECT cast(object_id AS VARCHAR) object_id,
                cast(channel_id AS VARCHAR) channel_id, sensor_type,
                try_cast(event_ts_local_raw AS TIMESTAMP) event_at,
                CASE WHEN count(DISTINCT value_raw) FILTER (WHERE value_numeric IS NULL) > 1
                     THEN '<CONFLICT>' ELSE coalesce(min(value_raw) FILTER
                     (WHERE value_numeric IS NULL), '<NUMERIC>') END state,
                bool_or(coalesce(alarm,false)) alarm, count(*) messages
              FROM working_events WHERE sensor_type IN ({",".join(map(literal, types))})
                AND object_id IS NOT NULL AND NOT is_epoch_placeholder
                AND substr(event_ts_local_raw,1,10) >= {literal(first.date())}
                AND substr(event_ts_local_raw,1,10) < {literal(end)}
                AND month = ?
              GROUP BY object_id, channel_id, sensor_type, event_at"""
            months = pd.period_range(first, pd.Timestamp(end) - pd.Timedelta(days=1), freq="M")
            for i, month in enumerate(months):
                print(f"extract: month {month}", flush=True)
                prefix = (
                    "CREATE TABLE staging.observations AS "
                    if i == 0
                    else "INSERT INTO staging.observations "
                )
                con.execute(prefix + query, [str(month)])
            con.execute("""CREATE TABLE staging.ordered AS SELECT *,
              lag(state) OVER w prev_state, lag(event_at) OVER w prev_at
              FROM staging.observations WINDOW w AS (PARTITION BY channel_id ORDER BY event_at)""")
            # Keep explicit conflict/unknown rows; do not bridge them by dropping them.
            runs = con.execute("""WITH r AS (SELECT * FROM staging.ordered
              WHERE state IS DISTINCT FROM prev_state)
              SELECT object_id, channel_id, sensor_type, event_at, state, alarm,
                  prev_state, prev_at,
              lead(event_at) OVER w next_at, lead(state) OVER w next_state FROM r
              WINDOW w AS (PARTITION BY channel_id ORDER BY event_at)""").df()
            runs = runs.rename(columns={"event_at": "at"})
            runs.to_parquet(output / "runs.parquet", index=False)
            cases = (
                "CASE "
                + " ".join(
                    f"WHEN sensor_type={literal(t)} AND state={literal(s)} THEN 1"
                    for t, s in ACTIVATIONS.items()
                )
                + " ELSE 0 END"
            )
            daily = con.execute(f"""SELECT object_id, sensor_type, cast(event_at AS DATE) AS day,
              sum(messages) messages, avg(cast(alarm AS INT)) alarm_share,
              avg(cast(state='Неисправен' AS INT)) fault_share, avg({cases}) activation_share,
              avg(cast(state IN ('<CONFLICT>','<NULL>','Неопределен') AS INT)) unknown_share,
              sum(cast(state IS DISTINCT FROM prev_state AS INT)) transitions,
              count(DISTINCT channel_id) channels,
              coalesce(avg(epoch(event_at-prev_at)/3600),0) mean_gap_hours,
              coalesce(max(epoch(event_at-prev_at)/3600),0) max_gap_hours
              FROM staging.ordered GROUP BY object_id,sensor_type,day""").df()
            daily.to_parquet(output / "daily.parquet", index=False)
            stage = {
                "config": config,
                "artifacts": {n: digest(output / n) for n in ("runs.parquet", "daily.parquet")},
                "snapshot_manifest_sha256": digest(snapshot / "manifest.json"),
                "extraction_code_sha256": digest(Path(__file__)),
            }
            write_json(output / "stage.json", stage)
        cov = con.execute(
            f"SELECT day FROM read_parquet({literal(snapshot / 'coverage.parquet')})"
            " WHERE working_covered"
        ).fetchall()
        rules = con.execute("SELECT day,sensor_type_scope FROM policy_days").fetchall()
        covered = {
            f: {pd.Timestamp(d[0]).date() for d in cov}
            - {
                pd.Timestamp(d).date()
                for d, scope in rules
                if scope is None or FAMILIES.get(scope) == f
            }
            for f in set(FAMILIES.values())
        }
        print(f"extract: {len(runs)} state runs; deriving labels", flush=True)
        events = derive_events(runs, covered)
        work = sessions(runs)
        private = {
            "events": events,
            "sessions": work,
            "coverage": {f: sorted(str(d) for d in ds) for f, ds in covered.items()},
        }
        write_json(output / "private.json", private)
        source_paths = [
            *sorted(snapshot.glob("curated/*/*.parquet")),
            snapshot / "manifest.json",
            snapshot / "channels.parquet",
            snapshot / "coverage.parquet",
        ]
        meta = {
            "status": "extracted",
            "stage": stage,
            "identity": code_identity(),
            "config": config,
            "input_sha256": {str(p.relative_to(snapshot)): digest(p) for p in source_paths},
            "overlay_sha256": digest(overlay),
            "policy_days_sha256": digest(overlay.parent / "policy_days.parquet"),
            "rows": int(daily.messages.sum()),
            "state_runs": len(runs),
            "fixture": False,
            "artifacts": {
                n: digest(output / n) for n in ("runs.parquet", "daily.parquet", "private.json")
            },
        }
        write_json(output / "manifest.json", meta)
        return meta
    finally:
        con.close()


def verify(directory, config):
    meta = json.loads((directory / "manifest.json").read_text())
    if meta["config"] != config:
        raise ValueError("extraction config mismatch; use a new directory")
    for name, checksum in meta["artifacts"].items():
        if digest(directory / name) != checksum:
            raise ValueError("extraction hash mismatch")
    current = code_identity()["code_sha256"]
    for name, checksum in meta["identity"]["code_sha256"].items():
        if name.endswith(("episodes/data.py", "episodes/protocol.py", "data/prepared.py")):
            if current.get(name) != checksum:
                raise ValueError("preparation code mismatch; extract again")
    return meta


def build(config, directory, target, horizon, mask):
    """Each eligible cutoff gets exactly 90 calendar days, including explicit empty days.

    Coverage denotes observed archive coverage, never proof of a device's health.
    Maintenance only censors labels/evaluation; it cannot alter historical eligibility.
    """
    raw = json.loads((directory / "private.json").read_text())
    daily = pd.read_parquet(directory / "daily.parquet")
    daily["family"] = daily.sensor_type.map(FAMILIES)
    families = (
        ["phase"]
        if target == "phase"
        else (["smoke", "heat"] if target == "B" else sorted(set(FAMILIES.values()) - {"phase"}))
    )
    component = "A" if target == "phase" else target
    events = [e for e in raw["events"] if e["component"] == component and e["family"] in families]
    heads = list(episode_heads(events))
    first, end = (
        pd.Timestamp(min(f["train_start"] for f in config["folds"])),
        pd.Timestamp(max(f["test_end"] for f in config["folds"])),
    )
    days = pd.date_range(first, end, inclusive="left")
    grid = pd.date_range(first - pd.Timedelta(days=365), end, inclusive="left")
    groups = list(daily[daily.family.isin(families)].groupby(["object_id", "family"], sort=True))
    nrows, width = len(groups) * len(days), config["history_days"]
    if nrows * width * len(FEATURES) * 4 > config["max_tensor_bytes"]:
        raise ValueError("tensor budget exceeded")
    # Disk-backed tensor avoids holding duplicate copies while fitting models.
    tensor_path = directory / f"tensor-{target}-{horizon}-{mask}.npy"
    numeric = np.lib.format.open_memmap(
        tensor_path, mode="w+", dtype="float32", shape=(nrows, width, len(FEATURES))
    )
    states = np.empty((nrows, width), dtype="U1")
    rows, tabs = [], []
    by_pair = defaultdict(list)
    for h in heads:
        by_pair[(h["object_id"], h["family"])].append(h)
    raw_by_pair = defaultdict(list)
    for e in events:
        raw_by_pair[(e["object_id"], e["family"])].append(e)
    work_by_obj = defaultdict(list)
    for w in raw["sessions"]:
        work_by_obj[w["object_id"]].append(
            (
                pd.Timestamp(w["start"]) - pd.Timedelta(minutes=30),
                pd.Timestamp(w["end"]) + pd.Timedelta(minutes=30),
            )
        )
    for (obj, family), g in groups:
        obj = str(obj)
        cov = set(raw["coverage"][family])
        # Multiple contact types share a family. Shares are weighted by timestamp count
        # approximately via messages; exact raw per-type inventory remains in daily.parquet.
        sums = ["messages", "transitions", "channels"]
        means = [
            "alarm_share",
            "fault_share",
            "activation_share",
            "unknown_share",
            "mean_gap_hours",
            "max_gap_hours",
        ]
        grouped = g.groupby("day").agg({**{k: "sum" for k in sums}, **{k: "mean" for k in means}})
        grouped.index = pd.to_datetime(grouped.index)
        table = grouped.reindex(grid, fill_value=0)
        covered = np.asarray([str(d.date()) in cov for d in grid])
        # Historical counts use only matured labels. B cannot leak its normal return.
        matured = pd.to_datetime([e["available_at"] for e in raw_by_pair[(obj, family)]])
        counts = (
            pd.Series(1, index=matured)
            .groupby(matured.normalize())
            .sum()
            .reindex(grid, fill_value=0)
        )
        features = np.column_stack(
            [
                np.log1p(table.messages),
                table.alarm_share,
                table.fault_share,
                table.activation_share,
                table.unknown_share,
                np.log1p(table.transitions),
                np.log1p(table.channels),
                np.log1p(table.mean_gap_hours),
                np.log1p(table.max_gap_hours),
                covered,
                np.log1p(counts),
                np.sin(grid.dayofweek * 2 * np.pi / 7),
                np.cos(grid.dayofweek * 2 * np.pi / 7),
            ]
        ).astype("float32")
        items = by_pair[(obj, family)]
        at = np.asarray([pd.Timestamp(h["at"]).value for h in items], dtype=np.int64)
        for day in days:
            idx, i = grid.get_loc(day), len(rows)
            numeric[i] = features[idx - width : idx]
            states[i] = str(sorted(set(FAMILIES.values())).index(family))
            # No requirement of previous positive events: cold/rare pairs remain visible.
            eligible = bool(
                covered[idx - width : idx].all()
                and table.messages.iloc[idx - width : idx].sum() > 0
            )
            until = day + pd.Timedelta(days=horizon)
            # B needs future confirmation/isolation; conservatively demand the extra day.
            known = all(
                str(d.date()) in cov
                for d in pd.date_range(
                    day,
                    until + (pd.Timedelta(days=1) if target == "B" else pd.Timedelta(0)),
                    inclusive="left",
                )
            )
            uncertain = family in ("smoke", "heat") and any(
                a < until and b >= day for a, b in work_by_obj[obj]
            )
            if mask == "structural_unknown" and uncertain:
                known = False
            lo = np.searchsorted(at, day.value, side="right")
            hi = np.searchsorted(at, until.value)
            future = items[lo:hi]
            # Day-level cov barriers for incident grouping are diagnosed separately.
            previous = counts.iloc[:idx]
            static = int(previous.iloc[-365:].sum())
            recent = int(previous.iloc[-14:].sum())
            history = features[idx - width : idx]
            tabs.append(
                np.concatenate(
                    [
                        history[-1],
                        history.mean(0),
                        history[-7:].mean(0),
                        history[-30:].mean(0),
                        [static, recent],
                    ]
                )
            )
            rows.append(
                {
                    "object_id": obj,
                    "family": family,
                    "day": str(day.date()),
                    "eligible": eligible,
                    "y": int(bool(future)) if known else -1,
                    "static_score": static,
                    "recent_score": recent,
                    "first_event": future[0]["at"] if future else None,
                    "work_like_window": bool(uncertain),
                    "truncated": False,
                }
            )
    numeric.flush()
    for h in heads:
        h["covered"] = str(pd.Timestamp(h["at"]).date()) in set(raw["coverage"][h["family"]])
        h["uncertain"] = h["family"] in ("smoke", "heat") and any(
            a <= pd.Timestamp(h["at"]) <= b for a, b in work_by_obj[h["object_id"]]
        )
    return Prepared(
        states,
        numeric,
        np.full(nrows, width, dtype=np.int64),
        np.asarray(tabs, dtype=np.float32),
        rows,
        heads,
    )


def split(rows, fold, horizon, target):
    result = {}
    keys = ["train_start", "validation_start", "test_start", "test_end"]
    extra = int(target == "B")
    for name, left, right in zip(("train", "validation", "test"), keys, keys[1:], strict=False):
        result[name] = np.asarray(
            [
                i
                for i, row in enumerate(rows)
                if row["eligible"]
                and row["y"] >= 0
                and fold[left] <= row["day"]
                and pd.Timestamp(row["day"]) + timedelta(days=horizon + extra)
                <= pd.Timestamp(fold[right])
            ],
            dtype=np.int64,
        )
    return result


def variant(data, directory, target, horizon, mask):
    """Re-evaluate fixed rankings under another horizon/mask; never refit on test."""
    raw = json.loads((directory / "private.json").read_text())
    coverage = {f: set(ds) for f, ds in raw["coverage"].items()}
    work = defaultdict(list)
    for w in raw["sessions"]:
        work[w["object_id"]].append(
            (
                pd.Timestamp(w["start"]) - pd.Timedelta(minutes=30),
                pd.Timestamp(w["end"]) + pd.Timedelta(minutes=30),
            )
        )
    times = defaultdict(list)
    for e in data.events:
        times[(e["object_id"], e["family"])].append(pd.Timestamp(e["at"]).value)
    times = {k: np.sort(v) for k, v in times.items()}
    rows = []
    for r in data.rows:
        day = pd.Timestamp(r["day"])
        until = day + pd.Timedelta(days=horizon)
        known = all(
            str(d.date()) in coverage[r["family"]]
            for d in pd.date_range(
                day, until + pd.Timedelta(days=int(target == "B")), inclusive="left"
            )
        )
        uncertain = r["family"] in ("smoke", "heat") and any(
            a < until and b >= day for a, b in work[r["object_id"]]
        )
        if mask == "structural_unknown" and uncertain:
            known = False
        ts = times.get((r["object_id"], r["family"]), np.array([], dtype=np.int64))
        lo = np.searchsorted(ts, day.value, side="right")
        hi = np.searchsorted(ts, until.value)
        rows.append(dict(r, y=int(hi > lo) if known else -1, work_like_window=uncertain))
    return Prepared(data.states, data.numeric, data.lengths, data.tabular, rows, data.events)
