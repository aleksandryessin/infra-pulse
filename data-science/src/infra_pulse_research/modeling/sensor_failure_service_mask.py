"""TO-session mask of fire detectors applied to the built sensor-failure labels, and the
labels of the demo target "fire detector failure" (v9; user decisions 27.09.2026 relayed by
Head DS).

The definitions (sessions, mask interval ±30 min, smoke and heat only, the target a + b1)
live in ``infra_pulse_research.sensor_failure_maintenance`` (DS-A,
``DEFINITION_VERSION = "sensor-failure-maintenance-v1"``); this module only applies them to
the card / link / event tables of the rolling-list evaluation:

- :func:`maintenance_sessions` — the sessions of the journal state runs;
- :func:`masked_members` — event members (connection-loss episodes) of smoke and heat inside a
  session of their object widened by 30 min;
- :func:`apply_mask` — labels without the masked (event, pair) memberships: links dropped,
  card outcome, first window event and history members recomputed;
- :func:`subsystem_labels` — cards, links and events of the unit object × fire subsystem
  (smoke + heat together) for events given as ``object_id, start_at``.

Limitation of :func:`apply_mask`: it works on built labels; the candidate status of channels
(active episode, Q shadow) is not recomputed. A masked event is not a proven non-failure.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from infra_pulse_research import sensor_failure_maintenance as fm

CARD_KEYS = ["object_id", "sensor_type", "issued_at"]
PAIR = ["event_id", "object_id", "sensor_type"]
EVALUABLE = ("positive", "negative")
FIRE_TYPES = fm.TARGET_SENSOR_TYPES
SUBSYSTEM = "Пожарная подсистема (дым + тепловой)"
SUBSYSTEM_SYSTEM = "Пожарная охрана"


def maintenance_sessions(runs: pd.DataFrame) -> pd.DataFrame:
    """TO sessions per object (``object_id, session_start, session_end, …``) of the state
    runs (``sensor_failure_maintenance.state_runs``)."""
    return fm.detect_maintenance_sessions(runs)


def masked_members(members: pd.DataFrame, sessions: pd.DataFrame) -> np.ndarray:
    """Members of smoke and heat whose ``start_at`` lies in a session of their object
    widened by 30 min (both ends included)."""
    return fm.maintenance_flags(members, sessions, time_col="start_at")


def apply_mask(
    cards: pd.DataFrame,
    links: pd.DataFrame,
    events: pd.DataFrame,
    members: pd.DataFrame,
    masked: np.ndarray,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Labels without the masked memberships.

    An (event, pair) is removed when every member of the pair in the event is masked; its
    links are dropped. Cards with a known outcome get ``events_in_window``,
    ``first_event_start``, ``outcome`` and ``y_card`` from the remaining candidate links of
    qualifying events; cards with an unknown outcome only their first event. Returns
    ``(cards, links, members)`` with the masked members removed from the history.
    """
    masked = np.asarray(masked, dtype=bool)
    m = members[PAIR].assign(_masked=masked)
    removed = m.groupby(PAIR)["_masked"].all()
    removed = removed[removed].reset_index()[PAIR]
    lk = links.merge(removed.assign(_drop=True), on=PAIR, how="left")
    lk = lk[lk["_drop"].isna()].drop(columns="_drop").reset_index(drop=True)
    qualifying = set(events.loc[events["qualifies"].astype(bool), "event_id"])
    hits = lk[lk["candidate_member"].astype(bool) & lk["event_id"].isin(qualifying)]
    agg = hits.groupby(CARD_KEYS).agg(_n=("event_id", "nunique"), _first=("event_start", "min"))
    out = cards.merge(agg, left_on=CARD_KEYS, right_index=True, how="left")
    out.index = cards.index
    n = out["_n"].fillna(0).astype("int64")
    known = out["outcome"].isin(EVALUABLE)
    out["first_event_start"] = pd.to_datetime(out["_first"])
    if "events_in_window" in out:
        out["events_in_window"] = n
    out["outcome"] = np.where(known, np.where(n > 0, "positive", "negative"), out["outcome"])
    out["y_card"] = np.where(known, (n > 0).astype(float), np.nan)
    kept = members[~masked].reset_index(drop=True)
    return out.drop(columns=["_n", "_first"]), lk, kept


def subsystem_labels(
    pair_cards: pd.DataFrame, unit_events: pd.DataFrame, days: int
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Cards, links, events and members of the unit object × fire subsystem.

    ``pair_cards`` are the label cards of smoke and heat pairs; a unit card exists at a cutoff
    if a pair card of the object exists. Its stored reason is empty if a pair card has no
    window reason (a missing candidate channel does not matter for this target), otherwise
    the first pair reason. ``unit_events`` (``object_id, start_at``) are the target events of
    the object's smoke and heat; events at the same moment of an object are one event. A
    unit card is positive if an event starts in ``[t, t + D)``. Returns
    ``(cards, links, events, members)`` in the layout of the evaluation (``sensor_type`` is
    the unit label, every member is a candidate; ``event_id`` values are local only).
    """
    pc = pair_cards[pair_cards["sensor_type"].isin(FIRE_TYPES)].copy()
    reason = pc["exclusion_reason"].where(pc["exclusion_reason"].ne("no_candidate_channels"))
    pc["_ok"] = reason.isna()
    pc["_reason"] = reason
    grouped = pc.sort_values(CARD_KEYS, kind="mergesort").groupby(
        ["object_id", "issued_at"], sort=True
    )
    cards = grouped.agg(_ok=("_ok", "any"), _reason=("_reason", "first")).reset_index()
    cards["exclusion_reason"] = cards["_reason"].where(~cards["_ok"])
    cards["candidate_channels"] = 1.0
    cards["sensor_type"] = SUBSYSTEM
    cards["system_type"] = SUBSYSTEM_SYSTEM
    ev = unit_events[["object_id", "start_at"]].drop_duplicates()
    ev = ev.sort_values(["object_id", "start_at"], kind="mergesort").reset_index(drop=True)
    ev["event_id"] = "u" + pd.Series(np.arange(len(ev)), dtype="int64").astype(str)
    ev["event_start"] = pd.to_datetime(ev["start_at"])
    window = pd.Timedelta(days=int(days))
    joined = cards[["object_id", "issued_at"]].merge(ev, on="object_id", how="inner")
    t = pd.to_datetime(joined["issued_at"])
    joined = joined[(joined["event_start"] >= t) & (joined["event_start"] < t + window)]
    links = joined[["object_id", "issued_at", "event_id", "event_start"]].assign(
        sensor_type=SUBSYSTEM, candidate_member=True
    )
    first = links.groupby(["object_id", "issued_at"]).agg(
        events_in_window=("event_id", "nunique"), first_event_start=("event_start", "min")
    )
    cards = cards.merge(first, on=["object_id", "issued_at"], how="left")
    cards["events_in_window"] = cards["events_in_window"].fillna(0).astype("int64")
    ok = cards["exclusion_reason"].isna()
    cards["outcome"] = np.where(
        ok, np.where(cards["events_in_window"] > 0, "positive", "negative"), "excluded"
    )
    cards["y_card"] = np.where(ok, (cards["events_in_window"] > 0).astype(float), np.nan)
    events = pd.DataFrame(
        {
            "event_id": ev["event_id"],
            "object_id": ev["object_id"],
            "event_start": ev["event_start"],
            "size": 1,
            "sensor_types": [[SUBSYSTEM]] * len(ev),
            "system_types": [[SUBSYSTEM_SYSTEM]] * len(ev),
            "qualifies": True,
        }
    )
    members = pd.DataFrame(
        {
            "event_id": ev["event_id"],
            "object_id": ev["object_id"],
            "sensor_type": SUBSYSTEM,
            "system_type": SUBSYSTEM_SYSTEM,
            "start_at": ev["event_start"],
            "qualifying": True,
        }
    )
    columns = [
        "object_id",
        "sensor_type",
        "system_type",
        "issued_at",
        "exclusion_reason",
        "candidate_channels",
        "outcome",
        "y_card",
        "events_in_window",
        "first_event_start",
    ]
    return cards[columns], links.reset_index(drop=True), events, members
