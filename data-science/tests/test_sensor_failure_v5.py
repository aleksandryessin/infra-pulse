"""v5: rolling list with release on the event, mask bootstrap and the frozen plan."""

import json
import runpy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling.sensor_failure_evaluation import select_policy
from infra_pulse_research.modeling.sensor_failure_nodes import (
    policy_metrics,
    select_rolling_release,
    select_v4,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads(
    (ROOT / "data-science/configs/sensor_failure_tuning_v5.json").read_text(encoding="utf-8")
)
RUNNER = runpy.run_path(str(ROOT / "data-science/scripts/tune_sensor_failure_v5.py"), run_name="t")
DAY0 = pd.Timestamp("2024-03-04")


def _cards(spec):
    """spec: (day, object, score, event_hours_after_issue or None)."""
    rows = []
    for d, obj, score, hours in spec:
        t = DAY0 + pd.Timedelta(days=d)
        rows.append(
            {
                "object_id": obj,
                "sensor_type": "s",
                "system_type": "x",
                "issued_at": t,
                "outcome": "positive" if hours is not None else "negative",
                "y_card": 1.0 if hours is not None else 0.0,
                "exclusion_reason": None,
                "first_event_start": t + pd.Timedelta(hours=hours) if hours is not None else pd.NaT,
                "score": score,
                "object_failing_past_24h": False,
                "weekend": False,
            }
        )
    return pd.DataFrame(rows)


def test_release_frees_the_place_from_the_next_cutoff_after_the_event():
    # max 1 open, window 5 days; object 1 (day 0) has its event on day 1 at 10:00.
    spec = [(d, 1, 0.9, 34 if d == 0 else None) for d in range(6)]
    spec += [(d, 2, 0.5, None) for d in range(6)]
    cards = _cards(spec)
    policy = {"type": "rolling_release", "max_open": 1, "window_days": 5}
    issued = cards[select_rolling_release(cards, "score", policy)]
    # Day 0: object 1. The event is on day 1 10:00, so the place is free on day 2.
    assert list(zip(issued.issued_at.dt.day - DAY0.day, issued.object_id, strict=True)) == [
        (0, 1),
        (2, 1),
    ]
    plain = select_policy(cards, "score", {"type": "rolling", "max_open": 1, "window_days": 5})
    assert cards[plain].issued_at.dt.day.tolist() == [DAY0.day, DAY0.day + 5]
    # Without events both policies coincide.
    quiet = _cards([(d, o, 0.5 + 0.1 * o, None) for d in range(8) for o in (1, 2, 3)])
    a = select_rolling_release(quiet, "score", {**policy, "max_open": 2})
    b = select_policy(quiet, "score", {"type": "rolling", "max_open": 2, "window_days": 5})
    assert (a == b).all()
    assert (
        select_v4(quiet, "score", {"type": "rolling", "max_open": 2, "window_days": 5}) == b
    ).all()


def test_release_policy_metrics_count_cards_per_day():
    spec = [(d, o, 1.0 / o, 10 if (d, o) == (0, 1) else None) for d in range(10) for o in (1, 2, 3)]
    cards = _cards(spec)
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
    m = policy_metrics(
        cards, links, events, "score", {"type": "rolling_release", "max_open": 2, "window_days": 5}
    )
    assert m["event_recall"] == 1.0 and m["lead_hours_median"] == pytest.approx(10.0)
    assert m["cards_selected"] > 2  # released places are refilled


def test_mask_bootstrap_is_paired_and_matches_points():
    rng = np.random.default_rng(1)
    spec = [
        (d, o, float(rng.random()), 5 if rng.random() < 0.2 else None)
        for d in range(28)
        for o in range(6)
    ]
    cards = _cards(spec)
    cards["other"] = rng.random(len(cards))
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
            "event_id": links.event_id,
            "object_id": pos.object_id.to_numpy(),
            "event_start": (pos.issued_at + pd.Timedelta(hours=5)).to_numpy(),
            "size": 1,
            "sensor_types": [["s"]] * len(pos),
            "system_types": [["x"]] * len(pos),
            "qualifies": True,
        }
    )
    policy = {"type": "topk", "k": 2}
    masks = {c: select_v4(cards, c, policy) for c in ("score", "other")}
    out = RUNNER["bootstrap_masks"](
        cards, links, events, masks, [("score", "other", "d")], replicates=50
    )
    assert out == RUNNER["bootstrap_masks"](
        cards, links, events, masks, [("score", "other", "d")], replicates=50
    )
    week = out["schemes"]["iso_week"]
    point = policy_metrics(cards, links, events, "score", policy)
    assert week["scores"]["score"]["card_precision"]["point"] == pytest.approx(
        point["card_precision"]
    )
    assert week["scores"]["score"]["event_recall"]["point"] == pytest.approx(point["event_recall"])
    d = week["deltas"]["d"]["card_precision"]
    assert d["point"] == pytest.approx(
        week["scores"]["score"]["card_precision"]["point"]
        - week["scores"]["other"]["card_precision"]["point"]
    )


def test_plan_is_frozen_with_ten_fits_and_policies():
    assert CONFIG["status"].startswith("frozen_before_fit")
    assert CONFIG["stage1"]["fit_count"] == 10
    assert len(CONFIG["stage1"]["fits"]) * len(CONFIG["stage1"]["folds"]) == 10
    assert CONFIG["stage1"]["catboost"]["thread_count"] == 6
    assert CONFIG["stage1"]["target"] == "connection_loss_ge2s"
    pol = RUNNER["policies"](10)
    assert pol["release_open10"] == {"type": "rolling_release", "max_open": 10, "window_days": 10}
    assert pol["norelease_open5"]["type"] == "rolling"
    sel = CONFIG["stage1"]["feature_sets"]["F2+sel"]
    assert "e4__" not in sel and "r4__" not in sel and "w4__" in sel
