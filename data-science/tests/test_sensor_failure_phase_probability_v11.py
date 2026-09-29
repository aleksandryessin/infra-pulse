"""v11: Poisson–gamma probability layer of the phase list (synthetic checks)."""

import runpy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
V11 = runpy.run_path(str(ROOT / "data-science/scripts/phase_probability_v11.py"), run_name="v11")


def test_gamma_prior_by_moments_and_fallback():
    n = np.array([0, 2, 10, 30])
    e = np.array([365.0, 365.0, 365.0, 365.0])
    a, b = V11["gamma_prior"](n, e)
    m = n.sum() / e.sum()
    v = np.var(n / e, ddof=1) - m * np.mean(1 / e)
    assert a == pytest.approx(m * m / v) and b == pytest.approx(m / v)
    # No heterogeneity beyond Poisson: exponential prior with the pooled mean.
    assert V11["gamma_prior"](np.array([1, 1, 1]), np.array([100.0, 100.0, 100.0])) == (
        1.0,
        pytest.approx(100.0),
    )
    assert V11["gamma_prior"](np.zeros(3), np.full(3, 365.0))[0] == 1.0


def test_probability_is_the_closed_form_of_the_gamma_mixture():
    alpha, beta, n, e, d = 1.2, 30.0, 5, 365.0, 14
    got = V11["p_at_least_one"](np.array([n]), np.array([e]), alpha, beta, d)[0]
    a, b = alpha + n, beta + e
    assert got == pytest.approx(1 - (b / (b + d)) ** a)
    rng = np.random.default_rng(3)
    lam = rng.gamma(a, 1 / b, 400_000)
    assert got == pytest.approx(np.mean(1 - np.exp(-lam * d)), abs=2e-3)
    more = V11["p_at_least_one"](np.array([n + 5]), np.array([e]), alpha, beta, d)[0]
    assert more > got


def test_bayes_by_cutoff_uses_the_pairs_of_each_cutoff_only():
    frame = pd.DataFrame(
        {
            "issued_at": pd.to_datetime(["2024-01-01"] * 3 + ["2024-01-02"] * 3),
            "n": [0, 5, 20, 100, 100, 100],
            "e": 365.0,
        }
    )
    p = V11["bayes_by_cutoff"](frame, "n", "e")
    a, b = V11["gamma_prior"](np.array([0, 5, 20]), np.full(3, 365.0))
    assert p[:3] == pytest.approx(
        V11["p_at_least_one"](np.array([0, 5, 20]), np.full(3, 365.0), a, b)
    )
    assert p[3] == p[4] == p[5]


def test_displayed_values_round_to_five_points_and_cap_above_ninety():
    shown = V11["displayed"](np.array([0.612, 0.638, 0.899, 0.9, 0.901, 0.97]))
    assert shown[:4].tolist() == pytest.approx([0.60, 0.65, 0.90, 0.90])
    assert (shown[4:] > V11["CAP"]).all()
    assert V11["display_label"](0.65) == "65%" and V11["display_label"](shown[5]) == "> 90%"


def test_reliability_and_display_table():
    rng = np.random.default_rng(5)
    p = rng.uniform(0.2, 0.95, 5000)
    y = (rng.random(5000) < p).astype(float)
    r = V11["reliability"](p, y)
    assert r["bins"] == 5 and r["inside_bins"] >= 4 and r["ece"] < 0.03
    biased = V11["reliability"](np.clip(p + 0.2, 0, 1), y)
    assert biased["ece"] > 0.1 and biased["inside_bins"] <= 1
    cards = pd.DataFrame(
        {
            "bayes": [0.6] * 20 + [0.95] * 20,
            "bayes_display": V11["displayed"](np.array([0.6] * 20 + [0.95] * 20)),
            "y_card": [1.0] * 12 + [0.0] * 8 + [1.0] * 10 + [0.0] * 10,
        }
    )
    rows = V11["display_table"](cards, np.ones(len(cards), dtype=bool))
    assert [(r["shown"], r["k"], r["n"]) for r in rows] == [("60%", 12, 20), ("> 90%", 10, 20)]
    assert rows[0]["passes"] and not rows[1]["passes"]


def test_k_of_n_table_uses_the_product_bins():
    counts = np.array([0, 37, 38, 62, 63, 200])
    y = np.array([0, 1, 1, 1, 0, 1], dtype=float)
    table = V11["k_of_n_table"](counts, y)
    assert table == [(0, 37, 2, 1), (38, 62, 2, 2), (63, None, 2, 1)]
    assert V11["k_of_n_forecast"](np.array([10, 50, 70]), table).tolist() == [0.5, 1.0, 0.5]
