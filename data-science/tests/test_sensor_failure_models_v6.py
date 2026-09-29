"""v6: the frozen plan, candidates without gradient training (Bayes, Hawkes), honest
recall, the paired bootstrap from block sums and the fresh-pair slice."""

import json
import runpy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling import sensor_failure_evaluation as ev

ROOT = Path(__file__).resolve().parents[2]
V6 = runpy.run_path(str(ROOT / "data-science/scripts/tune_sensor_failure_v6.py"), run_name="t")
V5 = V6["V5"]
CONFIG = json.loads(
    (ROOT / "data-science/configs/sensor_failure_tuning_v6.json").read_text(encoding="utf-8")
)
DAY0 = pd.Timestamp("2024-03-04")
RULE = {"op": ">=", "seconds": 2}


def test_plan_is_frozen_with_seven_candidates_and_no_holdout():
    assert CONFIG["status"].startswith("frozen_before_fit")
    families = [c["family"] for c in CONFIG["candidates"].values()]
    assert families[:4] == list(V6["FAMILIES"])
    assert families[4:] == ["Bayes Poisson-gamma", "Stacking", "Hawkes"]
    assert CONFIG["seeds"] == [17, 18, 19] and CONFIG["threads"] == 6
    assert CONFIG["horizons"]["168h"]["budgets"] == [3, 7]
    assert CONFIG["horizons"]["336h"]["budgets"] == [10, 12, 14]
    ends = [b for fold in CONFIG["folds"] for part in fold.values() for _, b in part]
    assert max(ends) <= "2025-01-01"
    assert CONFIG["mlflow"]["experiment"] == "sensor-failure-models-v6"
    assert CONFIG["fresh_slice"]["lookback_days"] == {"primary": 14, "secondary": 7}
    assert V6["family_params"](CONFIG, "LightGBM") == {
        "objective": "binary",
        "num_leaves": 31,
        "learning_rate": 0.05,
        "n_estimators": 500,
        "n_jobs": 6,
    }
    assert V6["family_params"](CONFIG, "CatBoost YetiRank")["loss_function"] == "YetiRank"
    assert V6["family_params"](CONFIG, "MLP")["hidden_layer_sizes"] == [64, 32]


# -- history of a pair ---------------------------------------------------------------------


def _members(starts, obj=1, sensor="s"):
    """One single-member qualifying event per start."""
    return pd.DataFrame(
        {
            "event_id": [f"e{i}" for i in range(len(starts))],
            "channel_id": 10,
            "object_id": obj,
            "sensor_type": sensor,
            "start_at": pd.to_datetime(pd.Series(starts), format="ISO8601"),
            "qualifying": True,
        }
    )


def test_pair_event_times_count_as_the_static_list_and_drop_2021():
    members = pd.concat(
        [
            _members(["2021-05-01", "2023-02-01 10:00", "2023-03-01"], obj=1),
            _members(["2023-02-20"], obj=2).assign(event_id="x0"),
        ]
    )
    times = V6["pair_event_times"](members, RULE)
    assert sorted(times["count_at"].dt.year) == [2023, 2023, 2023]
    cards = pd.DataFrame(
        {
            "object_id": [1, 1, 2],
            "sensor_type": "s",
            "issued_at": pd.to_datetime(["2023-02-01", "2023-02-02", "2023-03-02"]),
        }
    )
    # The event of 2023-02-01 10:00 counts from 10:00:02 (>= 2 s), so not at 00:00.
    listed = ev.static_list_scores(cards, members, RULE).to_numpy()
    assert list(listed) == [0.0, 1.0, 1.0]
    counted = pd.to_datetime(times["count_at"]).dt.floor("s")
    assert pd.Timestamp("2023-02-01 10:00:01") in set(counted)


def test_observed_time_skips_2021_and_decays():
    days = V6["to_days"]
    start = days(["2020-12-01"])[0]
    t = days(["2022-01-11"])
    # 31 days of December 2020 and 10 days of January 2022.
    assert V6["decayed_exposure"](start, t, 0.0)[0] == pytest.approx(41.0)
    assert V6["observed_parts"](start, t[0]) == [
        (start, V6["GAP_DAYS"][0]),
        (V6["GAP_DAYS"][1], t[0]),
    ]
    rate = np.log(2) / 90
    grid = np.linspace(V6["GAP_DAYS"][1], t[0], 200001)
    numeric = np.trapezoid(np.exp(-rate * (t[0] - grid)), grid)
    grid0 = np.linspace(start, V6["GAP_DAYS"][0], 200001)
    numeric += np.trapezoid(np.exp(-rate * (t[0] - grid0)), grid0)
    assert V6["decayed_exposure"](start, t, rate)[0] == pytest.approx(numeric, rel=1e-6)


def test_decayed_sum_uses_only_events_before_the_cutoff():
    events = np.array([1.0, 5.0, 10.0])
    got = V6["decayed_sum"](events, np.array([5.0, 10.0, 10.5]), 0.5)
    assert got[0] == pytest.approx(np.exp(-2.0))
    assert got[1] == pytest.approx(np.exp(-4.5) + np.exp(-2.5))
    assert got[2] == pytest.approx(np.exp(-4.75) + np.exp(-2.75) + np.exp(-0.25))


def test_gamma_prior_by_moments():
    rng = np.random.default_rng(3)
    rates = rng.gamma(shape=0.5, scale=0.02, size=4000)
    exposure = rng.uniform(200, 700, size=4000)
    counts = rng.poisson(rates * exposure)
    alpha, beta = V6["gamma_prior"](counts, exposure)
    assert alpha / beta == pytest.approx(0.01, rel=0.05)
    assert alpha == pytest.approx(0.5, rel=0.15)
    assert V6["gamma_prior"](np.array([1, 2]), np.array([100.0, 100.0])) is None
    # No overdispersion: equal rates give v <= 0, so no prior.
    assert V6["gamma_prior"](np.array([5, 5, 5, 5]), np.full(4, 500.0)) is None


def _bayes_world():
    members = pd.concat(
        [
            _members(["2023-01-10", "2023-02-15"], obj=1),
            _members(["2023-03-20"], obj=2).assign(event_id="z"),
        ]
    )
    times = V6["pair_event_times"](members, RULE)
    starts = pd.DataFrame(
        {
            "object_id": [1, 2, 3, 4],
            "sensor_type": "s",
            "first_card": pd.to_datetime(["2022-01-01", "2022-01-01", "2022-01-01", "2023-03-25"]),
        }
    )
    starts["start"] = V6["to_days"](starts["first_card"])
    cards = pd.DataFrame(
        {
            "object_id": [1, 1, 2, 3, 4],
            "sensor_type": "s",
            "issued_at": pd.to_datetime(
                ["2023-02-15", "2023-02-16", "2023-04-01", "2023-04-01", "2023-04-01"]
            ),
        }
    )
    params = {"_pooled": {"alpha": 0.5, "beta": 50.0}, "_half_life": 90}
    return cards, times, starts, params


def test_bayes_score_is_point_in_time_and_shrinks_short_histories():
    cards, times, starts, params = _bayes_world()
    got = V6["bayes_scores"](cards, times, starts, params, 7)
    # The event of 2023-02-15 counts only from 2 s after its start: card 1 does not see it.
    assert got[1] > got[0]
    later = pd.concat(
        [times, times.iloc[[0]].assign(count_at=pd.Timestamp("2023-06-01"), event_id="late")]
    )
    assert np.allclose(V6["bayes_scores"](cards, later, starts, params, 7), got)
    # No events: the pair observed for 7 days is closer to the prior than one observed a year.
    assert got[4] > got[3]
    a, b = (
        0.5,
        50.0
        + V6["decayed_exposure"](
            starts["start"][2], V6["to_days"](cards.issued_at[3:4]), np.log(2) / 90
        )[0],
    )
    assert got[3] == pytest.approx(1 - (b / (b + 7)) ** a)


# -- Hawkes ---------------------------------------------------------------------------------


def _history(events, start, end):
    events = np.sort(np.asarray(events, dtype=float))
    parts = V6["observed_parts"](start, end)
    inside = np.zeros(len(events), dtype=bool)
    for a, b in parts:
        inside |= (events >= a) & (events < b)
    return {
        "sensor_type": "s",
        "events": events,
        "parts": parts,
        "observed": inside,
        "exposure": float(sum(b - a for a, b in parts)),
        "n": int(inside.sum()),
    }


def test_hawkes_loglik_matches_a_direct_computation_across_the_gap():
    gap0, gap1 = V6["GAP_DAYS"]
    h = _history([gap0 - 3.0, gap0 - 1.0, gap1 + 2.0], gap0 - 10.0, gap1 + 20.0)
    mu, alpha, tau = 0.05, 0.4, 3.0
    got = V6["hawkes_loglik"](mu, alpha, tau, V6["hawkes_data"]([h]))
    e = h["events"]

    def lam(t):
        past = e[e < t]
        return mu + alpha * np.exp(-(t - past) / tau).sum()

    log_part = sum(np.log(lam(t)) for t in e)
    grid_a = np.linspace(gap0 - 10.0, gap0, 400001)
    grid_b = np.linspace(gap1, gap1 + 20.0, 400001)
    integral = sum(np.trapezoid([lam(t) for t in g[::100]], g[::100]) for g in (grid_a, grid_b))
    assert got == pytest.approx(log_part - integral, rel=1e-3)


def _simulate_hawkes(mu, alpha, tau, horizon, rng):
    """Ogata thinning on [0, horizon)."""
    events, t = [], 0.0
    while True:
        past = np.array(events)
        bound = mu + alpha * np.exp(-(t - past) / tau).sum() + alpha
        t += rng.exponential(1.0 / bound)
        if t >= horizon:
            return np.array(events)
        lam = mu + alpha * np.exp(-(t - past) / tau).sum()
        if rng.random() * bound <= lam:
            events.append(t)


def test_hawkes_mle_recovers_simulated_parameters():
    rng = np.random.default_rng(11)
    mu, alpha, tau = 0.01, 0.1, 5.0
    histories = []
    for _ in range(40):
        e = _simulate_hawkes(mu, alpha, tau, 700.0, rng)
        histories.append(_history(e, 0.0, 700.0))
    fit = V6["fit_hawkes_group"](V6["hawkes_data"](histories))
    assert fit["mu"] == pytest.approx(mu, rel=0.3)
    assert fit["branching"] == pytest.approx(alpha * tau, rel=0.3)
    assert fit["tau"] == pytest.approx(tau, rel=0.5)


def test_hawkes_score_is_the_window_probability_and_point_in_time():
    members = _members(["2023-03-01 12:00", "2023-03-05 08:00"])
    times = V6["pair_event_times"](members, RULE)
    cards = pd.DataFrame(
        {
            "object_id": 1,
            "sensor_type": "s",
            "issued_at": pd.to_datetime(["2023-03-05", "2023-03-06"]),
        }
    )
    params = {"_pooled": {"mu": 0.02, "alpha": 0.3, "tau": 2.0}}
    got = V6["hawkes_scores"](cards, times, params, 7)
    t = V6["to_days"](cards.issued_at)
    e = V6["to_days"](times.count_at)
    excite0 = np.exp(-(t[0] - e[0]) / 2.0)
    expected0 = 1 - np.exp(-(0.02 * 7 + 0.3 * 2.0 * (1 - np.exp(-3.5)) * excite0))
    assert got[0] == pytest.approx(expected0)
    assert got[1] > got[0]


# -- honest recall and bootstrap --------------------------------------------------------


def _world(seed=5, days=42, objects=6):
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(days):
        for o in range(objects):
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
                    "c": float(rng.random()),
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


def test_honest_capture_matches_exploration_3_and_drops_events_after_release():
    p3 = runpy.run_path(
        str(ROOT / "data-science/scripts/explore_sensor_failure_v5_part3.py"), run_name="t3"
    )
    cards, links, events = _world()
    for score in ("a", "b"):
        mask = V6["select"](cards, score, 7, 3)
        got = V6["honest_capture"](cards, links, events, mask, 7)
        assert got["captured_open"].mean() == pytest.approx(
            p3["recall_open"](cards, links, events, mask, 7)
        )
        assert (got["captured_open"] <= got["captured"]).all()
        assert (got.loc[got.captured_open, "lead_open"] >= 0).all()


def test_block_bootstrap_equals_v5_mask_bootstrap_and_averages_groups():
    cards, links, events = _world()
    masks = {s: V6["select"](cards, s, 7, 3) for s in ("a", "b", "c")}
    v5 = V5["bootstrap_masks"](
        cards, links, events, masks, [("a", "c", "a - c")], replicates=50, seed=17
    )
    evaluable = cards["outcome"].isin(ev.EVALUABLE).to_numpy()
    rows = cards[evaluable].reset_index(drop=True)
    outcomes = {s: ev.event_outcomes(cards, links, events, m) for s, m in masks.items()}
    base = outcomes["a"][outcomes["a"]["evaluable"]].reset_index(drop=True)
    captured = {
        s: base[["event_id"]]
        .merge(o[["event_id", "captured"]], on="event_id")["captured"]
        .to_numpy(dtype=float)
        for s, o in outcomes.items()
    }
    sel = {s: m[evaluable].astype(float) for s, m in masks.items()}
    got = V6["block_bootstrap"](
        rows, sel, base, captured, {"ab": ["a", "b"]}, [("a", "c"), ("ab", "c")], replicates=50
    )
    for scheme in ("iso_week", "object"):
        ours, theirs = got["schemes"][scheme], v5["schemes"][scheme]
        for m, m5 in (("card_precision", "card_precision"), ("event_recall_open", "event_recall")):
            for key in ("point", "low", "high"):
                assert ours["deltas"]["a - c"][m][key] == pytest.approx(
                    theirs["deltas"]["a - c"][m5][key]
                )
                assert ours["scores"]["b"][m][key] == pytest.approx(theirs["scores"]["b"][m5][key])
        pa, pb = (ours["scores"][s]["card_precision"]["point"] for s in ("a", "b"))
        assert ours["scores"]["ab"]["card_precision"]["point"] == pytest.approx((pa + pb) / 2)


def test_within_day_ranks_and_mlp_inputs():
    frame = pd.DataFrame(
        {
            "issued_at": [DAY0] * 3 + [DAY0 + pd.Timedelta(days=1)] * 2,
            "hist__x": [1.0, 3.0, 2.0, 100.0, np.nan],
            "cat__sensor_type": ["a", "b", "a", "b", "c"],
        }
    )
    ranks = V6["within_day_ranks"](frame, ["hist__x"])
    assert ranks["hist__x"].tolist()[:4] == pytest.approx([1 / 3, 1.0, 2 / 3, 1.0])
    assert np.isnan(ranks["hist__x"].iloc[4])
    inputs = V6["Inputs"](frame.iloc[:4], ["cat__sensor_type", "hist__x"], ranks.iloc[:4])
    assert inputs.categories == {"cat__sensor_type": ["a", "b"]} and inputs.missing == []
    inputs = V6["Inputs"](frame, ["cat__sensor_type", "hist__x"], ranks)
    x = inputs.mlp(frame, ranks)
    # rank, missing indicator, one-hot a/b/c
    assert x.shape == (5, 5)
    assert x[4].tolist() == [0.0, 1.0, 0.0, 0.0, 1.0]
    trees = inputs.trees(frame.assign(cat__sensor_type=["a", "b", "a", "b", "zzz"]))
    assert trees["cat__sensor_type"].isna().tolist() == [False] * 4 + [True]


def test_abort_rule_needs_every_budget_below_persistence():
    persistence = {3: 0.5, 7: 0.45}
    p = {
        "LightGBM": {3: 0.49, 7: 0.40},
        "XGBoost": {3: 0.55, 7: 0.40},
        "MLP": {3: 0.30, 7: 0.20},
    }
    assert V6["aborted_families"](p, persistence) == ["LightGBM", "MLP"]


def test_best_ml_breaks_ties_in_family_order():
    def boot(values):
        return {
            "schemes": {
                "iso_week": {
                    "scores": {f: {"card_precision": {"point": v}} for f, v in values.items()}
                }
            }
        }

    budgets = [3, 7]
    boots = {3: boot({"XGBoost": 0.6, "MLP": 0.6}), 7: boot({"XGBoost": 0.5, "MLP": 0.5})}
    fams = {"XGBoost": ["XGBoost s17"], "MLP": ["MLP s17"]}
    assert V6["best_ml"](boots, fams, budgets) == "XGBoost"
    boots[7]["schemes"]["iso_week"]["scores"]["MLP"]["card_precision"]["point"] = 0.51
    assert V6["best_ml"](boots, fams, budgets) == "MLP"


def test_fresh_events_have_no_event_of_their_pairs_in_the_lookback():
    members = pd.concat(
        [
            _members(["2024-03-01"], obj=1).assign(event_id="old"),
            _members(["2024-03-10"], obj=1).assign(event_id="near"),
            _members(["2024-03-30"], obj=1).assign(event_id="far"),
            _members(["2024-03-10"], obj=2).assign(event_id="other"),
        ]
    )
    events = pd.DataFrame(
        {
            "event_id": ["old", "near", "far", "other"],
            "event_start": pd.to_datetime(["2024-03-01", "2024-03-10", "2024-03-30", "2024-03-10"]),
        }
    )
    assert V6["fresh_event_ids"](events, members, 14) == {"old", "far", "other"}
    assert V6["fresh_event_ids"](events, members, 7) == {"old", "near", "far", "other"}


def test_calibration_bins_and_ece():
    prob = np.array([0.005, 0.005, 0.15, 0.15, 0.15, 0.15])
    y = np.array([0, 0, 1, 0, 0, 0])
    out = V6["calibration"](prob, y)
    assert [b["n"] for b in out["bins"]] == [2, 4]
    assert out["bins"][1]["share_with_event"] == pytest.approx(0.25)
    assert out["ece"] == pytest.approx((2 * 0.005 + 4 * 0.1) / 6)
