"""Synthetic checks for the card-budget evaluation of subsystem scores."""

import json

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import average_precision_score

from infra_pulse_research.modeling.subsystem_evaluation import (
    bootstrap_intervals,
    card_mask,
    evaluate_scores,
    select_threshold,
    weighted_average_precision,
)

GAS = "Газовая охрана"
FIRE = "Пожарная охрана"


def _frame(records):
    """records: (day, object_id, system_type, outcome, score, extra dict)."""
    rows = []
    for day, obj, system, outcome, score, *extra in records:
        row = {
            "object_id": obj,
            "system_type": system,
            "issued_at": pd.Timestamp(day),
            "split_period": "validation_2025_h1",
            "outcome": outcome,
            "y": {"positive": 1, "negative": 0}.get(outcome, np.nan),
            "exclusion_reason": "source_coverage" if outcome == "excluded" else None,
            "known_channels": 3,
            "stratum": "onset_after_24h_quiet" if outcome == "positive" else None,
            "new_channel_alarm": False,
            "episode_id": None,
            "episode_start_in_window": False,
            "episode_start_at": pd.NaT,
            "first_alarm_at": pd.NaT,
            "score": score,
        }
        row.update(extra[0] if extra else {})
        rows.append(row)
    return pd.DataFrame(rows)


def _random_frame(seed=3, days=28, objects=12):
    rng = np.random.default_rng(seed)
    records = []
    for d in pd.date_range("2025-01-06", periods=days):
        for obj in range(objects):
            for system in (GAS, FIRE):
                score = float(rng.random())
                outcome = "positive" if rng.random() < 0.15 + 0.5 * score else "negative"
                records.append((d, obj, system, outcome, score, {"other": float(rng.random())}))
    return _frame(records)


def test_card_budget_and_deterministic_tie_break():
    frame = _frame(
        [
            ("2025-01-01", 3, GAS, "negative", 0.5),
            ("2025-01-01", 1, FIRE, "positive", 0.5),
            ("2025-01-01", 1, GAS, "negative", 0.5),
            ("2025-01-01", 2, GAS, "negative", 0.9),
            ("2025-01-02", 5, GAS, "negative", 0.1),
        ]
    )
    mask = card_mask(frame, "score", budget=2)
    # 0.9 first, then the (object_id, system_type)-smallest of the tied 0.5 rows.
    assert mask.tolist() == [False, False, True, True, True]
    shuffled = frame.sample(frac=1, random_state=0)
    assert np.array_equal(card_mask(shuffled, "score", budget=2), mask[shuffled.index])
    assert card_mask(frame, "score", budget=2, threshold=0.6).tolist() == [
        False,
        False,
        False,
        True,
        False,
    ]
    assert card_mask(frame, "score", budget=None, threshold=0.5).sum() == 4
    with pytest.raises(ValueError, match="unique"):
        card_mask(pd.concat([frame, frame.iloc[:1]]), "score", budget=2)


def test_threshold_is_deterministic_and_reproduces_cards():
    frame = _random_frame()
    chosen = select_threshold(frame, "score", budget=5)
    again = select_threshold(frame.sample(frac=1, random_state=1), "score", budget=5)
    assert chosen == again
    cards = card_mask(frame, "score", budget=5, threshold=chosen["threshold"])
    y = (frame.outcome == "positive").to_numpy()
    assert cards.sum() == chosen["cards"]
    assert (cards & y).sum() == chosen["tp"]
    # Brute force over all unique scores gives the same best F1.
    best = 0.0
    for value in np.unique(frame.score):
        c = card_mask(frame, "score", budget=5, threshold=value)
        best = max(best, 2 * (c & y).sum() / (c.sum() + y.sum()))
    assert chosen["f1"] == pytest.approx(best)


def test_evaluation_counts_excluded_separately_and_macro():
    frame = _frame(
        [
            ("2025-01-01", 1, GAS, "positive", 0.9),
            ("2025-01-01", 2, GAS, "positive", 0.1),
            ("2025-01-01", 1, FIRE, "positive", 0.8),
            ("2025-01-01", 2, FIRE, "negative", 0.7),
            ("2025-01-02", 1, GAS, "excluded", np.nan),
        ]
    )
    result = evaluate_scores(frame, "score", threshold=0.5, budget=10)
    json.dumps(result)
    assert result["rows_evaluable"] == 4 and result["rows_excluded"] == 1
    assert result["excluded_by_reason"] == {"source_coverage": 1}
    policy = result["threshold_policy"]
    assert (policy["tp"], policy["fp"], policy["fn"], policy["tn"]) == (2, 1, 1, 0)
    assert policy["by_system"]["gas"]["recall"] == 0.5
    assert policy["by_system"]["fire"]["recall"] == 1.0
    assert policy["by_system"]["temp"]["rows"] == 0
    assert policy["macro_over_systems"]["recall"] == {"value": 0.75, "systems": 2}
    assert policy["macro_over_systems"]["precision"]["value"] == pytest.approx(0.75)
    assert policy["objects"]["macro_recall_over_objects"] == 0.5
    assert policy["objects"]["tp_share_top1_object"] == 1.0
    assert policy["size_buckets"]["2-8"]["rows"] == 4
    assert result["topk_policy"]["cards"] == 4
    assert result["calibration"]["reliability"][9]["rows"] == 1
    assert "object_id" not in json.dumps(result)


def test_episode_capture_and_lead():
    start = pd.Timestamp("2025-01-02 05:00")
    frame = _frame(
        [
            ("2025-01-01", 1, GAS, "negative", 0.9),
            (
                "2025-01-02",
                1,
                GAS,
                "positive",
                0.8,
                {"episode_id": "e1", "episode_start_in_window": True, "episode_start_at": start},
            ),
            (
                "2025-01-02",
                2,
                GAS,
                "positive",
                0.1,
                {
                    "episode_id": "e2",
                    "episode_start_in_window": True,
                    "episode_start_at": start + pd.Timedelta(hours=2),
                    "stratum": "continuation",
                    "new_channel_alarm": True,
                },
            ),
            ("2025-01-03", 1, GAS, "positive", 0.9),
        ]
    )
    episodes = evaluate_scores(frame, "score", threshold=0.5)["threshold_policy"]["episodes"]
    assert (episodes["episodes"], episodes["captured"]) == (2, 1)
    assert episodes["lead_hours_median"] == 5.0
    strata = evaluate_scores(frame, "score", threshold=0.5)["threshold_policy"]["strata"]
    assert strata["continuation"] == {"positives": 1, "tp": 0, "recall": 0.0}
    assert strata["onset_after_24h_quiet"]["recall"] == 1.0
    assert strata["new_channel_alarm"]["positives"] == 1


def test_weighted_average_precision_matches_sklearn():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 200)
    score = np.round(rng.random(200), 1)  # many ties
    weights = rng.integers(0, 3, size=(3, 200)).astype(float)
    ours = weighted_average_precision(y, score, weights)
    for i in range(3):
        assert ours[i] == pytest.approx(average_precision_score(y, score, sample_weight=weights[i]))


def test_bootstrap_is_reproducible_and_paired():
    frame = _random_frame()
    thresholds = {"score": 0.5, "other": None}
    kwargs = {
        "thresholds": thresholds,
        "budget": 5,
        "replicates": 60,
        "deltas": [("score", "other", "signal")],
    }
    first = bootstrap_intervals(frame, ["score", "other"], seed=17, **kwargs)
    assert first == bootstrap_intervals(frame, ["score", "other"], seed=17, **kwargs)
    assert first != bootstrap_intervals(frame, ["score", "other"], seed=18, **kwargs)
    json.dumps(first)
    week = first["schemes"]["iso_week"]
    assert week["blocks"] == 4 and first["schemes"]["object_id"]["blocks"] == 12
    pr = week["scores"]["score"]["pr_auc"]
    assert pr["low"] <= pr["point"] <= pr["high"]
    delta = week["deltas"]["signal"]["pr_auc"]
    assert delta["point"] == pytest.approx(pr["point"] - week["scores"]["other"]["pr_auc"]["point"])
    assert delta["share_positive"] > 0.9
    rows = frame[frame.outcome != "excluded"]
    y = (rows.outcome == "positive").to_numpy()
    assert pr["point"] == pytest.approx(average_precision_score(y, rows.score))
