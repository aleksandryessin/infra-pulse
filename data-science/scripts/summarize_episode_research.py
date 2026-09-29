"""Publish only aggregate evidence from a completed local registered-episode run."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np

from infra_pulse_research.episodes.protocol import episode_heads
from infra_pulse_research.sequence.io import ROOT
from infra_pulse_research.sequence.training import digest, write_json


def summarize(prepared: Path, run_dir: Path, output: Path):
    run_path = run_dir / "run.json"
    run = json.loads(run_path.read_text())
    if run["status"] != "complete":
        raise ValueError("do not publish partial research as a completed result")
    audit = json.loads((run_dir / "audit.json").read_text())
    private = json.loads((prepared / "private.json").read_text())
    groups = {}
    for item in run["models"]:
        if item["status"] != "complete":
            continue
        key = tuple(item[k] for k in ("target", "fold", "horizon_days", "mask", "model"))
        groups.setdefault(key, []).append(item)
    comparisons = []
    for key, items in sorted(groups.items()):
        metrics = {}
        for metric in items[0]["metrics"]:
            values = [i["metrics"].get(metric) for i in items]
            metrics[metric] = float(np.mean(values)) if all(v is not None for v in values) else None
        comparisons.append(
            dict(zip(("target", "fold", "horizon_days", "mask", "model"), key, strict=True))
            | {
                "seeds": len(items),
                "mean_metrics": metrics,
                "families": [{"seed": i.get("seed"), "metrics": i["families"]} for i in items],
            }
        )
    composition = []
    for component in ("A", "B"):
        events = [e for e in private["events"] if e["component"] == component]
        heads = list(episode_heads(events))
        for fold in run["config"]["folds"]:
            for family in sorted({e["family"] for e in events}):
                raw_count = sum(
                    e["family"] == family and fold["test_start"] <= e["at"] < fold["test_end"]
                    for e in events
                )
                head_count = sum(
                    e["family"] == family and fold["test_start"] <= e["at"] < fold["test_end"]
                    for e in heads
                )
                composition.append(
                    dict(
                        component=component,
                        family=family,
                        fold=fold["name"],
                        channel_onsets=raw_count,
                        episode_heads=head_count,
                        continuation_or_simultaneous_members=raw_count - head_count,
                    )
                )
    baseline = audit["baseline_reconciliation"]
    output_data = {
        "status": "complete",
        "evaluation": "viewed_history_diagnostic_not_physical_failures",
        "run_sha256": digest(run_path),
        "prepared_manifest_sha256": digest(prepared / "manifest.json"),
        "audit_sha256": digest(run_dir / "audit.json"),
        "summary_code_sha256": digest(Path(__file__)),
        "identity": run["identity"],
        "mlflow_run_id": run.get("mlflow_run_id"),
        "seconds": run["seconds"],
        "config": run["config"],
        "comparisons": comparisons,
        "gates": run["gates"],
        "conclusions": run["conclusions"],
        "episode_composition": composition,
        "maintenance_by_month": audit["maintenance_by_month"],
        "event_components": audit["event_components"],
        "baseline_reconciliation": {
            "original_run_sha256": baseline["sequence_run_sha256"],
            "original_signature": baseline["sequence_signature"],
            "folds": baseline["folds"],
        },
        "schedules": {
            "usable_as_feature": False,
            "usable_as_target": False,
            "identity": "unconfirmed outcome-informed crosswalk",
            "availability": "unknown",
            "execution": "plan is not completed work",
            "to_year_conflict": True,
        },
        "fire_risk": audit["fire_risk"],
        "model_status_counts": dict(Counter(m["status"] for m in run["models"])),
        "limits": [
            "archive coverage is not device health",
            "current reference lacks historical validity",
            "B isolation is per journal object and configured activation subset, not whole site",
            "neural family token vs numerical CatBoost aggregates: pipeline comparison",
            "CatBoost additionally uses 365/14-day onset counts; neural history is 90 days",
            "scores are uncalibrated",
            "24h is fixed-ranking sensitivity, not separately trained",
            "precision requires a fully known card window; recall uses known episode heads",
            "lead summaries include all matched heads, including uncertain outcomes",
            "no product promotion",
        ],
    }
    write_json(output, output_data)
    lines = [
        "# Исследование зарегистрированных эпизодов",
        "",
        "Просмотренная история; зарегистрированные состояния, не физические отказы.",
        "",
        "Основной горизонт 14 суток, маска предполагаемых работ переводит исход в unknown.",
        "",
        "| Таргет | Период | Модель | Precision | Recall | Unknown-карточки |",
        "|---|---|---|---:|---:|---:|",
    ]

    def fmt(value):
        return "нет оценки" if value is None else f"{value:.3f}"

    for c in comparisons:
        if c["horizon_days"] != 14 or c["mask"] != "structural_unknown":
            continue
        m = c["mean_metrics"]
        lines.append(
            f"| {c['target']} | {c['fold']} | {c['model']} | {fmt(m['card_precision'])} | "
            f"{fmt(m['incident_recall'])} | {fmt(m['cards_unknown'])} |"
        )
    lines += ["", "## Вердикты", ""] + [
        f"- {c['target']} / {c['model']}: {c['verdict']}" for c in run["conclusions"]
    ]
    lines += [
        "",
        "Межмодельные критерии и интервалы — в JSON рядом. Порог P≥0,7/R≥0,5 отдельно от прироста.",
        "Все семейства сохранены; продолжения показаны отдельно от новых эпизодов.",
        "Precision исключает неизвестные окна карточек; recall считает известные начала,",
        "в том числе пойманные карточкой с неопределённым остатком окна. Поэтому числители",
        "могут различаться при сопоставлении один-к-одному. Lead включает все совпадения,",
        "включая неопределённые исходы; это не подтверждение заблаговременности инцидентов.",
        "Подтверждение требует новых данных или проверенных эксплуатационных исходов.",
    ]
    output.with_suffix(".md").write_text("\n".join(lines) + "\n")
    return output_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepared", type=Path, default=ROOT / "data-science/artifacts/episodes-v1/prepared"
    )
    parser.add_argument("--run", type=Path, default=ROOT / "data-science/artifacts/episodes-v1/run")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data-science/reports/registered-episodes-2026-09-29.json",
    )
    args = parser.parse_args()
    summarize(args.prepared, args.run, args.output)


if __name__ == "__main__":
    main()
