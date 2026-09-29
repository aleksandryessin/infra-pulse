"""v5 exploration 3: lead of the model against the list, best point by the worse fold."""

import json
import runpy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling.sensor_failure_nodes import select_v4

ROOT = Path(__file__).resolve().parents[2]
P3 = runpy.run_path(
    str(ROOT / "data-science/scripts/explore_sensor_failure_v5_part3.py"), run_name="t"
)
CONFIG = json.loads(
    (ROOT / "data-science/configs/sensor_failure_tuning_v5_7d.json").read_text(encoding="utf-8")
)
DAY0 = pd.Timestamp("2024-03-04")


def test_config_has_eight_fits_and_no_holdout():
    assert P3["model_columns"](CONFIG) == ["F2 s17", "F2 s18", "F2 s19", "F2+sel s17"]
    assert CONFIG["fit_count"] == 8 == 2 * len(P3["model_columns"](CONFIG))
    assert CONFIG["catboost"]["thread_count"] == 6 and "random_seed" not in CONFIG["catboost"]
    ends = [b for fold in CONFIG["folds"] for part in fold.values() for _, b in part]
    assert max(ends) <= "2025-01-01"


def test_weighted_median_is_the_lower_median():
    values = np.array([1.0, 2.0, 3.0, 4.0])
    got = P3["weighted_median"](values, np.array([[1, 1, 1, 1], [0, 0, 1, 1], [0, 0, 0, 0]]))
    assert got[0] == 2.0 and got[1] == 3.0 and np.isnan(got[2])


def _world():
    rng = np.random.default_rng(5)
    rows = []
    for d in range(42):
        for o in range(6):
            t = DAY0 + pd.Timedelta(days=d)
            hit = rng.random() < 0.15
            rows.append(
                {
                    "object_id": o,
                    "sensor_type": "s",
                    "system_type": "x",
                    "issued_at": t,
                    "outcome": "positive" if hit else "negative",
                    "y_card": 1.0 if hit else 0.0,
                    "exclusion_reason": None,
                    "first_event_start": t + pd.Timedelta(hours=float(rng.integers(1, 160)))
                    if hit
                    else pd.NaT,
                    "object_failing_past_24h": False,
                    "weekend": False,
                    "a": float(rng.random()),
                    "b": float(rng.random()),
                }
            )
    cards = pd.DataFrame(rows)
    pos = cards[cards.y_card == 1]
    links = pd.DataFrame(
        {
            "object_id": pos.object_id,
            "sensor_type": "s",
            "issued_at": pos.issued_at,
            "event_id": [f"e{i}" for i in range(len(pos))],
            "candidate_member": True,
        }
    )
    events = pd.DataFrame(
        {
            "event_id": links.event_id.to_numpy(),
            "object_id": pos.object_id.to_numpy(),
            "event_start": pos.first_event_start.to_numpy(),
            "size": 1,
            "sensor_types": [["s"]] * len(pos),
            "system_types": [["x"]] * len(pos),
            "qualifies": True,
        }
    )
    return cards, links, events


def test_lead_bootstrap_is_paired_deterministic_and_matches_points():
    cards, links, events = _world()
    policy = P3["X"]["release"](7, 2)
    masks = {s: select_v4(cards, s, policy) for s in ("a", "b")}
    out = P3["lead_bootstrap"](cards, links, events, masks, [("a", "b")], replicates=60)
    assert out == P3["lead_bootstrap"](cards, links, events, masks, [("a", "b")], replicates=60)
    stats = out["stats"]
    delta = out["schemes"]["iso_week"]["deltas"]["a - b"]
    assert delta["point"] == pytest.approx(stats["a"]["median"] - stats["b"]["median"])
    assert delta["low"] <= delta["high"]
    assert set(out["schemes"]) == {"iso_week", "object"}


def test_best_point_uses_the_worse_fold():
    def v(folds):
        return {
            "folds": [
                {"card_precision": p, "event_recall": r, "new_cards_per_day": 1.0} for p, r in folds
            ],
            "mean": {},
        }

    points = {
        # a is better on average, b is better in its worse fold
        "a": {"5": v([(0.95, 0.60), (0.55, 0.40)])},
        "b": {"5": v([(0.72, 0.49), (0.71, 0.49)])},
        "ceiling": {"5": v([(1.0, 1.0), (1.0, 1.0)])},
    }
    best = P3["best_by_worst_fold"](points)
    assert (best["score"], best["k"]) == ("b", 5)
    assert best["gap_worst_fold"] == pytest.approx(0.01)
    assert P3["best_by_worst_fold"](points, {7}) is None


def test_open_card_recall_drops_events_after_release():
    t = DAY0
    cards = pd.DataFrame(
        {
            "object_id": [1, 1, 1],
            "sensor_type": "s",
            "system_type": "x",
            "issued_at": [t, t + pd.Timedelta(days=1), t + pd.Timedelta(days=2)],
            "outcome": ["positive", "positive", "positive"],
            "y_card": 1.0,
            "exclusion_reason": None,
            "first_event_start": [
                t + pd.Timedelta(hours=10),
                t + pd.Timedelta(days=3, hours=5),
                t + pd.Timedelta(days=3, hours=5),
            ],
            "object_failing_past_24h": False,
            "weekend": False,
            "score": [0.9, 0.1, 0.1],
        }
    )
    # Event e1 on day 0, event e2 on day 3; the day-0 card's 7-day window holds both.
    links = pd.DataFrame(
        {
            "object_id": 1,
            "sensor_type": "s",
            "issued_at": [t, t, t + pd.Timedelta(days=1), t + pd.Timedelta(days=2)],
            "event_id": ["e1", "e2", "e2", "e2"],
            "candidate_member": True,
        }
    )
    events = pd.DataFrame(
        {
            "event_id": ["e1", "e2"],
            "object_id": 1,
            "event_start": [t + pd.Timedelta(hours=10), t + pd.Timedelta(days=3, hours=5)],
            "size": 1,
            "sensor_types": [["s"], ["s"]],
            "system_types": [["x"], ["x"]],
            "qualifies": True,
        }
    )
    only_first = np.array([True, False, False])
    # Protocol: the day-0 card captures both events; open card: e2 starts after release.
    assert P3["recall_open"](cards, links, events, only_first, 7) == pytest.approx(0.5)
    # A card re-issued after the release (day 2) is open when e2 starts.
    reissued = np.array([True, False, True])
    assert P3["recall_open"](cards, links, events, reissued, 7) == pytest.approx(1.0)


def test_scores_add_rank_combinations_per_seed():
    cards, _, _ = _world()
    cards["static_list"] = cards["a"].round(1)
    cards["persistence"] = cards["b"]
    cards["F2 s17"] = cards["b"]
    out = P3["add_scores"](cards, ["F2 s17"])
    assert {"list+persistence", "rank_mean F2 s17+list"} <= set(out.columns)
    assert (out["list+persistence"] == out["rank_mean F2 s17+list"]).all()
