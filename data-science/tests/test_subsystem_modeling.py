"""Feature-set, preprocessing and fit-row checks for subsystem-alarm-v1."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling.subsystem_modeling import (
    check_fit_rows,
    drop_uninformative,
    feature_columns,
    fit_feature_set,
    heldout_objects,
    rule_repeat_score,
    rule_share_score,
)

CONFIG = json.loads(
    (Path(__file__).resolve().parents[1] / "configs/subsystem_alarm_v1.json").read_text(
        encoding="utf-8"
    )
)
COLUMNS = [
    "object_id",
    "cat__system_type",
    "cal__issued_dow",
    "own_sig__alarm_rows_24h",
    "own_num__gas_sensor__z_last_max",
    "comp__known_channels",
    "qual__uncovered_days_7d",
    "nbr_obj_sig__temp__alarm_rows_24h",
    "nbr_obj_num__temp__z_last_max",
    "nbr_pk_sig__alarm_rows_24h",
    "nbr_pk_num__z_last_max",
    "name__last_alarm_channel",
    "card__channels",
]


def test_feature_sets_are_nested_and_d_has_no_numeric() -> None:
    sets = {name: set(feature_columns(COLUMNS, CONFIG, name)) for name in "ABCDE"}
    assert sets["A"] < sets["B"] < sets["C"] < sets["E"]
    assert sets["D"] < sets["C"]
    assert sets["C"] - sets["D"] == {c for c in sets["C"] if "_num__" in c}
    assert sets["E"] - sets["C"] == {"name__last_alarm_channel"}
    assert all("object_id" not in s and "card__channels" not in s for s in sets.values())


def test_constant_and_duplicate_columns_use_development_only() -> None:
    dev = pd.DataFrame({"a": [1, 1, 1], "b": [1, 2, 3], "c": [1, 2, 3], "d": [None, 1, None]})
    kept, dropped = drop_uninformative(dev, ["a", "b", "c", "d"])
    assert kept == ["b", "d"]
    assert dropped == {"constant": ["a"], "duplicate_of": {"c": "b"}}


def _frame(rows: int = 400, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.date_range("2024-01-01", periods=rows // 4, freq="D")
    frame = pd.DataFrame(
        {
            "object_id": np.tile([1, 2, 3, 4], rows // 4),
            "system_type": "Пожарная охрана",
            "issued_at": np.repeat(days, 4),
        }
    )
    signal = rng.normal(size=rows)
    frame["own_sig__alarm_rows_24h"] = (signal > 0.3).astype(float) * rng.integers(1, 5, rows)
    frame["own_sig__alarm_channel_share_24h"] = np.clip(signal, 0, None) / 3
    frame["own_sig__hours_since_last_alarm"] = np.where(signal > 0, rng.uniform(0, 24, rows), None)
    frame["own_num__gas_sensor__z_last_max"] = rng.normal(size=rows)
    frame["cat__system_type"] = "Пожарная охрана"
    frame["cat__last_text_state"] = rng.choice(["Норма", "Обнаружен дым", None], rows)
    frame["comp__known_channels"] = 5.0
    frame["y"] = (signal + rng.normal(scale=0.5, size=rows) > 0.5).astype(float)
    frame["outcome"] = np.where(frame.y == 1, "positive", "negative")
    frame["split_period"] = "development"
    return frame


def test_excluded_rows_never_enter_fit() -> None:
    frame = _frame()
    frame.loc[:9, "outcome"] = "excluded"
    frame.loc[:9, "y"] = np.nan
    mask = check_fit_rows(frame)
    assert not mask[:10].any() and mask[10:].all()
    frame.loc[20, "y"] = np.nan
    with pytest.raises(ValueError):
        check_fit_rows(frame)


def test_fit_is_deterministic_and_scores_every_row() -> None:
    frame = _frame()
    frame.loc[:3, "split_period"] = "validation_2025_h1"
    first = fit_feature_set(frame, CONFIG, "B")
    second = fit_feature_set(frame, CONFIG, "B")
    for model in ("logreg", "catboost"):
        assert len(first["scores"][model]) == len(frame)
        np.testing.assert_allclose(first["scores"][model], second["scores"][model])
    assert "cat__system_type" not in first["columns"]  # constant on development
    assert first["meta"]["dropped_constant"] >= 2


def test_rule_scores_rank_share_then_recency() -> None:
    frame = pd.DataFrame(
        {
            "own_sig__alarm_channel_share_24h": [0.5, 0.5, 0.9, 0.0, None],
            "own_sig__hours_since_last_alarm": [10.0, 2.0, 30.0, None, None],
            "own_sig__alarm_rows_24h": [1, 3, 2, 0, None],
        }
    )
    score = rule_share_score(frame)
    assert list(np.argsort(-score, kind="stable")) == [2, 1, 0, 3, 4]
    assert score[3] == score[4]
    assert list(rule_repeat_score(frame)) == [1, 1, 1, 0, 0]


def test_heldout_split_is_fixed_by_hash() -> None:
    ids = pd.Series(range(1, 201))
    first, second = heldout_objects(ids, CONFIG), heldout_objects(ids.sample(frac=1), CONFIG)
    assert first.sum() == second.sum()
    assert 20 <= first.sum() <= 60
