"""CLI: preflight, prepare, train and a wholly synthetic end-to-end smoke."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from importlib.metadata import version
from pathlib import Path

from infra_pulse_core.features.incident_list import cutoff_of
from infra_pulse_core.features.phase_feeder_episodes import Record
from infra_pulse_research.sequence.data import combine, dates, prepare_object
from infra_pulse_research.sequence.io import (
    ROOT,
    identity,
    load_config,
    load_prepared,
    preflight,
    prepare_snapshot,
    save_prepared,
)
from infra_pulse_research.sequence.training import digest, run_experiment


def synthetic(config: dict):
    start, end = date(2022, 1, 1), date(2023, 10, 1)
    covered = set(dates(start, end))
    parts = []
    for obj in range(4):
        records = []
        for i, day in enumerate(dates(start, end)):
            value = (
                "Неисправен"
                if i % (19 + obj * 12) == 0
                else ("Обесточен" if i % 7 == 0 else "Есть питание")
            )
            records.append(
                Record(
                    f"fixture-channel-{obj}",
                    cutoff_of(day) + timedelta(hours=6),
                    value,
                    value == "Неисправен",
                    f"fixture-object-{obj}",
                )
            )
        parts.append(
            prepare_object(
                records,
                covered,
                date(2023, 1, 1),
                end,
                max_steps=config["max_steps"],
                history_days=config["history_days"],
            )
        )
    return combine(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "prepare", "train", "smoke"))
    parser.add_argument(
        "--config", type=Path, default=ROOT / "data-science/configs/sequence_research_v1.json"
    )
    parser.add_argument(
        "--prepared", type=Path, default=ROOT / "data-science/artifacts/sequence-v1/prepared"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data-science/artifacts/sequence-v1/run"
    )
    parser.add_argument("--tracking-uri", help="optional local HTTP MLflow server")
    parser.add_argument(
        "--resume", action="store_true", help="reuse completed matching checkpoints"
    )
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "preflight":
        print(json.dumps(preflight(config), ensure_ascii=False, indent=2))
        return
    if args.command == "prepare":
        if args.prepared.exists():
            raise FileExistsError("prepared directory already exists; choose a new directory")
        data, meta = prepare_snapshot(config)
        save_prepared(data, args.prepared, dict(meta, preparation_identity=identity()), config)
        print(json.dumps({"status": "prepared", "rows": len(data.rows)}))
        return
    if args.command == "smoke":
        config.update(
            max_steps=16,
            history_days=30,
            seeds=[17],
            folds=[
                {
                    "name": "fixture",
                    "train_start": "2023-01-01",
                    "validation_start": "2023-05-01",
                    "test_start": "2023-07-01",
                    "test_end": "2023-10-01",
                }
            ],
        )
        config["training"].update(
            epochs=2, patience=2, hidden=8, batch_size=128, catboost_iterations=8, device="cpu"
        )
        data = synthetic(config)
        # Fixture provenance is independent of local private inputs.
        meta = {"fixture": True, "generator": "sequence.synthetic-v1"}
    else:
        data, meta = load_prepared(args.prepared, config)
        meta = {
            "fixture": meta["fixture"],
            "prepared_manifest_sha256": digest(args.prepared / "manifest.json"),
        }
    meta.update(identity())
    meta["packages"]["torch"] = version("torch")
    report = run_experiment(
        data, config, args.output, meta, tracking_uri=args.tracking_uri, resume=args.resume
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "fixture": meta["fixture"],
                "evaluations": len(report["models"]),
            }
        )
    )


if __name__ == "__main__":
    main()
