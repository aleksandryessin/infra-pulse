"""Resumable, fixed-protocol local comparison. No product promotion."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from time import perf_counter

import numpy as np

from infra_pulse_research.episodes import VERSION
from infra_pulse_research.episodes.data import build, code_identity, split, variant, verify
from infra_pulse_research.episodes.evaluation import gate, replay
from infra_pulse_research.sequence.training import (
    Tracking,
    binary_metrics,
    digest,
    fit_catboost,
    fit_neural,
    write_json,
)


def load_config(path):
    c = json.loads(Path(path).read_text())
    if (
        c["version"] != VERSION
        or c["history_days"] != 90
        or c["k"] != 10
        or c["horizons_days"] != [14, 1]
        or c["seeds"] != [17, 43, 91]
        or c["masks"] != ["none", "structural_unknown"]
        or c["models"] != ["catboost", "tcn", "transformer"]
        or c["targets"] != ["A", "B", "phase"]
        or c["schedule_use"] != "diagnostic_only"
    ):
        raise ValueError("unsupported protocol; create a new version for semantic changes")
    return c


def public(item, object_codes):
    # Object-level paired intervals need stable clustering, not private source IDs.
    result = dict(item)
    if "blocks" in result:
        result["blocks"] = dict(
            result["blocks"],
            objects={object_codes[k]: v for k, v in result["blocks"]["objects"].items()},
        )
    return result


def run(config, prepared, output, *, tracking_uri=None, resume=False):
    meta = verify(prepared, config)
    identity = code_identity() | {
        "extraction_manifest_sha256": digest(prepared / "manifest.json"),
        "fixture": meta["fixture"],
    }
    sig = hashlib.sha256(
        json.dumps({"config": config, "identity": identity}, sort_keys=True).encode()
    ).hexdigest()
    path = output / "run.json"
    if path.exists() and (not resume or json.loads(path.read_text())["signature"] != sig):
        raise ValueError("output exists or identity changed; use a new output")
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "running",
        "signature": sig,
        "identity": identity,
        "config": config,
        "evaluation": "viewed_history_diagnostic",
        "models": [],
        "gates": [],
        "diagnostic_24h": "fixed 14d-trained rankings; no separate 24h model fit",
        "training_mask": "structural_unknown",
        "probabilities": "uncalibrated ranking only",
    }
    write_json(path, report)
    tracking = Tracking(tracking_uri, "registered-episode-research-v1")
    began = perf_counter()
    try:
        with tracking.run("registered-episodes-v1") as root:
            if root:
                report["mlflow_run_id"] = root.info.run_id
            tracking.log({"signature": sig, "fixture": identity["fixture"]}, {})
            for target in config["targets"]:
                print(f"building daily inputs: {target}", flush=True)
                data = build(config, prepared, target, 14, "structural_unknown")
                object_codes = {
                    o: f"object_{i:03d}"
                    for i, o in enumerate(sorted({r["object_id"] for r in data.rows}))
                }
                variants = {
                    (h, m): variant(data, prepared, target, h, m)
                    for h in config["horizons_days"]
                    for m in config["masks"]
                }
                for fold in config["folds"]:
                    ix = split(data.rows, fold, 14, target)
                    y = np.asarray([r["y"] for r in data.rows])
                    fitted = {}
                    # Fit once on primary 14d censored labels. Mask/horizon sensitivity
                    # uses identical scores so it cannot pick a model using test outcomes.
                    for architecture in config["models"]:
                        for seed in config["seeds"]:
                            key = f"{target}-{fold['name']}-{architecture}-{seed}"
                            if any(not len(v) for v in ix.values()) or len(set(y[ix["train"]])) < 2:
                                report["models"].append(
                                    {
                                        "target": target,
                                        "fold": fold["name"],
                                        "model": architecture,
                                        "seed": seed,
                                        "status": "insufficient_data",
                                    }
                                )
                                continue
                            checkpoint = (
                                output
                                / "checkpoints"
                                / (key + (".cbm" if architecture == "catboost" else ".pt"))
                            )
                            cache, cache_meta = output / (key + ".npy"), output / (key + ".json")
                            signature = hashlib.sha256((sig + key).encode()).hexdigest()
                            started = perf_counter()
                            print(
                                f"training {key}: { {k: len(v) for k, v in ix.items()} }",
                                flush=True,
                            )
                            if resume and cache.exists() and cache_meta.exists():
                                details = json.loads(cache_meta.read_text())
                                if (
                                    details["signature"] != signature
                                    or details["scores_sha256"] != digest(cache)
                                    or details["checkpoint_sha256"] != digest(checkpoint)
                                ):
                                    raise ValueError("resume artifact mismatch")
                                scores = np.load(cache, allow_pickle=False)
                            else:
                                if architecture == "catboost":
                                    scores, details = fit_catboost(
                                        data,
                                        ix,
                                        seed,
                                        config["training"],
                                        checkpoint,
                                        signature,
                                        resume=resume,
                                    )
                                else:
                                    scores, details = fit_neural(
                                        data,
                                        ix,
                                        architecture,
                                        seed,
                                        config["training"],
                                        checkpoint,
                                        signature,
                                        resume=resume,
                                    )
                                np.save(cache, scores, allow_pickle=False)
                                details.update(
                                    signature=signature,
                                    scores_sha256=digest(cache),
                                    checkpoint_sha256=digest(checkpoint),
                                    seconds=perf_counter() - started,
                                )
                                write_json(cache_meta, details)
                            fitted[(architecture, seed)] = (scores, details)
                    for (horizon, mask), evaluation in variants.items():
                        common = dict(
                            target=target, fold=fold["name"], horizon_days=horizon, mask=mask
                        )
                        baselines = {}
                        for name, field in (
                            ("frequency", "static_score"),
                            ("persistence", "recent_score"),
                        ):
                            result = replay(
                                evaluation.rows,
                                np.asarray([r[field] for r in evaluation.rows]),
                                data.events,
                                fold,
                                horizon=horizon,
                                mask=mask,
                                k=config["k"],
                                target=target,
                            )
                            baselines[name] = result
                            item = (
                                common
                                | {"model": name, "status": "complete"}
                                | public(result, object_codes)
                            )
                            report["models"].append(item)
                            with tracking.run(
                                f"{target}-{fold['name']}-{horizon}-{mask}-{name}", nested=True
                            ):
                                tracking.log(common | {"model": name}, item)
                        for architecture in config["models"]:
                            candidates = []
                            for seed in config["seeds"]:
                                if (architecture, seed) not in fitted:
                                    continue
                                scores, details = fitted[(architecture, seed)]
                                result = replay(
                                    evaluation.rows,
                                    scores,
                                    data.events,
                                    fold,
                                    horizon=horizon,
                                    mask=mask,
                                    k=config["k"],
                                    target=target,
                                )
                                if horizon == 14 and mask == "structural_unknown":
                                    result["metrics"].update(
                                        binary_metrics(y[ix["test"]], scores[ix["test"]])
                                    )
                                candidates.append(result)
                                item = (
                                    common
                                    | {
                                        "model": architecture,
                                        "seed": seed,
                                        "training": details,
                                        "split_counts": {k: len(v) for k, v in ix.items()},
                                        "status": "complete",
                                    }
                                    | public(result, object_codes)
                                )
                                report["models"].append(item)
                                with tracking.run(
                                    f"{target}-{fold['name']}-{horizon}-{mask}-{architecture}-{seed}",
                                    nested=True,
                                ):
                                    tracking.log(
                                        common | {"model": architecture, "seed": seed}, item
                                    )
                            report["gates"].append(
                                common
                                | {"model": architecture}
                                | gate(
                                    candidates,
                                    baselines,
                                    config["acceptance"],
                                    config["bootstrap_replicates"],
                                )
                            )
                        write_json(path, report)
            # No cherry-picking fold/mask: primary gain must survive both on D14.
            report["conclusions"] = []
            for target in config["targets"]:
                for model in config["models"]:
                    checks = [
                        g
                        for g in report["gates"]
                        if g["target"] == target and g["model"] == model and g["horizon_days"] == 14
                    ]
                    report["conclusions"].append(
                        {
                            "target": target,
                            "model": model,
                            "verdict": "advantage_established_on_viewed_history"
                            if len(checks) == 4 and all(g["passed"] for g in checks)
                            else "advantage_not_established",
                        }
                    )
            report.update(status="complete", seconds=perf_counter() - began)
            tracking.log({}, report)
    except Exception as exc:
        report.update(
            status="failed", error_type=type(exc).__name__, seconds=perf_counter() - began
        )
        write_json(path, report)
        raise
    write_json(path, report)
    table = [
        "# Registered episode research",
        "",
        "Viewed history; not physical failures. 24h uses fixed 14d-trained rankings.",
        "",
        "| Target | Fold | Days | Mask | Model | P (3 seeds) | R (3 seeds) | Gain gate |",
        "|---|---|---:|---|---|---:|---:|---|",
    ]
    for g in report["gates"]:
        means = g.get("means", {})

        def fmt(x):
            return f"{x:.4f}" if x is not None else "insufficient"

        table.append(
            f"| {g['target']} | {g['fold']} | {g['horizon_days']} | {g['mask']} | {g['model']} | "
            f"{fmt(means.get('card_precision'))} | {fmt(means.get('incident_recall'))} | "
            f"{g['passed']} |"
        )
    (output / "comparison.md").write_text("\n".join(table) + "\n")
    return report
