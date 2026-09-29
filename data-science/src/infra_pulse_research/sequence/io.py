"""Local snapshot adapter and hash-verified prepared datasets (never HTTP runtime)."""

from __future__ import annotations

import json
import platform
import subprocess
from datetime import date
from importlib.metadata import version
from pathlib import Path

import numpy as np

from infra_pulse_core.features import incident_list as il
from infra_pulse_core.features.phase_feeder_episodes import (
    DETECTOR_VERSION,
    MSK,
    PHASE_SENSOR_TYPE,
    Record,
)
from infra_pulse_research.data.prepared import open_prepared
from infra_pulse_research.sequence import VERSION
from infra_pulse_research.sequence.data import Prepared, combine, prepare_object
from infra_pulse_research.sequence.training import digest, write_json

ROOT = Path(__file__).resolve().parents[4]


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text())
    if config["version"] != VERSION:
        raise ValueError("unsupported sequence config version")
    if config["models"] != list(dict.fromkeys(config["models"])) or not config["models"]:
        raise ValueError("models must be nonempty and unique")
    if set(config["models"]) - {"gru", "lstm", "tcn", "transformer", "catboost"}:
        raise ValueError("unknown model")
    if not config["seeds"] or any(type(s) is not int or s < 0 for s in config["seeds"]):
        raise ValueError("nonnegative integer seeds required")
    if not config["folds"]:
        raise ValueError("at least one forward fold required")
    names = []
    for fold in config["folds"]:
        name = fold["name"]
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in name):
            raise ValueError("fold name must be a simple local filename component")
        names.append(name)
        boundaries = [
            date.fromisoformat(fold[k])
            for k in ("train_start", "validation_start", "test_start", "test_end")
        ]
        if boundaries != sorted(set(boundaries)):
            raise ValueError("fold must be strictly forward in time")
        if any((b - a).days < 15 for a, b in zip(boundaries[:-1], boundaries[1:], strict=True)):
            raise ValueError("each split needs more than 14 days")
    if len(names) != len(set(names)):
        raise ValueError("duplicate fold name")
    for key in (
        "max_steps",
        "history_days",
        "max_rows_per_object",
        "max_prepared_rows",
        "max_tensor_bytes",
    ):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"positive integer required: {key}")
    settings = config["training"]
    for key in ("threads", "hidden", "epochs", "batch_size", "patience", "catboost_iterations"):
        if type(settings[key]) is not int or settings[key] < 1:
            raise ValueError(f"positive training integer required: {key}")
    if settings["hidden"] % 4 or not 0 < settings["learning_rate"] < 1:
        raise ValueError("hidden must be divisible by 4; learning rate must be in (0,1)")
    if settings["device"] not in ("auto", "cpu", "mps", "cuda"):
        raise ValueError("device must be auto/cpu/mps/cuda")
    return config


def identity() -> dict:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

    paths = [
        *Path(__file__).parent.glob("*.py"),
        ROOT / "packages/core/src/infra_pulse_core/features/incident_list.py",
        ROOT / "packages/core/src/infra_pulse_core/features/phase_feeder_episodes.py",
        ROOT / "data-science/src/infra_pulse_research/data/prepared.py",
    ]
    return {
        "git_sha": git("rev-parse", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "code_sha256": {str(p.relative_to(ROOT)): digest(p) for p in sorted(paths)},
        "lock_sha256": digest(ROOT / "uv.lock"),
        "python": platform.python_version(),
        "platform": platform.system(),
        "packages": {p: version(p) for p in ("numpy", "duckdb", "catboost", "mlflow")},
        "feature_version": VERSION,
        "label_version": DETECTOR_VERSION,
        "policy_version": il.LIST_POLICY_VERSION,
    }


def source(config: dict):
    snapshot = (ROOT / config["snapshot"]).resolve()
    overlay = (ROOT / config["overlay"]).resolve()
    con = open_prepared(
        snapshot, policy_manifest=overlay, memory_limit="2GB", threads=config["training"]["threads"]
    )
    con.execute("SET TimeZone='Europe/Moscow'")
    channel_file = str(snapshot / "channels.parquet").replace("'", "''")
    con.execute(
        f"CREATE VIEW sequence_channels AS SELECT CAST(channel_id AS VARCHAR) channel_id, "
        f"CAST(object_id AS VARCHAR) object_id FROM read_parquet('{channel_file}') "
        "WHERE sensor_type = 'Состояние фазы' AND object_id IS NOT NULL"
    )
    n, unique = con.execute(
        "SELECT count(*), count(DISTINCT channel_id) FROM sequence_channels"
    ).fetchone()
    if n != unique or not n:
        con.close()
        raise ValueError("empty or ambiguous current phase channel reference")
    return con, snapshot, overlay


def preflight(config: dict) -> dict:
    con, snapshot, overlay = source(config)
    try:
        columns = {r[0] for r in con.execute("DESCRIBE working_events").fetchall()}
        if not {"channel_id", "object_id", "event_ts_local_raw", "value_raw", "alarm"} <= columns:
            raise ValueError("missing observation columns")
        objects = con.execute("SELECT count(DISTINCT object_id) FROM sequence_channels").fetchone()[
            0
        ]
        start = min(f["train_start"] for f in config["folds"])
        end = max(f["test_end"] for f in config["folds"])
        rows = objects * (date.fromisoformat(end) - date.fromisoformat(start)).days
        if rows > config["max_prepared_rows"]:
            raise ValueError("prepared row budget exceeded")
        max_text = (
            con.execute(
                "SELECT max(length(value_raw)) FROM working_events WHERE sensor_type = ?",
                [PHASE_SENSOR_TYPE],
            ).fetchone()[0]
            or 0
        )
        # Unicode storage uses four bytes per character. Combining parts temporarily
        # duplicates these arrays; Python records and DuckDB have separate budgets.
        tensor_bytes = rows * config["max_steps"] * (4 * max(max_text, 12) + 16)
        if tensor_bytes > config["max_tensor_bytes"]:
            raise ValueError("tensor byte budget exceeded; reduce history/steps/cutoff range")
        return {
            "status": "preflight_only",
            "objects": objects,
            "cutoff_rows_upper_bound": rows,
            "tensor_bytes_upper_bound": tensor_bytes,
            "start": start,
            "end": end,
            "snapshot_manifest_sha256": digest(snapshot / "manifest.json"),
            "overlay_manifest_sha256": digest(overlay),
            "availability": "simulated_source_time",
            "reference": "current_snapshot_without_validity_dates",
            "coverage": "archive_coverage_not_device_health",
        }
    finally:
        con.close()


def prepare_snapshot(config: dict) -> tuple[Prepared, dict]:
    meta = preflight(config)
    con, snapshot, overlay = source(config)
    try:
        covfile = str(snapshot / "coverage.parquet").replace("'", "''")
        covered = {
            r[0]
            for r in con.execute(
                f"SELECT day FROM read_parquet('{covfile}') WHERE working_covered"
            ).fetchall()
        }
        excluded = {
            r[0]
            for r in con.execute(
                "SELECT day FROM policy_days "
                "WHERE sensor_type_scope IS NULL OR sensor_type_scope = ?",
                [PHASE_SENSOR_TYPE],
            ).fetchall()
        }
        covered -= excluded
        start, end = date.fromisoformat(meta["start"]), date.fromisoformat(meta["end"])
        objects = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT object_id FROM sequence_channels ORDER BY object_id"
            ).fetchall()
        ]
        parts = []
        query = """SELECT CAST(e.channel_id AS VARCHAR),
                     TRY_CAST(e.event_ts_local_raw AS TIMESTAMP) AS at,
                     e.value_raw, COALESCE(e.alarm, false), CAST(e.object_id AS VARCHAR)
                   FROM working_events e JOIN sequence_channels c
                     ON CAST(e.channel_id AS VARCHAR) = c.channel_id
                   WHERE c.object_id = ? AND e.sensor_type = ?
                     AND NOT e.is_epoch_placeholder
                     AND TRY_CAST(e.event_ts_local_raw AS TIMESTAMP) < CAST(? AS TIMESTAMP)"""
        for obj in objects:
            rows = con.execute(
                query + " LIMIT ?",
                [obj, PHASE_SENSOR_TYPE, str(end), config["max_rows_per_object"] + 1],
            ).fetchall()
            if len(rows) > config["max_rows_per_object"]:
                raise ValueError("per-object observation budget exceeded")
            if any(o != obj for _, _, _, _, o in rows):
                raise ValueError("source/current-reference object assignment conflict")
            records = [Record(c, t.replace(tzinfo=MSK), v, a, o) for c, t, v, a, o in rows]
            if records:
                parts.append(
                    prepare_object(
                        records,
                        covered,
                        start,
                        end,
                        max_steps=config["max_steps"],
                        history_days=config["history_days"],
                    )
                )
        meta["input_sha256"] = {
            str(p.relative_to(snapshot)): digest(p)
            for p in sorted(snapshot.glob("curated/*/*.parquet"))
        }
        for p in (snapshot / "channels.parquet", snapshot / "coverage.parquet"):
            meta["input_sha256"][p.name] = digest(p)
        meta["policy_days_sha256"] = digest(overlay.parent / "policy_days.parquet")
        meta.update(status="prepared", fixture=False)
        return combine(parts), meta
    finally:
        con.close()


def save_prepared(data: Prepared, output: Path, meta: dict, config: dict):
    output.mkdir(parents=True, exist_ok=False)
    # manifest is published last; incomplete folders are never accepted by load_prepared.
    np.savez_compressed(
        output / "arrays.npz",
        states=data.states,
        numeric=data.numeric,
        lengths=data.lengths,
        tabular=data.tabular,
    )
    write_json(output / "private.json", {"rows": data.rows, "events": data.events})
    write_json(output / "config.json", config)
    meta = dict(
        meta,
        artifacts={
            name: digest(output / name) for name in ("arrays.npz", "private.json", "config.json")
        },
        rows=len(data.rows),
        eligible=sum(r["eligible"] for r in data.rows),
        unknown=sum(r["y"] < 0 for r in data.rows),
        truncated=sum(r["truncated"] for r in data.rows),
    )
    write_json(output / "manifest.json", meta)


def load_prepared(directory: Path, config: dict) -> tuple[Prepared, dict]:
    meta = json.loads((directory / "manifest.json").read_text())
    if not meta.get("fixture"):
        recorded = meta["preparation_identity"]["code_sha256"]
        current = identity()["code_sha256"]
        for name, checksum in recorded.items():
            if name.endswith(
                (
                    "/data.py",
                    "/io.py",
                    "/prepared.py",
                    "/incident_list.py",
                    "/phase_feeder_episodes.py",
                )
            ):
                if current.get(name) != checksum:
                    raise ValueError("preparation code changed; rebuild the dataset")
    for name in ("arrays.npz", "private.json", "config.json"):
        if digest(directory / name) != meta["artifacts"][name]:
            raise ValueError("prepared artifact checksum mismatch")
    original = json.loads((directory / "config.json").read_text())
    for key in ("version", "max_steps", "history_days", "folds", "snapshot", "overlay"):
        if original[key] != config[key]:
            raise ValueError(f"prepared configuration mismatch: {key}")
    raw = json.loads((directory / "private.json").read_text())
    with np.load(directory / "arrays.npz", allow_pickle=False) as arrays:
        data = Prepared(
            *(arrays[k] for k in ("states", "numeric", "lengths", "tabular")),
            raw["rows"],
            raw["events"],
        )
    return data, meta
