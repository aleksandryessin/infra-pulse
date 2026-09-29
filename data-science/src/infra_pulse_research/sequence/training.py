"""Bounded minibatch training, local checkpoints and optional loopback MLflow."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

from infra_pulse_research.sequence.data import Prepared, Vocabulary, split_indices
from infra_pulse_research.sequence.evaluation import paired_intervals, replay


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def predict(model, tokens, numeric, lengths, indices, *, device, batch_size):
    import torch

    model.eval()
    out = []
    with torch.no_grad():
        for offset in range(0, len(indices), batch_size):
            idx = indices[offset : offset + batch_size]
            logits = model(
                torch.as_tensor(tokens[idx], device=device),
                torch.as_tensor(numeric[idx], device=device),
                torch.as_tensor(lengths[idx], device=device),
            )
            out.append(logits.sigmoid().cpu().numpy())
    return np.concatenate(out) if out else np.empty(0)


def fit_neural(
    data: Prepared,
    split: dict,
    architecture: str,
    seed: int,
    settings: dict,
    checkpoint: Path,
    signature: str,
    *,
    resume: bool,
):
    import torch

    from infra_pulse_research.sequence.models import SequenceClassifier, device_for

    torch.set_num_threads(settings["threads"])
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    device = device_for(settings["device"])
    # CPU is the reproducible reference; unsupported deterministic accelerator ops fail clearly.
    torch.use_deterministic_algorithms(True)
    vocabulary = Vocabulary.fit(data.states[split["train"]], data.lengths[split["train"]])
    tokens = vocabulary.transform(data.states, data.lengths)
    model = SequenceClassifier(
        architecture,
        len(vocabulary.tokens) + 2,
        hidden=settings["hidden"],
        max_steps=data.states.shape[1],
        numeric_features=data.numeric.shape[2],
    ).to(device)
    if resume and checkpoint.exists():
        payload = torch.load(checkpoint, map_location="cpu", weights_only=True)
        if payload["signature"] != signature or payload["vocabulary"] != vocabulary.tokens:
            raise ValueError("checkpoint does not match data/config/code/seed")
        model.load_state_dict(payload["state_dict"])
        trace = payload["trace"]
    else:
        optimizer = torch.optim.Adam(model.parameters(), lr=settings["learning_rate"])
        loss_fn = torch.nn.BCEWithLogitsLoss()
        y = np.asarray([r["y"] for r in data.rows], dtype=np.float32)
        best, stale, trace, best_state = float("inf"), 0, [], None
        for epoch in range(settings["epochs"]):
            model.train()
            order = rng.permutation(split["train"])
            for offset in range(0, len(order), settings["batch_size"]):
                idx = order[offset : offset + settings["batch_size"]]
                optimizer.zero_grad(set_to_none=True)
                logits = model(
                    torch.as_tensor(tokens[idx], device=device),
                    torch.as_tensor(data.numeric[idx], device=device),
                    torch.as_tensor(data.lengths[idx], device=device),
                )
                loss = loss_fn(logits, torch.as_tensor(y[idx], device=device))
                if not torch.isfinite(loss):
                    raise ValueError("nonfinite training loss")
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
            p = predict(
                model,
                tokens,
                data.numeric,
                data.lengths,
                split["validation"],
                device=device,
                batch_size=settings["batch_size"],
            )
            val = float(log_loss(y[split["validation"]], p, labels=[0, 1]))
            trace.append({"epoch": epoch + 1, "validation_log_loss": val})
            if val < best - 1e-6:
                best, stale = val, 0
                best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            else:
                stale += 1
            if stale >= settings["patience"]:
                break
        model.load_state_dict(best_state)
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temp = checkpoint.with_suffix(".tmp")
        torch.save(
            {
                "state_dict": best_state,
                "settings": model.settings,
                "vocabulary": vocabulary.tokens,
                "signature": signature,
                "trace": trace,
            },
            temp,
        )
        temp.replace(checkpoint)
    scores = predict(
        model,
        tokens,
        data.numeric,
        data.lengths,
        np.arange(len(data.rows)),
        device=device,
        batch_size=settings["batch_size"],
    )
    return scores, {
        "epochs": len(trace),
        "device": str(device),
        "parameters": sum(p.numel() for p in model.parameters()),
        "trace": trace,
    }


def fit_catboost(data, split, seed, settings, checkpoint, signature, *, resume):
    from catboost import CatBoostClassifier

    y = np.asarray([r["y"] for r in data.rows])
    model = CatBoostClassifier(
        iterations=settings["catboost_iterations"],
        depth=4,
        learning_rate=0.05,
        loss_function="Logloss",
        random_seed=seed,
        thread_count=settings["threads"],
        allow_writing_files=False,
        verbose=False,
    )
    identity = checkpoint.with_suffix(".json")
    if resume and checkpoint.exists() and identity.exists():
        if json.loads(identity.read_text())["signature"] != signature:
            raise ValueError("CatBoost checkpoint mismatch")
        model.load_model(str(checkpoint))
    else:
        model.fit(
            data.tabular[split["train"]],
            y[split["train"]],
            eval_set=(data.tabular[split["validation"]], y[split["validation"]]),
            early_stopping_rounds=settings["patience"],
            use_best_model=True,
        )
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = checkpoint.with_suffix(".tmp")
        model.save_model(str(temporary))
        temporary.replace(checkpoint)
        write_json(identity, {"signature": signature})
    return model.predict_proba(data.tabular)[:, 1], {"trees": model.tree_count_, "device": "cpu"}


def binary_metrics(y, scores) -> dict:
    return {
        "brier_uncalibrated": float(brier_score_loss(y, scores)),
        "roc_auc": float(roc_auc_score(y, scores)) if len(set(y)) == 2 else None,
        "average_precision": float(average_precision_score(y, scores)) if y.sum() else None,
    }


class Tracking:
    def __init__(self, uri: str | None, experiment: str = "phase-sequence-research-v1"):
        self.mlflow = None
        if uri:
            parsed = urlparse(uri)
            if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("tracking URI must be local loopback HTTP")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("tracking URI must not contain credentials/query/fragment")
            import mlflow

            self.mlflow = mlflow
            mlflow.set_tracking_uri(uri)
            mlflow.set_experiment(experiment)

    def run(self, name, *, nested=False):
        return self.mlflow.start_run(run_name=name, nested=nested) if self.mlflow else nullcontext()

    def log(self, params, report):
        if self.mlflow:
            self.mlflow.log_params(params)
            self.mlflow.log_dict(report, "aggregate.json")
            self.mlflow.log_metrics(
                {k: v for k, v in report.get("metrics", {}).items() if isinstance(v, (int, float))}
            )


def run_experiment(
    data: Prepared,
    config: dict,
    output: Path,
    identity: dict,
    *,
    tracking_uri: str | None = None,
    resume: bool = False,
) -> dict:
    tracking = Tracking(tracking_uri)
    signature_base = hashlib.sha256(
        json.dumps({"config": config, "identity": identity}, sort_keys=True).encode()
    ).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    previous = output / "run.json"
    if previous.exists():
        if not resume or json.loads(previous.read_text())["signature"] != signature_base:
            raise ValueError("output exists; use a new directory or matching --resume")
    report = {
        "status": "running",
        "signature": signature_base,
        "identity": identity,
        "evaluation": "viewed_history_diagnostic",
        "models": [],
        "calibration": "not calibrated; scores are for ranking only",
        "config": {k: v for k, v in config.items() if k not in ("snapshot", "overlay")},
        "entrypoint": "uv run --locked --group sequence python -m infra_pulse_research.sequence",
    }
    write_json(output / "config.json", config)
    write_json(previous, report)
    try:
        with tracking.run("phase-sequence"):
            tracking.log({"signature": signature_base, "fixture": identity["fixture"]}, {})
            for fold in config["folds"]:
                split = split_indices(data.rows, fold)
                y = np.asarray([r["y"] for r in data.rows])
                if len(set(y[split["train"]])) < 2:
                    raise ValueError("training split needs both classes")
                baseline_scores = np.asarray([r["static_score"] for r in data.rows], dtype=float)
                baseline, baseline_blocks = replay(
                    data.rows,
                    baseline_scores,
                    data.events,
                    start=fold["test_start"],
                    end=fold["test_end"],
                )
                with tracking.run(fold["name"], nested=True):
                    base = {
                        "fold": fold["name"],
                        "model": "static_v9_core",
                        "metrics": baseline,
                        "weekly_blocks": baseline_blocks,
                    }
                    report["models"].append(base)
                    tracking.log({"fold": fold["name"]}, base)
                    for architecture in config["models"]:
                        for seed in config["seeds"]:
                            key = f"{fold['name']}-{architecture}-{seed}"
                            signature = hashlib.sha256((signature_base + key).encode()).hexdigest()
                            extension = ".cbm" if architecture == "catboost" else ".pt"
                            checkpoint = output / "checkpoints" / (key + extension)
                            with tracking.run(key, nested=True):
                                fit = fit_catboost if architecture == "catboost" else fit_neural
                                args = (
                                    data,
                                    split,
                                    seed,
                                    config["training"],
                                    checkpoint,
                                    signature,
                                )
                                if architecture != "catboost":
                                    args = (data, split, architecture, *args[2:])
                                scores, details = fit(*args, resume=resume)
                                metrics, blocks = replay(
                                    data.rows,
                                    scores,
                                    data.events,
                                    start=fold["test_start"],
                                    end=fold["test_end"],
                                )
                                metrics.update(
                                    binary_metrics(y[split["test"]], scores[split["test"]])
                                )
                                item = {
                                    "fold": fold["name"],
                                    "model": architecture,
                                    "seed": seed,
                                    "metrics": metrics,
                                    "training": details,
                                    "split_counts": {k: len(v) for k, v in split.items()},
                                    "checkpoint_sha256": digest(checkpoint),
                                    "weekly_blocks": blocks,
                                    "paired_vs_static": paired_intervals(
                                        blocks, baseline_blocks, seed=seed
                                    ),
                                }
                                report["models"].append(item)
                                # Only aggregates enter MLflow; private inputs stay local.
                                tracking.log(
                                    {"model": architecture, "seed": seed, "signature": signature},
                                    item,
                                )
                                write_json(previous, report)
            report["status"] = "complete"
            tracking.log({}, report)
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        write_json(previous, report)
        raise
    write_json(previous, report)
    return report
