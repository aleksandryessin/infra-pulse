"""Card and event evaluation for ``sensor-failure-model-v1`` (pandas/numpy/sklearn).

Inputs are the tables of :func:`sensor_failure_target.label_target_month` for
one target, horizon and period: ``cards`` (all card rows of the period,
including excluded ones), ``links`` (event × card at a cutoff) and ``events``.

- A card score is the maximum score of its candidate channels.
- Each cutoff day (Monday for 168 h) gets the top ``budget`` evaluable cards;
  ties go to ``object_id`` then ``sensor_type``.
- An event is *evaluable* if one of its cards has an evaluable window. It is
  *capturable* if a member is a candidate channel of an evaluable card, and
  *captured* if such a card is selected. Lead = event start − earliest
  capturing cutoff.

Issue policies (:func:`select_policy`): top-k per cutoff, every d days, or a
rolling list with at most m open cards. Unit B (:func:`to_unit_b`) groups cards
by object × system type. :func:`ceilings` applies a perfect ranking under the
same policy.

Outputs are JSON-safe aggregates without object or channel identifiers.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score

from infra_pulse_research.modeling.subsystem_evaluation import (
    _interval,
    _py,
    calibration,
    weighted_average_precision,
)

CARD_KEYS = ["object_id", "sensor_type", "issued_at"]
EVALUABLE = ("positive", "negative")
WINDOW_OK = ("no_candidate_channels",)
CALIBRATION_PERIOD = "calibration_and_threshold"
BLOCKS = ("iso_week", "object_id")
_BATCH = 50
# Optional boolean event columns (see :func:`event_day_flags`) reported as slices.
EVENT_FLAGS = (
    "prior_same_day_object_event",
    "starts_after_cutoff_hour",
    "prior_object_event_before_cutoff",
)


def card_scores(channels: pd.DataFrame, score_cols: list[str]) -> pd.DataFrame:
    """Maximum score over candidate channels per object × sensor type × cutoff."""
    candidates = channels[channels["candidate"].astype(bool)]
    return candidates.groupby(CARD_KEYS, sort=False)[list(score_cols)].max().reset_index()


def attach_card_scores(
    cards: pd.DataFrame, channels: pd.DataFrame, score_cols: list[str]
) -> pd.DataFrame:
    """Cards with card scores; cards without scored candidate channels get NaN."""
    scores = card_scores(channels, score_cols)
    merged = cards.drop(columns=[c for c in score_cols if c in cards]).merge(
        scores, on=CARD_KEYS, how="left", validate="one_to_one"
    )
    if len(merged) != len(cards):
        raise ValueError("card merge changed the number of rows")
    return merged


def _evaluable(cards: pd.DataFrame) -> np.ndarray:
    return cards["outcome"].isin(EVALUABLE).to_numpy()


def day_off(issued_at: pd.Series, holidays=None) -> np.ndarray:
    """Saturday/Sunday of the issue day, plus listed holiday dates if a verified
    calendar is given (``None``: weekends only)."""
    days = pd.to_datetime(pd.Series(issued_at)).dt.normalize()
    off = (days.dt.dayofweek >= 5).to_numpy()
    if holidays is not None:
        off |= days.isin(pd.to_datetime(list(holidays)).normalize()).to_numpy()
    return off


def select_cards(
    cards: pd.DataFrame,
    score_col: str,
    *,
    budget: int,
    weekend_budget: int | None = None,
    holidays=None,
) -> np.ndarray:
    """Top ``budget`` evaluable cards per cutoff; deterministic tie-break.

    ``weekend_budget`` (calendar budget) replaces ``budget`` on cutoffs issued on
    Saturday/Sunday, or on ``holidays`` dates when a verified calendar is passed.
    """
    if budget < 1 or (weekend_budget is not None and weekend_budget < 0):
        raise ValueError("budget must be positive")
    frame = cards[CARD_KEYS].copy()
    if frame.duplicated().any():
        raise ValueError("cards must be unique per object_id × sensor_type × issued_at")
    frame["score"] = cards[score_col].to_numpy(dtype=float)
    frame["position"] = np.arange(len(cards))
    frame["budget"] = budget
    if weekend_budget is not None:
        frame["budget"] = np.where(day_off(cards["issued_at"], holidays), weekend_budget, budget)
    eligible = _evaluable(cards) & np.isfinite(frame["score"].to_numpy())
    ranked = frame[eligible].sort_values(
        ["issued_at", "score", "object_id", "sensor_type"],
        ascending=[True, False, True, True],
        kind="mergesort",
    )
    keep = (
        ranked.groupby("issued_at", sort=False).cumcount().to_numpy() < ranked["budget"].to_numpy()
    )
    mask = np.zeros(len(cards), dtype=bool)
    mask[ranked["position"].to_numpy()[keep]] = True
    return mask


def event_outcomes(
    cards: pd.DataFrame, links: pd.DataFrame, events: pd.DataFrame, selected: np.ndarray
) -> pd.DataFrame:
    """Per qualifying event linked to ``cards``: evaluable, capturable, captured, lead."""
    card = cards[CARD_KEYS].copy()
    card["card_position"] = np.arange(len(cards))
    joined = links.merge(card, on=CARD_KEYS, how="inner")
    position = joined["card_position"].to_numpy()
    reason = cards["exclusion_reason"].to_numpy(dtype=object)[position]
    window_ok = pd.isna(reason) | np.isin(reason, WINDOW_OK)
    evaluable_card = _evaluable(cards)[position]
    capturable = joined["candidate_member"].to_numpy(dtype=bool) & evaluable_card
    captured = capturable & selected[position]
    joined = joined.assign(window_ok=window_ok, capturable=capturable, captured=captured)
    joined["capture_at"] = joined["issued_at"].where(joined["captured"])
    per_event = joined.groupby("event_id").agg(
        evaluable=("window_ok", "any"),
        capturable=("capturable", "any"),
        captured=("captured", "any"),
        first_capture_at=("capture_at", "min"),
    )
    columns = ["event_id", "object_id", "event_start", "size", "sensor_types", "system_types"]
    columns += [flag for flag in EVENT_FLAGS if flag in events]
    result = events.loc[events["qualifies"].astype(bool), columns].merge(
        per_event.reset_index(), on="event_id", how="inner"
    )
    result["lead_hours"] = (
        result["event_start"] - result["first_capture_at"]
    ).dt.total_seconds() / 3600
    return result


def _pr_auc(y: np.ndarray, score: np.ndarray):
    if len(y) == 0 or y.min() == y.max():
        return None
    return float(average_precision_score(y, score))


def _precision(y: np.ndarray, sel: np.ndarray):
    return float(y[sel].mean()) if sel.any() else None


def _recall(flags: pd.Series):
    return float(flags.mean()) if len(flags) else None


def _events_block(ev: pd.DataFrame) -> dict:
    lead = ev.loc[ev["captured"], "lead_hours"].to_numpy(dtype=float)
    q = np.percentile(lead, [25, 50, 75]) if len(lead) else [None] * 3
    return {
        "events": len(ev),
        "capturable": int(ev["capturable"].sum()),
        "captured": int(ev["captured"].sum()),
        "recall": _recall(ev["captured"]),
        "recall_capturable": _recall(ev.loc[ev["capturable"], "captured"]),
        "lead_hours_p25": q[0],
        "lead_hours_median": q[1],
        "lead_hours_p75": q[2],
    }


def _by_list(ev: pd.DataFrame, column: str) -> dict:
    exploded = ev[[column, "captured", "capturable", "lead_hours"]].explode(column)
    return {str(key): _events_block(part) for key, part in exploded.groupby(column, sort=True)}


def _evaluate_selected(
    cards: pd.DataFrame,
    links: pd.DataFrame,
    events: pd.DataFrame,
    score_col: str,
    selected: np.ndarray,
    *,
    meta: dict,
    calibrator: IsotonicRegression | None = None,
) -> dict:
    evaluable = _evaluable(cards)
    y = cards["y_card"].to_numpy(dtype=float)
    ev = event_outcomes(cards, links, events, selected)
    ev = ev[ev["evaluable"]]
    rows = cards[evaluable]
    score = rows[score_col].to_numpy(dtype=float)
    finite = np.isfinite(score)
    y_eval = y[evaluable].astype(int)
    sel = selected[evaluable]
    days = cards["issued_at"].to_numpy()
    per_day = pd.Series(selected.astype(int)).groupby(days).sum()
    per_day = per_day[pd.Series(evaluable).groupby(days).any()]
    reasons = cards.loc[~evaluable, "exclusion_reason"].fillna("missing").value_counts()
    failing = rows["object_failing_past_24h"].fillna(False).to_numpy(dtype=bool)
    weekend = rows["weekend"].to_numpy(dtype=bool)
    event_weekend = pd.to_datetime(ev["event_start"]).dt.dayofweek.to_numpy() >= 5
    load = {
        "cutoffs": len(per_day),
        "cards_per_cutoff_mean": float(per_day.mean()) if len(per_day) else None,
    }
    if meta.get("budget") is not None:
        load["cutoffs_at_budget"] = int((per_day >= meta["budget"]).sum())
    policy = meta.get("policy") or {}
    if policy.get("type") == "rolling":
        open_counts = per_day.rolling(int(policy["window_days"]), min_periods=1).sum()
        load["open_cards_mean"] = float(open_counts.mean()) if len(open_counts) else None
        load["open_cards_max"] = int(open_counts.max()) if len(open_counts) else None
    if policy.get("type") == "topk" and policy.get("weekend_k") is not None:
        off = day_off(pd.Series(per_day.index), policy.get("holidays"))
        working, days_off = per_day[~off], per_day[off]
        load["cards_per_working_cutoff_mean"] = float(working.mean()) if len(working) else None
        load["cards_per_day_off_cutoff_mean"] = float(days_off.mean()) if len(days_off) else None
        load["days_off"] = (
            "weekends and verified holidays"
            if policy.get("holidays") is not None
            else "weekends only (no verified holiday calendar)"
        )
    result = {
        "score": score_col,
        **meta,
        "cards_total": len(cards),
        "cards_evaluable": int(evaluable.sum()),
        "cards_excluded_by_reason": reasons.sort_index().to_dict(),
        "card_positives": int(y_eval.sum()),
        "card_base_rate": float(y_eval.mean()) if len(y_eval) else None,
        "cards_without_score": int((~finite).sum()),
        "cards_selected": int(sel.sum()),
        "card_precision": _precision(y_eval, sel),
        "card_pr_auc": _pr_auc(y_eval[finite], score[finite]),
        "load": load,
        "events": _events_block(ev),
        "events_single_channel": _events_block(ev[ev["size"] == 1]),
        "events_mass": _events_block(ev[ev["size"] >= 2]),
        "events_weekday": _events_block(ev[~event_weekend]),
        "events_weekend": _events_block(ev[event_weekend]),
        "events_by_sensor_type": _by_list(ev, "sensor_types"),
        "events_by_system_type": _by_list(ev, "system_types"),
        "cards_by_sensor_type": {
            str(k): {
                "cards_selected": int(sel[m].sum()),
                "precision": _precision(y_eval[m], sel[m]),
                "positives": int(y_eval[m].sum()),
            }
            for k in sorted(rows["sensor_type"].unique())
            for m in [(rows["sensor_type"] == k).to_numpy()]
        },
        "cards_by_system_type": {
            str(k): {
                "cards_selected": int(sel[m].sum()),
                "precision": _precision(y_eval[m], sel[m]),
            }
            for k in sorted(rows["system_type"].dropna().unique())
            for m in [(rows["system_type"] == k).to_numpy()]
        },
        "already_failing_objects": {
            "share_of_selected_cards": float(failing[sel].mean()) if sel.any() else None,
            "precision_failing": _precision(y_eval[failing], sel[failing]),
            "precision_not_failing": _precision(y_eval[~failing], sel[~failing]),
        },
        "weekday_weekend_cards": {
            "weekday_precision": _precision(y_eval[~weekend], sel[~weekend]),
            "weekend_precision": _precision(y_eval[weekend], sel[weekend]),
        },
    }
    for flag in EVENT_FLAGS:
        if flag in ev:
            marked = ev[flag].fillna(False).to_numpy(dtype=bool)
            result[f"events_{flag}"] = _events_block(ev[marked])
            result[f"events_not_{flag}"] = _events_block(ev[~marked])
    if calibrator is not None:
        probs = calibrator.predict(score[finite])
        result["calibration"] = calibration(y_eval[finite], np.asarray(probs, dtype=float))
    return _py(result)


def evaluate_cards(
    cards: pd.DataFrame,
    links: pd.DataFrame,
    events: pd.DataFrame,
    score_col: str,
    *,
    budget: int,
    calibrator: IsotonicRegression | None = None,
) -> dict:
    """Card precision, event recall and lead at ``budget`` cards per cutoff, plus
    card PR-AUC, optional calibration and breakdowns."""
    selected = select_cards(cards, score_col, budget=budget)
    return _evaluate_selected(
        cards, links, events, score_col, selected, meta={"budget": budget}, calibrator=calibrator
    )


def select_policy(cards: pd.DataFrame, score_col: str, policy: dict) -> np.ndarray:
    """Mask of issued cards; each issued card is the row of its issue cutoff.

    - ``{"type": "topk", "k": k}``: top k evaluable cards every cutoff.
      Calendar budget: ``"weekend_k": k2`` issues k2 cards on Saturday/Sunday
      cutoffs; ``"holidays": [dates]`` adds days off only from a verified
      calendar (without it holidays are treated as working days).
    - ``{"type": "cadence", "every_days": d, "k": k}``: top k every d days from
      ``policy["anchor"]`` (default: the first cutoff of ``cards``).
    - ``{"type": "rolling", "max_open": m, "window_days": w}``: a card stays open
      for w days from its issue; each cutoff adds cards by score for units
      (``object_id × sensor_type``) without an open card until m are open. Open
      cards are never displaced; one card per unit per open window.
    Ties go to ``object_id`` then ``sensor_type``.
    """
    kind = policy["type"]
    if kind == "topk":
        weekend = policy.get("weekend_k")
        return select_cards(
            cards,
            score_col,
            budget=int(policy["k"]),
            weekend_budget=None if weekend is None else int(weekend),
            holidays=policy.get("holidays"),
        )
    if kind == "cadence":
        mask = select_cards(cards, score_col, budget=int(policy["k"]))
        days = pd.to_datetime(cards["issued_at"]).dt.normalize()
        anchor = pd.Timestamp(policy.get("anchor", days.min()))
        issue_day = ((days - anchor).dt.days % int(policy["every_days"])) == 0
        return mask & issue_day.to_numpy()
    if kind != "rolling":
        raise ValueError(f"unknown policy type: {kind}")
    max_open, window = int(policy["max_open"]), pd.Timedelta(days=int(policy["window_days"]))
    if max_open < 1:
        raise ValueError("max_open must be positive")
    frame = cards[CARD_KEYS].copy()
    if frame.duplicated().any():
        raise ValueError("cards must be unique per object_id × sensor_type × issued_at")
    frame["score"] = cards[score_col].to_numpy(dtype=float)
    frame["position"] = np.arange(len(cards))
    eligible = _evaluable(cards) & np.isfinite(frame["score"].to_numpy())
    ranked = frame[eligible].sort_values(
        ["issued_at", "score", "object_id", "sensor_type"],
        ascending=[True, False, True, True],
        kind="mergesort",
    )
    mask = np.zeros(len(cards), dtype=bool)
    open_until: dict[tuple, pd.Timestamp] = {}
    for day, group in ranked.groupby("issued_at", sort=True):
        open_until = {unit: until for unit, until in open_until.items() if until > day}
        slots = max_open - len(open_until)
        if slots <= 0:
            continue
        for obj, sensor, position in zip(
            group["object_id"], group["sensor_type"], group["position"], strict=True
        ):
            if (obj, sensor) in open_until:
                continue
            mask[position] = True
            open_until[(obj, sensor)] = day + window
            slots -= 1
            if slots == 0:
                break
    return mask


def evaluate_policy(
    cards: pd.DataFrame,
    links: pd.DataFrame,
    events: pd.DataFrame,
    score_col: str,
    policy: dict,
    *,
    calibrator: IsotonicRegression | None = None,
) -> dict:
    """:func:`evaluate_cards` for an issue policy; issued cards use their own window."""
    selected = select_policy(cards, score_col, policy)
    return _evaluate_selected(
        cards, links, events, score_col, selected, meta={"policy": policy}, calibrator=calibrator
    )


DISPATCH_SYSTEM = "Диспетчерский контроль"


def to_unit_b(
    cards: pd.DataFrame, links: pd.DataFrame, score_cols: list[str] = ()
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Unit B: ``object_id × system_type`` (``object_id × sensor_type`` inside
    'Диспетчерский контроль'). The unit label is put in ``sensor_type`` so the
    same selection/evaluation code applies; ``unit_group`` repeats it.

    A group is evaluable if a member card is; ``y_card`` = any evaluable member
    positive; scores = max over evaluable members; a link counts as a candidate
    member only through an evaluable member card.
    """
    cards = cards.copy()
    defaults = {
        "first_event_start": pd.NaT,
        "known_channels": 0,
        "candidate_channels": 0,
        "object_failing_past_24h": False,
        "weekend": False,
    }
    for column, value in defaults.items():
        if column not in cards:
            cards[column] = value
    cards["unit_group"] = np.where(
        cards["system_type"] == DISPATCH_SYSTEM, cards["sensor_type"], cards["system_type"]
    )
    evaluable = _evaluable(cards)
    cards["_eval"] = evaluable
    cards["_y"] = np.where(evaluable, cards["y_card"].to_numpy(dtype=float), np.nan)
    cards["_first"] = cards["first_event_start"].where(evaluable & (cards["_y"] == 1))
    cards["_nocand"] = cards["exclusion_reason"].eq("no_candidate_channels")
    cards["_reason"] = (
        cards["exclusion_reason"].astype(object).where(cards["exclusion_reason"].notna(), "~")
    )
    keys = ["object_id", "unit_group", "issued_at"]
    for col in score_cols:
        cards[f"_score_{col}"] = cards[col].where(evaluable)
    agg = {
        "system_type": ("system_type", "first"),
        "split_period": ("split_period", "first"),
        "member_cards": ("sensor_type", "size"),
        "evaluable_members": ("_eval", "sum"),
        "y_card": ("_y", "max"),
        "known_channels": ("known_channels", "sum"),
        "candidate_channels": ("candidate_channels", "sum"),
        "first_event_start": ("_first", "min"),
        "object_failing_past_24h": ("object_failing_past_24h", "any"),
        "weekend": ("weekend", "first"),
        "any_no_candidate": ("_nocand", "any"),
        "min_reason": ("_reason", "min"),
        **{col: (f"_score_{col}", "max") for col in score_cols},
    }
    out = cards.groupby(keys, sort=False).agg(**agg).reset_index()
    has_eval = out["evaluable_members"] > 0
    out["exclusion_reason"] = np.where(
        has_eval,
        None,
        np.where(
            out["any_no_candidate"],
            "no_candidate_channels",
            out["min_reason"].where(out["min_reason"] != "~", None),
        ),
    )
    out["outcome"] = np.where(
        has_eval, np.where(out["y_card"] == 1, "positive", "negative"), "excluded"
    )
    out["y_card"] = out["y_card"].where(has_eval)
    out["sensor_type"] = out["unit_group"]
    out["unit"] = "B"
    out = out.drop(columns=["any_no_candidate", "min_reason"])
    member = cards[CARD_KEYS + ["unit_group", "_eval"]]
    lb = links.merge(member, on=CARD_KEYS, how="inner")
    lb["candidate_member"] = lb["candidate_member"].astype(bool) & lb["_eval"]
    lb = (
        lb.groupby(["object_id", "unit_group", "issued_at", "event_id"], sort=False)
        .agg(event_start=("event_start", "first"), candidate_member=("candidate_member", "any"))
        .reset_index()
    )
    lb["sensor_type"] = lb["unit_group"]
    return out, lb


def ceilings(cards: pd.DataFrame, links: pd.DataFrame, events: pd.DataFrame, policy: dict) -> dict:
    """Perfect ranking under the same policy: positive cards first, more
    capturable events first (greedy oracle, an upper reference)."""
    linked = links[links["candidate_member"].astype(bool)]
    linked = linked[linked["event_id"].isin(events.loc[events["qualifies"], "event_id"])]
    counts = linked.groupby(CARD_KEYS).event_id.nunique().rename("_events")
    frame = cards.merge(counts, on=CARD_KEYS, how="left")
    frame["_oracle"] = frame["y_card"].fillna(0) * (1 + frame["_events"].fillna(0))
    frame.index = cards.index
    result = evaluate_policy(frame, links, events, "_oracle", policy)
    return {
        "policy": policy,
        "cards_selected": result["cards_selected"],
        "card_precision": result["card_precision"],
        "event_recall": result["events"]["recall"],
        "event_recall_capturable": result["events"]["recall_capturable"],
        "events": result["events"]["events"],
    }


def fit_calibrator(
    cards: pd.DataFrame, score_col: str, *, period: str = CALIBRATION_PERIOD
) -> IsotonicRegression:
    """Isotonic map card score → P(y_card=1), fitted only on ``period`` cards."""
    if not (cards["split_period"] == period).all():
        raise ValueError(f"calibration cards must all belong to {period}")
    rows = cards[_evaluable(cards)]
    score = rows[score_col].to_numpy(dtype=float)
    finite = np.isfinite(score)
    model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    model.fit(score[finite], rows["y_card"].to_numpy(dtype=float)[finite])
    return model


def _block_keys(dates: pd.Series, objects: pd.Series, scheme: str) -> pd.Series:
    if scheme == "iso_week":
        iso = pd.to_datetime(dates).dt.isocalendar()
        return iso["year"].astype(str) + "-" + iso["week"].astype(str).str.zfill(2)
    if scheme == "object_id":
        return objects.astype(str)
    raise ValueError(f"unknown block scheme: {scheme}")


def bootstrap_intervals(
    cards: pd.DataFrame,
    links: pd.DataFrame,
    events: pd.DataFrame,
    score_cols: list[str],
    *,
    budget: int | None = None,
    policy: dict | None = None,
    replicates: int = 400,
    seed: int = 17,
    deltas: list | tuple = (),
    schemes: tuple[str, ...] = BLOCKS,
) -> dict:
    """95% block-bootstrap intervals for card precision, event recall and card
    PR-AUC; selections are fixed on the full period and replicates are shared
    across score columns, so deltas ``(a, b, name)`` are paired (a − b).
    Give either ``budget`` (top-k per cutoff) or an issue ``policy``."""
    if (budget is None) == (policy is None):
        raise ValueError("give exactly one of budget or policy")
    policy = policy or {"type": "topk", "k": int(budget)}
    for a, b, _ in deltas:
        if a not in score_cols or b not in score_cols:
            raise ValueError(f"delta columns must be among score_cols: {a}, {b}")
    evaluable = _evaluable(cards)
    rows = cards[evaluable].reset_index(drop=True)
    y = rows["y_card"].to_numpy(dtype=float)
    for col in score_cols:
        if not np.isfinite(rows[col].to_numpy(dtype=float)).all():
            raise ValueError(f"{col}: evaluable cards need finite scores")
    masks = {c: select_policy(cards, c, policy) for c in score_cols}
    sel = {c: masks[c][evaluable].astype(float) for c in score_cols}
    outcomes = {c: event_outcomes(cards, links, events, masks[c]) for c in score_cols}
    base = outcomes[score_cols[0]]
    base = base[base["evaluable"]].reset_index(drop=True)
    captured = {
        c: base[["event_id"]]
        .merge(o[["event_id", "captured"]], on="event_id", how="left")["captured"]
        .fillna(False)
        .to_numpy(dtype=float)
        for c, o in outcomes.items()
    }

    def metrics(wc: np.ndarray, we: np.ndarray) -> dict:
        out = {}
        for c in score_cols:
            with np.errstate(invalid="ignore", divide="ignore"):
                n_sel = wc @ sel[c]
                out[c] = {
                    "card_precision": np.where(n_sel > 0, (wc @ (sel[c] * y)) / n_sel, np.nan),
                    "event_recall": np.where(
                        we.sum(axis=1) > 0, (we @ captured[c]) / we.sum(axis=1), np.nan
                    ),
                    "card_pr_auc": weighted_average_precision(
                        y.astype(int), rows[c].to_numpy(dtype=float), wc
                    ),
                }
        return out

    point = metrics(np.ones((1, len(rows))), np.ones((1, len(base))))
    result = {
        "replicates": replicates,
        "seed": seed,
        "budget": budget,
        "policy": policy,
        "cards_evaluable": len(rows),
        "events_evaluable": len(base),
        "selection": "cards fixed on the full period; blocks resampled with replacement",
        "schemes": {},
    }
    for offset, scheme in enumerate(schemes):
        card_key = _block_keys(rows["issued_at"], rows["object_id"], scheme)
        event_key = _block_keys(base["event_start"], base["object_id"], scheme)
        codes, uniques = pd.factorize(pd.concat([card_key, event_key], ignore_index=True))
        card_codes, event_codes = codes[: len(rows)], codes[len(rows) :]
        blocks = len(uniques)
        rng = np.random.default_rng(seed + offset)
        draws = rng.integers(0, blocks, size=(replicates, blocks))
        collected = {c: {m: [] for m in point[c]} for c in score_cols}
        for lo in range(0, replicates, _BATCH):
            counts = np.stack(
                [np.bincount(d, minlength=blocks) for d in draws[lo : lo + _BATCH]]
            ).astype(float)
            batch = metrics(counts[:, card_codes], counts[:, event_codes])
            for c in score_cols:
                for m in collected[c]:
                    collected[c][m].append(batch[c][m])
        values = {c: {m: np.concatenate(v) for m, v in per.items()} for c, per in collected.items()}
        block = {
            "blocks": blocks,
            "scores": {
                c: {m: _interval(float(point[c][m][0]), values[c][m]) for m in values[c]}
                for c in score_cols
            },
            "deltas": {},
        }
        for a, b, name in deltas:
            block["deltas"][name] = {"a": a, "b": b}
            for m in values[a]:
                diff = values[a][m] - values[b][m]
                entry = _interval(float(point[a][m][0] - point[b][m][0]), diff)
                valid = diff[np.isfinite(diff)]
                entry["share_positive"] = float((valid > 0).mean()) if len(valid) else None
                block["deltas"][name][m] = entry
        result["schemes"][scheme] = block
    return _py(result)


def event_day_flags(events: pd.DataFrame, *, cutoff_hour: int = 0) -> pd.DataFrame:
    """Evaluation slices of events (not features) from the start times of all
    events of the label on the same object, qualifying or not:

    - ``prior_same_day_object_event``: another event of the object started
      earlier on the same calendar day (a "session" continuation);
    - ``starts_after_cutoff_hour``: the event starts at or after ``cutoff_hour``
      of its day;
    - ``prior_object_event_before_cutoff``: it starts after the cutoff hour and
      another event of the object started that day in ``[00:00, cutoff_hour)`` —
      visible to a same-day cutoff at ``cutoff_hour`` but not at midnight.
    """
    out = events.copy()
    start = pd.to_datetime(out["event_start"])
    day = start.dt.normalize()
    order = np.lexsort((out["event_id"].astype(str).to_numpy(), start.to_numpy(), out["object_id"]))
    frame = pd.DataFrame({"object_id": out["object_id"].to_numpy(), "start": start, "day": day})
    frame = frame.iloc[order]
    previous = frame.groupby("object_id", sort=False)["start"].shift(1)
    prior = (previous >= frame["day"]) & (previous < frame["start"])
    first_of_day = frame.groupby(["object_id", "day"], sort=False)["start"].transform("min")
    cut = frame["day"] + pd.Timedelta(hours=int(cutoff_hour))
    after = frame["start"] >= cut
    out["prior_same_day_object_event"] = prior.reindex(out.index).to_numpy(dtype=bool)
    out["starts_after_cutoff_hour"] = after.reindex(out.index).to_numpy(dtype=bool)
    out["prior_object_event_before_cutoff"] = (
        (after & (first_of_day < cut)).reindex(out.index).to_numpy(dtype=bool)
    )
    return out


def _known_after(start: pd.Series, rule) -> pd.Series:
    """Earliest time after which a qualifying member is known to qualify.

    ``rule`` is the target's episode filter: None (all), minimum hours (v1) or
    ``{"op": ">"|">=", "seconds": s}``; visible at t iff the result is < t.
    """
    if rule is None:
        return start
    if isinstance(rule, dict):
        span = pd.Timedelta(seconds=int(rule["seconds"]))
        return start + span if rule["op"] == ">" else start + span - pd.Timedelta(1, "ns")
    return start + pd.Timedelta(hours=float(rule)) - pd.Timedelta(1, "ns")


def static_list_scores(
    cards: pd.DataFrame,
    members: pd.DataFrame,
    episode_filter,
    *,
    window_days: int = 365,
    unit: tuple[str, ...] = ("object_id", "sensor_type"),
) -> pd.Series:
    """Point-in-time "static list of last year": per card, the number of target
    events of its unit (object × sensor type) seen in ``[t − window_days, t)``.

    An event counts for a unit at the later of (a) the first start of a member
    episode on the unit's channels and (b) the moment the event is known to
    qualify (``episode_filter`` of the target; e.g. > 2 s means 2 s after a
    member start). Only starts and elapsed time before t are used, never ends
    or durations after t. Returns a float series aligned with ``cards``.
    """
    unit = list(unit)
    m = members.copy()
    m["start_at"] = pd.to_datetime(m["start_at"])
    qualifying = m["qualifying"].astype(bool)
    known = _known_after(m["start_at"], episode_filter).where(qualifying)
    event_known = known.groupby(m["event_id"]).min().rename("event_known")
    pair_first = m.groupby(["event_id", *unit])["start_at"].min().rename("pair_first").reset_index()
    pair_first = pair_first.merge(event_known, left_on="event_id", right_index=True, how="left")
    pair_first = pair_first.dropna(subset=["event_known"])
    pair_first["count_at"] = pair_first[["pair_first", "event_known"]].max(axis=1)
    times = {
        key: np.sort(part["count_at"].to_numpy(dtype="datetime64[ns]"))
        for key, part in pair_first.groupby(unit, sort=False)
    }
    window = np.timedelta64(int(window_days), "D")
    result = np.zeros(len(cards), dtype=float)
    issued = pd.to_datetime(cards["issued_at"]).to_numpy(dtype="datetime64[ns]")
    for key, positions in cards.groupby(unit, sort=False).indices.items():
        stamps = times.get(key if isinstance(key, tuple) else (key,))
        if stamps is None:
            continue
        t = issued[positions]
        result[positions] = np.searchsorted(stamps, t, side="left") - np.searchsorted(
            stamps, t - window, side="left"
        )
    return pd.Series(result, index=cards.index, name="static_list")


def _week_key(values: pd.Series) -> pd.Series:
    return _block_keys(values, values, "iso_week")


def bootstrap_variant_deltas(
    variants: dict[str, dict],
    deltas: list | tuple,
    *,
    replicates: int = 400,
    seed: int = 17,
) -> dict:
    """Week-block bootstrap of card precision and event recall across variants
    whose card tables differ (e.g. issue at 00:00 vs 06:00, or two policies).

    ``variants[name] = {"cards", "links", "events", "score", "policy"}``. Cards
    fall into the ISO week of their cutoff and events into the week of their
    start; each replicate resamples weeks once and applies the same weights to
    every variant, so ``deltas`` ``(a, b, name)`` are paired (a − b).
    """
    sums = {}
    for name, v in variants.items():
        cards = v["cards"]
        mask = select_policy(cards, v["score"], v["policy"])
        evaluable = _evaluable(cards)
        rows = cards[evaluable]
        y = rows["y_card"].to_numpy(dtype=float)
        sel = mask[evaluable].astype(float)
        ev = event_outcomes(cards, v["links"], v["events"], mask)
        ev = ev[ev["evaluable"]]
        card_week = _week_key(rows["issued_at"]).to_numpy()
        event_week = _week_key(ev["event_start"]).to_numpy()
        sums[name] = {
            "sel": pd.Series(sel).groupby(card_week).sum(),
            "hit": pd.Series(sel * y).groupby(card_week).sum(),
            "events": pd.Series(np.ones(len(ev))).groupby(event_week).sum(),
            "captured": pd.Series(ev["captured"].to_numpy(dtype=float)).groupby(event_week).sum(),
        }
    for a, b, _ in deltas:
        if a not in sums or b not in sums:
            raise ValueError(f"delta variants must be among variants: {a}, {b}")
    weeks = sorted(set().union(*(s[k].index for s in sums.values() for k in s)))
    arrays = {
        name: {k: s[k].reindex(weeks, fill_value=0.0).to_numpy(dtype=float) for k in s}
        for name, s in sums.items()
    }

    def metrics(weights: np.ndarray) -> dict:
        out = {}
        for name, s in arrays.items():
            with np.errstate(invalid="ignore", divide="ignore"):
                n_sel, n_ev = weights @ s["sel"], weights @ s["events"]
                out[name] = {
                    "card_precision": np.where(n_sel > 0, (weights @ s["hit"]) / n_sel, np.nan),
                    "event_recall": np.where(n_ev > 0, (weights @ s["captured"]) / n_ev, np.nan),
                }
        return out

    point = metrics(np.ones((1, len(weeks))))
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(weeks), size=(replicates, len(weeks)))
    counts = np.stack([np.bincount(d, minlength=len(weeks)) for d in draws]).astype(float)
    values = metrics(counts)
    result = {
        "replicates": replicates,
        "seed": seed,
        "blocks": len(weeks),
        "scheme": "iso_week (cards by cutoff, events by start)",
        "variants": {
            name: {
                "policy": variants[name]["policy"],
                **{m: _interval(float(point[name][m][0]), values[name][m]) for m in values[name]},
            }
            for name in variants
        },
        "deltas": {},
    }
    for a, b, name in deltas:
        result["deltas"][name] = {"a": a, "b": b}
        for m in values[a]:
            diff = values[a][m] - values[b][m]
            entry = _interval(float(point[a][m][0] - point[b][m][0]), diff)
            valid = diff[np.isfinite(diff)]
            entry["share_positive"] = float((valid > 0).mean()) if len(valid) else None
            result["deltas"][name][m] = entry
    return _py(result)
