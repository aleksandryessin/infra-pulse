"""Aggregation, combination and feature variants for sensor-failure-tuning-v2."""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from infra_pulse_research.modeling.sensor_failure_tuning import (
    card_aggregates,
    combine,
    routing_by_type,
    variant_columns,
)

CONFIG = json.loads(
    (Path(__file__).resolve().parents[1] / "configs/sensor_failure_tuning_v2.json").read_text(
        encoding="utf-8"
    )
)
DAY = pd.Timestamp("2024-01-01")


def test_variants_drop_numbers_or_identity_proxies() -> None:
    columns = ["hist__a", "cal__b", "cat__c", "ctx__obj_other_channels", "ctx__x", "num__z"]
    assert "num__z" not in variant_columns(columns, CONFIG, "F2")
    assert "num__z" in variant_columns(columns, CONFIG, "F3")
    noproxy = variant_columns(columns, CONFIG, "F3_noproxy")
    assert "ctx__obj_other_channels" not in noproxy and "ctx__x" in noproxy


def test_card_aggregations_use_candidate_channels_only() -> None:
    channels = pd.DataFrame(
        {
            "object_id": [1, 1, 1, 2],
            "sensor_type": ["s", "s", "s", "s"],
            "issued_at": [DAY] * 4,
            "candidate": [True, True, False, True],
            "p": [0.5, 0.2, 0.99, 0.1],
        }
    )
    maxed = card_aggregates(channels, "p", "max").set_index("object_id")["score"]
    noisy = card_aggregates(channels, "p", "noisy_or").set_index("object_id")["score"]
    top2 = card_aggregates(channels, "p", "mean_top2").set_index("object_id")["score"]
    assert maxed[1] == 0.5 and np.isclose(noisy[1], 1 - 0.5 * 0.8) and np.isclose(top2[1], 0.35)
    assert np.isclose(top2[2], 0.1)


def test_combinations_and_routing() -> None:
    cards = pd.DataFrame(
        {
            "issued_at": [DAY] * 4,
            "sensor_type": ["a", "a", "b", "b"],
            "m": [0.9, 0.1, 0.2, 0.8],
            "p": [0.1, 0.9, 0.7, 0.3],
            "y_card": [1, 0, 1, 0],
        }
    )
    assert list(combine(cards, "m", "p", "model_only")) == [0.9, 0.1, 0.2, 0.8]
    mean = combine(cards, "m", "p", "rank_mean_with_persistence")
    assert np.isclose(mean, 0.625).all()
    routing = routing_by_type(cards, "m", "p")
    assert routing == {"a": "model", "b": "persistence"}
    routed = combine(
        cards, "m", "p", "per_sensor_type_best_of_model_or_persistence_by_folds", routing
    )
    assert routed[0] == 1.0 and routed[2] == 0.75
