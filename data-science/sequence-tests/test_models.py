"""Explicit optional suite: make check-sequence installs torch; never silently skips."""

import json

import numpy as np
import pytest
import torch

from infra_pulse_research.sequence.__main__ import synthetic
from infra_pulse_research.sequence.data import split_indices
from infra_pulse_research.sequence.io import ROOT, load_config, load_prepared, save_prepared
from infra_pulse_research.sequence.models import ARCHITECTURES, SequenceClassifier
from infra_pulse_research.sequence.training import fit_neural, run_experiment


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_padding_batch_invariance_and_checkpoint_roundtrip(architecture, tmp_path):
    torch.manual_seed(17)
    model = SequenceClassifier(architecture, 8, hidden=8, max_steps=10).eval()
    tokens = torch.tensor([[2, 3, 0, 0], [2, 4, 5, 6]])
    numbers = torch.randn(2, 4, 4)
    lengths = torch.tensor([2, 4])
    with torch.no_grad():
        before = model(tokens, numbers, lengths)
        numbers[0, 2:] = 10000
        tokens[0, 2:] = 7
        torch.testing.assert_close(before, model(tokens, numbers, lengths))
        torch.testing.assert_close(before[:1], model(tokens[:1, :2], numbers[:1, :2], lengths[:1]))
    checkpoint = tmp_path / "model.pt"
    torch.save({"settings": model.settings, "state_dict": model.state_dict()}, checkpoint)
    payload = torch.load(checkpoint, weights_only=True)
    restored = SequenceClassifier(**payload["settings"]).eval()
    restored.load_state_dict(payload["state_dict"])
    with torch.no_grad():
        torch.testing.assert_close(before, restored(tokens, numbers, lengths))


def fixture_config():
    config = load_config(ROOT / "data-science/configs/sequence_research_v1.json")
    config.update(
        max_steps=8,
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
    config["training"].update(epochs=1, hidden=8, batch_size=128, catboost_iterations=4)
    return config


def test_all_models_train_evaluate_and_resume_on_synthetic(tmp_path):
    config = fixture_config()
    data = synthetic(config)
    report = run_experiment(data, config, tmp_path / "run", {"fixture": True})
    assert report["status"] == "complete"
    assert len(report["models"]) == 6
    again = run_experiment(data, config, tmp_path / "run", {"fixture": True}, resume=True)
    assert report == again
    assert "fixture-object" not in json.dumps(report)
    assert "fixture-channel" not in json.dumps(report)
    assert all(r["metrics"]["max_open"] <= 10 for r in report["models"])
    with pytest.raises(ValueError, match="output exists"):
        run_experiment(data, config, tmp_path / "run", {"fixture": True})
    checkpoint = tmp_path / "run/checkpoints/fixture-gru-17.pt"
    with pytest.raises(ValueError, match="checkpoint"):
        fit_neural(
            data,
            split_indices(data.rows, config["folds"][0]),
            "gru",
            17,
            config["training"],
            checkpoint,
            "wrong-signature",
            resume=True,
        )


def test_prepared_hashes_and_schema_are_verified(tmp_path):
    config = fixture_config()
    data = synthetic(config)
    path = tmp_path / "prepared"
    save_prepared(data, path, {"fixture": True}, config)
    loaded, _ = load_prepared(path, config)
    np.testing.assert_array_equal(data.states, loaded.states)
    with (path / "private.json").open("a") as handle:
        handle.write(" ")
    with pytest.raises(ValueError, match="checksum"):
        load_prepared(path, config)
