"""v5 exploration: combinations with the static list, list calibration, best points."""

import runpy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling.sensor_failure_nodes import policy_metrics, select_v4

ROOT = Path(__file__).resolve().parents[2]
X = runpy.run_path(str(ROOT / "data-science/scripts/explore_sensor_failure_v5.py"), run_name="t")
DAY0 = pd.Timestamp("2024-03-04")


def _cards(rows):
    """rows: (day, object, static_list, F2, event_hours or None)."""
    out = []
    for d, obj, count, f2, hours in rows:
        t = DAY0 + pd.Timedelta(days=d)
        out.append(
            {
                "object_id": obj,
                "sensor_type": "s",
                "issued_at": t,
                "outcome": "positive" if hours is not None else "negative",
                "y_card": 1.0 if hours is not None else 0.0,
                "exclusion_reason": None,
                "first_event_start": t + pd.Timedelta(hours=hours) if hours is not None else pd.NaT,
                "static_list": float(count),
                "F2": f2,
                "F2+sel": f2,
            }
        )
    return pd.DataFrame(out)


def test_combinations_use_same_day_ranks_and_ties_never_cross_a_count():
    rows = [(0, 1, 5, 0.1, None), (0, 2, 5, 0.9, None), (0, 3, 4, 0.99, None), (0, 4, 0, 0.5, None)]
    rows += [(1, o, c, s, None) for o, c, s in ((1, 9, 0.2), (2, 1, 0.3), (3, 0, 0.4), (4, 0, 0.1))]
    cards = X["add_combinations"](_cards(rows))
    day = cards[cards.issued_at == DAY0].set_index("object_id")
    # Ties inside one count are ordered by F2; a higher count always stays above.
    tie = day["list, ties by F2"]
    assert tie[2] > tie[1] > tie[3] > tie[4]
    # Rank mean: average of the within-day percentile ranks.
    assert day.loc[3, "rank_mean F2+list"] == pytest.approx(0.5 * (0.5 + 1.0))
    # Scores of another day never change the ranks of this day (point in time).
    other = _cards(rows)
    other.loc[other.issued_at > DAY0, ["static_list", "F2", "F2+sel"]] = [99.0, 0.0, 0.0]
    again = X["add_combinations"](other)
    cols = ["rank_mean F2+list", "rank_mean F2+sel+list", "list, ties by F2"]
    first = cards.issued_at == DAY0
    assert np.allclose(again.loc[first, cols], cards.loc[first, cols])


def test_mask_metrics_match_policy_metrics():
    rows = [(d, o, o, 1.0 / o, 10 if (d, o) == (0, 1) else None) for d in range(8) for o in (1, 2)]
    cards = _cards(rows)
    links = pd.DataFrame(
        {
            "object_id": [1],
            "sensor_type": ["s"],
            "issued_at": [DAY0],
            "event_id": ["e"],
            "candidate_member": [True],
        }
    )
    events = pd.DataFrame(
        {
            "event_id": ["e"],
            "object_id": [1],
            "event_start": [DAY0 + pd.Timedelta(hours=10)],
            "size": [1],
            "sensor_types": [["s"]],
            "system_types": [["x"]],
            "qualifies": [True],
        }
    )
    policy = X["release"](5, 1)
    got = X["mask_metrics"](cards, links, events, select_v4(cards, "F2", policy))
    want = policy_metrics(cards, links, events, "F2", policy)
    assert got == {k: want[k] for k in got}


def test_bins_hold_min_n_and_the_remainder_joins_the_last_bin():
    counts = np.array([0] * 50 + [1] * 10 + [2] * 25 + [3] * 5 + [7] * 3)
    edges = X["bin_edges"](counts, 30)
    assert edges == [0.0, 1.0]
    table = X["bin_table"](counts, np.zeros(len(counts)), edges)
    assert [r["n"] for r in table] == [50, 43] and table[1]["counts"] == [1.0, None]
    # A value below the first build edge goes to the first bin.
    assert X["bin_table"](np.array([-1.0]), np.zeros(1), edges)[0]["n"] == 1


def test_calibration_reports_drops_and_agreement():
    rng = np.random.default_rng(3)
    counts = np.repeat([0, 1, 2], 400)
    rate = np.array([0.1, 0.5, 0.3])[counts]
    build = pd.DataFrame({"count": counts, "y": (rng.random(len(counts)) < rate).astype(float)})
    out = X["calibration"](build, build.copy(), 30)
    assert out["monotone_build"] is False and out["drops_build"] == 1
    assert out["drops_outside_wilson"] == 1  # 0.5 -> 0.3 with n = 400 is outside Wilson
    assert out["test_rate_inside_build_wilson"] == "3/3" and out["weighted_abs_gap"] == 0


def test_best_points_region_f1_and_precision_first():
    def point(p, r):
        return {"mean": {"card_precision": p, "event_recall": r, "f1": 2 * p * r / (p + r)}}

    points = {
        "a": {"5": point(0.80, 0.30), "10": point(0.66, 0.52)},
        "b": {"5": point(0.72, 0.40), "10": point(0.60, 0.60)},
        "ceiling": {"5": point(1.0, 0.9)},
    }
    best = X["best_points"](points)
    assert best["goal_reached"] == []
    assert (best["nearest"]["score"], best["nearest"]["k"]) == ("a", 10)
    assert (best["best_f1"]["score"], best["best_f1"]["k"]) == ("b", 10)
    assert (best["max_recall_with_p70"]["score"], best["max_recall_with_p70"]["k"]) == ("b", 5)
    assert best["per_score"]["a"] == {"best_f1_k": 10, "max_recall_with_p70_k": 5}
