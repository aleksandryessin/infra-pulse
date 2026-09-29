"""Release rule, causal eligibility, rolling selection and recall definitions of the
sensor-failure list (v9; user decisions 26–27.09.2026 and the independent audit).

One rule for every caller (selection, honest recall, holdout scripts):

- **Release.** An issued card of a pair (object × sensor type) leaves the list at the
  start of the first qualifying event of the pair in its window (``release_at``); the
  releasing event itself is caught by the card. Without an event the card stays until
  the end of its window ``t + D``. Under ``keep`` (reference only) a card always stays
  until ``t + D``.
- **Open.** A card is open at a moment ``x`` iff ``t <= x < t + D`` and
  ``x <= release_at`` (:func:`is_open`). The same predicate decides whether a card still
  holds its place at a cutoff and whether an event is caught.
- **Causal eligibility.** At a cutoff the selector knows only the past: a card may be
  issued if its stored exclusion reason is empty or depends only on the coverage of the
  future window (``FUTURE_REASONS``), it has candidate channels and its lookback is
  covered (checked independently of the first stored reason, audit 8b). Cutoffs whose
  window crosses the data or split end are not used.

Recall definitions (:func:`recall_summary`); the denominator is the evaluable events of
the protocol (``event_outcomes``):

- ``R_repeats`` — an event is caught if a card of its pair is open at its start;
- ``R_strict`` — an event is caught only if it is the first event of an open card;
- ``R_incident`` — events of a pair whose starts are less than 24 h apart (chained) form
  one incident; an incident is caught if a card of the pair is open at the start of its
  first event; the denominator is incidents whose first event is evaluable.

Precision does not depend on the definition: an issued card is correct if its window
holds an event (``y_card``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from infra_pulse_research.modeling import sensor_failure_evaluation as ev

CARD_KEYS = ["object_id", "sensor_type", "issued_at"]
UNIT = ["object_id", "sensor_type"]
EVALUABLE = ("positive", "negative")
FUTURE_REASONS = ("source_coverage", "policy_day")
BEYOND_REASONS = ("data_end", "split_boundary")
POLICIES = ("release", "keep")
INCIDENT_GAP = pd.Timedelta(hours=24)
FRESH_DAYS = 14
RECALLS = ("R_repeats", "R_strict", "R_incident")


# -- the release rule ------------------------------------------------------------------


def first_event_starts(cards: pd.DataFrame, links: pd.DataFrame, events: pd.DataFrame) -> pd.Series:
    """Start of the first qualifying event of the pair in each card window (a member on a
    candidate channel), as observed in the journal whatever the card's coverage status.
    NaT when the window has none. Aligned with ``cards``."""
    qualifying = set(events.loc[events["qualifies"].astype(bool), "event_id"])
    lk = links[links["candidate_member"].astype(bool) & links["event_id"].isin(qualifying)]
    if "event_start" not in lk:
        lk = lk.merge(events[["event_id", "event_start"]], on="event_id", how="left")
    first = lk.groupby(CARD_KEYS)["event_start"].min().rename("_first")
    out = cards[CARD_KEYS].merge(first, left_on=CARD_KEYS, right_index=True, how="left")["_first"]
    return pd.Series(pd.to_datetime(out).to_numpy(), index=cards.index, name="first_event_start")


def release_at(issued_at, first_event_start, days: int, policy: str = "release") -> pd.Series:
    """The moment an issued card leaves the list (see the module docstring).

    ``release``: the first event start if it lies in ``[t, t + D)``, else ``t + D``;
    ``keep``: ``t + D``.
    """
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}")
    issued = pd.to_datetime(pd.Series(issued_at)).reset_index(drop=True)
    end = issued + pd.Timedelta(days=int(days))
    if policy == "keep":
        return end
    first = pd.to_datetime(pd.Series(first_event_start)).reset_index(drop=True)
    inside = first.notna() & (first >= issued) & (first < end)
    return first.where(inside, end)


def is_open(x, issued_at, release, days: int) -> np.ndarray:
    """Whether a card issued at ``issued_at`` with ``release`` is open at moment ``x``."""
    x = pd.to_datetime(pd.Series(x)).to_numpy()
    issued = pd.to_datetime(pd.Series(issued_at)).to_numpy()
    rel = pd.to_datetime(pd.Series(release)).to_numpy()
    end = issued + np.timedelta64(int(days), "D")
    return (x >= issued) & (x < end) & (x <= rel)


# -- eligibility and selection ---------------------------------------------------------


def _lookback_blocked(cards: pd.DataFrame, blocked_days, lookback_days: int) -> np.ndarray:
    """Cards whose lookback ``[t − lookback_days, t)`` touches a blocked day.

    ``blocked_days``: a frame ``day`` (+ optional ``sensor_type_scope``; missing/None
    means all sensor types) — uncovered journal days and policy days before the cutoff.
    """
    out = np.zeros(len(cards), dtype=bool)
    if blocked_days is None or not len(blocked_days):
        return out
    blocked = pd.DataFrame(blocked_days).copy()
    if "sensor_type_scope" not in blocked:
        blocked["sensor_type_scope"] = None
    days = pd.to_datetime(blocked["day"]).to_numpy().astype("datetime64[D]")
    scope = blocked["sensor_type_scope"]
    glob = days[scope.isna().to_numpy()]
    scoped = {
        (str(d), s) for d, s in zip(days[scope.notna().to_numpy()], scope.dropna(), strict=True)
    }
    day = pd.to_datetime(cards["issued_at"]).to_numpy().astype("datetime64[D]")
    sensor = cards["sensor_type"].to_numpy(dtype=object)
    for lag in range(1, int(lookback_days) + 1):
        back = day - np.timedelta64(lag, "D")
        out |= np.isin(back, glob)
        if scoped:
            out |= np.array([(str(b), s) in scoped for b, s in zip(back, sensor, strict=True)])
    return out


def eligibility(
    cards: pd.DataFrame,
    days: int,
    *,
    period_end=None,
    causal: bool = True,
    blocked_days=None,
    lookback_days: int = 1,
) -> tuple[np.ndarray, np.ndarray]:
    """(eligible, excluded cutoff) masks aligned with ``cards``.

    Causal (default): the stored reason is empty or future-dependent, the card has
    candidate channels (when the column exists) and its lookback is not blocked.
    ``causal=False``: the earlier selection by known outcome, for comparison only.
    Cards whose window crosses the data/split end or ``period_end`` are excluded
    cutoffs in both modes.
    """
    issued = pd.to_datetime(cards["issued_at"])
    reason = cards["exclusion_reason"]
    beyond = reason.isin(BEYOND_REASONS).to_numpy()
    if period_end is not None:
        beyond |= (issued + pd.Timedelta(days=int(days)) > pd.Timestamp(period_end)).to_numpy()
    if causal:
        ok = (reason.isna() | reason.isin(FUTURE_REASONS)).to_numpy()
        if "candidate_channels" in cards:
            ok &= cards["candidate_channels"].fillna(0).to_numpy(dtype=float) > 0
        ok &= ~_lookback_blocked(cards, blocked_days, lookback_days)
    else:
        ok = cards["outcome"].isin(EVALUABLE).to_numpy()
    return ok & ~beyond, beyond


def select_rolling(
    cards: pd.DataFrame,
    score_col: str,
    *,
    k: int,
    days: int,
    policy: str = "release",
    eligible: np.ndarray | None = None,
    first: pd.Series | None = None,
) -> np.ndarray:
    """Rolling list: every daily cutoff adds eligible cards with a finite score, highest
    first (ties by ``object_id`` then ``sensor_type``), for pairs without an open card
    until ``k`` are open; open cards are never displaced. A card holds its place while it
    is open (:func:`is_open`). ``eligible`` defaults to :func:`eligibility` (causal);
    ``first`` defaults to ``cards['first_event_start']``."""
    if int(k) < 1:
        raise ValueError("k must be positive")
    frame = cards[CARD_KEYS].copy()
    if frame.duplicated().any():
        raise ValueError("cards must be unique per object_id × sensor_type × issued_at")
    if eligible is None:
        eligible = eligibility(cards, days)[0]
    if first is None:
        first = cards["first_event_start"]
    frame["score"] = cards[score_col].to_numpy(dtype=float)
    frame["position"] = np.arange(len(cards))
    frame["release"] = release_at(cards["issued_at"], first, days, policy).to_numpy()
    ok = np.asarray(eligible, dtype=bool) & np.isfinite(frame["score"].to_numpy())
    ranked = frame[ok].sort_values(
        ["issued_at", "score", "object_id", "sensor_type"],
        ascending=[True, False, True, True],
        kind="mergesort",
    )
    window = pd.Timedelta(days=int(days))
    mask = np.zeros(len(cards), dtype=bool)
    held: dict[tuple, tuple] = {}
    for day, group in ranked.groupby("issued_at", sort=True):
        held = {u: (t, r) for u, (t, r) in held.items() if t <= day < t + window and day <= r}
        slots = int(k) - len(held)
        if slots <= 0:
            continue
        for obj, sensor, position, release in zip(
            group["object_id"],
            group["sensor_type"],
            group["position"],
            group["release"],
            strict=True,
        ):
            if (obj, sensor) in held:
                continue
            mask[position] = True
            held[(obj, sensor)] = (day, release)
            slots -= 1
            if slots == 0:
                break
    return mask


# -- honest capture, incidents and recalls ----------------------------------------------


def honest_events(
    cards: pd.DataFrame,
    links: pd.DataFrame,
    events: pd.DataFrame,
    mask: np.ndarray,
    days: int,
    policy: str = "release",
    first: pd.Series | None = None,
) -> pd.DataFrame:
    """Evaluable events (protocol ``event_outcomes``) with the honest capture.

    Columns added: ``captured_open`` (a selected card of the pair, whatever its coverage
    status, is open at the start), ``captured_first`` (the event is the first event of
    such a card), ``lead_open`` (hours from the issue of the earliest open capturing card)
    and ``repeat`` (captured, but not the first event of any capturing card).
    """
    if first is None:
        first = first_event_starts(cards, links, events)
    out = ev.event_outcomes(cards, links, events, mask)
    out = out[out["evaluable"]].copy()
    sel = cards.loc[mask, CARD_KEYS].copy()
    sel["_first"] = pd.to_datetime(pd.Series(first).to_numpy()[mask])
    sel["_release"] = release_at(sel["issued_at"], sel["_first"], days, policy).to_numpy()
    lk = links.loc[links["candidate_member"].astype(bool), [*CARD_KEYS, "event_id"]]
    lk = lk.merge(sel, on=CARD_KEYS, how="inner")
    lk = lk.merge(
        events[["event_id", "event_start"]].rename(columns={"event_start": "_start"}),
        on="event_id",
    )
    lk = lk[is_open(lk["_start"], lk["issued_at"], lk["_release"], days)]
    lk = lk.assign(_is_first=pd.to_datetime(lk["_start"]).to_numpy() == lk["_first"].to_numpy())
    per = lk.groupby("event_id").agg(
        _issued=("issued_at", "min"),
        _start=("_start", "first"),
        captured_first=("_is_first", "any"),
    )
    per["lead_open"] = (
        pd.to_datetime(per["_start"]) - pd.to_datetime(per["_issued"])
    ).dt.total_seconds() / 3600
    out = out.merge(
        per[["lead_open", "captured_first"]], left_on="event_id", right_index=True, how="left"
    )
    out["captured_open"] = out["lead_open"].notna()
    out["captured_first"] = out["captured_first"].fillna(False).astype(bool)
    out["repeat"] = out["captured_open"] & ~out["captured_first"]
    return out


def event_pairs(members: pd.DataFrame, events: pd.DataFrame, sensor_types=None) -> pd.DataFrame:
    """Distinct (event, pair) of qualifying events from their members, optionally only
    pairs of the given sensor types (the scope)."""
    qualifying = events.loc[events["qualifies"].astype(bool), ["event_id", "event_start"]]
    pairs = members[["event_id", *UNIT]].drop_duplicates()
    if sensor_types is not None:
        pairs = pairs[pairs["sensor_type"].isin(set(sensor_types))]
    return pairs.merge(qualifying, on="event_id", how="inner").reset_index(drop=True)


def incidents(
    pairs: pd.DataFrame, gap: pd.Timedelta = INCIDENT_GAP, fresh_days: int = FRESH_DAYS
) -> pd.DataFrame:
    """Incident of every event: events of one pair whose starts are less than ``gap``
    apart are chained into one incident, over all events of the pair (caught or not);
    an event of several pairs joins their chains. Returns ``event_id, event_start,
    incident, incident_start, incident_first, incident_fresh`` (one row per event);
    an incident is *fresh* if no pair of its first event had an event in the
    ``fresh_days`` before its start, otherwise *chronic*."""
    columns = [
        "event_id",
        "event_start",
        "incident",
        "incident_start",
        "incident_first",
        "incident_fresh",
    ]
    if not len(pairs):
        return pd.DataFrame(columns=columns)
    p = pairs[["event_id", *UNIT, "event_start"]].copy()
    p["event_start"] = pd.to_datetime(p["event_start"])
    p = p.sort_values([*UNIT, "event_start", "event_id"], kind="mergesort").reset_index(drop=True)
    same_pair = (p[UNIT].shift() == p[UNIT]).all(axis=1).to_numpy()
    close = (p["event_start"].diff() < gap).to_numpy()
    p["chain"] = np.cumsum(~(same_pair & close))
    codes, uniques = pd.factorize(p["event_id"])
    n_events, n_chains = len(uniques), int(p["chain"].max()) + 1
    graph = coo_matrix(
        (np.ones(len(p)), (codes, n_events + p["chain"].to_numpy())),
        shape=(n_events + n_chains, n_events + n_chains),
    )
    _, label = connected_components(graph, directed=False)
    out = p.drop_duplicates("event_id")[["event_id", "event_start"]].copy()
    out["incident"] = label[pd.Index(uniques).get_indexer(out["event_id"])]
    out["incident_start"] = out.groupby("incident")["event_start"].transform("min")
    out["incident_first"] = out["event_start"] == out["incident_start"]
    prev = p.groupby(UNIT, sort=False)["event_start"].shift()
    recent = (p["event_start"] - prev) < pd.Timedelta(days=int(fresh_days))
    chronic = recent.groupby(p["event_id"]).any()
    out["incident_fresh"] = ~out["event_id"].map(chronic).fillna(False).astype(bool)
    head_fresh = out.loc[out["incident_first"]].set_index("incident")["incident_fresh"]
    out["incident_fresh"] = out["incident"].map(head_fresh).astype(bool)
    return out[columns].reset_index(drop=True)


def recall_summary(h: pd.DataFrame, incident_table: pd.DataFrame) -> dict:
    """The three recalls of :func:`honest_events` output, event and incident counts and
    the share of continuation events (evaluable events that do not start an incident)."""
    n = len(h)
    inc = h[["event_id", "captured_open"]].merge(
        incident_table[["event_id", "incident", "incident_first"]], on="event_id", how="left"
    )
    if inc["incident"].isna().any():
        raise ValueError("every evaluable event needs an incident")
    heads = inc[inc["incident_first"].astype(bool)]
    out = {
        "events": n,
        "incidents": len(heads),
        "continuation_share": float(1 - len(heads) / n) if n else None,
        "R_repeats": float(h["captured_open"].mean()) if n else None,
        "R_strict": float(h["captured_first"].mean()) if n else None,
        "R_incident": float(heads["captured_open"].mean()) if len(heads) else None,
        "captured_repeats": int(h["captured_open"].sum()),
        "captured_strict": int(h["captured_first"].sum()),
        "captured_incidents": int(heads["captured_open"].sum()),
    }
    if "incident_fresh" in incident_table:
        fresh = (
            heads["event_id"]
            .map(incident_table.set_index("event_id")["incident_fresh"])
            .astype(bool)
        )
        for name, part in (
            ("fresh", heads[fresh.to_numpy()]),
            ("chronic", heads[~fresh.to_numpy()]),
        ):
            out[f"incidents_{name}"] = len(part)
            out[f"R_incident_{name}"] = float(part["captured_open"].mean()) if len(part) else None
    return out


def card_summary(cards: pd.DataFrame, mask: np.ndarray) -> dict:
    """Precision over issued cards with a known outcome and its lower bound (unknown
    outcomes counted as misses)."""
    known = cards["outcome"].isin(EVALUABLE).to_numpy()
    y = np.nan_to_num(cards["y_card"].to_numpy(dtype=float))
    sel = mask & known
    issued = int(mask.sum())
    return {
        "cards_issued": issued,
        "pairs_issued": int(cards.loc[mask, UNIT].drop_duplicates().shape[0]),
        "cards_known": int(sel.sum()),
        "cards_unknown_outcome": int((mask & ~known).sum()),
        "card_precision": float(y[sel].mean()) if sel.any() else None,
        "card_precision_lower_bound": float(y[sel].sum() / issued) if issued else None,
    }


def f1(p, r):
    """Harmonic mean of precision and recall; None if either is missing."""
    if p is None or r is None or p + r <= 0:
        return None
    return 2 * p * r / (p + r)
