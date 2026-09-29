"""End-to-end synthetic daily experiment and checkpoint/signature replay."""

import json

import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.episodes.data import code_identity
from infra_pulse_research.episodes.runner import run
from infra_pulse_research.sequence.io import ROOT
from infra_pulse_research.sequence.training import digest


def test_daily_models_replay_and_resume(tmp_path):
    config = json.loads((ROOT / "data-science/configs/episode_research_v1.json").read_text())
    config["targets"] = ["A"]
    config["folds"] = [
        {
            "name": "fixture",
            "train_start": "2023-01-01",
            "validation_start": "2023-03-01",
            "test_start": "2023-05-01",
            "test_end": "2023-07-01",
        }
    ]
    config["training"].update(epochs=1, hidden=8, catboost_iterations=3)
    config["bootstrap_replicates"] = 25
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    days = pd.date_range("2022-01-01", "2023-07-01", inclusive="left")
    records = [
        dict(
            object_id=o,
            sensor_type="Датчик дыма",
            day=d,
            messages=2,
            transitions=1,
            channels=1,
            alarm_share=0.0,
            fault_share=0.0,
            activation_share=0.0,
            unknown_share=0.0,
            mean_gap_hours=1.0,
            max_gap_hours=1.0,
        )
        for o in ("o1", "o2")
        for d in days
    ]
    pd.DataFrame(records).to_parquet(prepared / "daily.parquet", index=False)
    events = [
        dict(
            object_id=o,
            family="smoke",
            channel_id=o + "c",
            component="A",
            at=t.isoformat(),
            available_at=t.isoformat(),
        )
        for o in ("o1", "o2")
        for t in pd.date_range("2022-01-10 01:00", "2023-06-29", freq="25D")
    ]
    (prepared / "private.json").write_text(
        json.dumps(
            {"events": events, "sessions": [], "coverage": {"smoke": [str(d.date()) for d in days]}}
        )
    )
    (prepared / "manifest.json").write_text(
        json.dumps(
            {
                "config": config,
                "fixture": True,
                "identity": code_identity(),
                "artifacts": {n: digest(prepared / n) for n in ("daily.parquet", "private.json")},
            }
        )
    )
    output = tmp_path / "run"
    report = run(config, prepared, output)
    assert report["status"] == "complete" and len(report["models"]) == 44
    assert all(m["metrics"]["max_open"] <= 10 for m in report["models"])
    for path in output.glob("*.npy"):
        assert np.isfinite(np.load(path)).all()
    assert len(list((output / "checkpoints").glob("*.pt"))) == 6
    resumed = run(config, prepared, output, resume=True)
    assert [m["metrics"] for m in resumed["models"]] == [m["metrics"] for m in report["models"]]
    changed = dict(config, k=9)
    with pytest.raises(ValueError, match="config mismatch"):
        run(changed, prepared, output, resume=True)
    scorefile = next(output.glob("*.npy"))
    scorefile.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="artifact mismatch"):
        run(config, prepared, output, resume=True)


def test_daily_tcn_reaches_the_first_of_90_days():
    import torch

    from infra_pulse_research.sequence.models import SequenceClassifier

    model = SequenceClassifier("tcn", 3, hidden=8, max_steps=90, numeric_features=13)
    for param in model.parameters():
        torch.nn.init.constant_(param, 0.1)
    numeric = torch.ones((1, 90, 13), requires_grad=True)
    model(torch.ones((1, 90), dtype=torch.long), numeric, torch.tensor([90])).sum().backward()
    assert numeric.grad[0, 0, 0] > 0
