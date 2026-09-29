"""Hidden shared nodes of ``sensor-failure`` channels (v4, idea I1), point in time.

A *node* is a recurring co-failure group: channels of one object that start
episodes of the label inside the same mass cluster (W = 10 min) again and again.
Rule (as ``artifacts/sensor-failure-v1/ideas/cofailure.py``): a pair of channels
is *strong* if it shares at least ``min_joint`` events and the shared events are at
least ``min_ratio`` of the rarer channel's events; nodes are connected components
of strong pairs. Events with more than ``max_event_size`` members add no pairs.

Point in time: a snapshot ``as_of`` uses only member starts ``< as_of`` (the
members of a cluster straddling ``as_of`` are its prefix, which is a valid chain).
Ends, durations and the target's qualifying flag are never read here. A channel
outside every node is its own node (``node_id = channel_id``); a node id is the
smallest channel id of the group (groups of one object never share channels).

Unit N (:func:`node_cards`): a card is a node × cutoff; it is positive if a
qualifying event starts in ``[t, t + H)`` with a member on a candidate channel of
the node, as unit A does for object × sensor type. Node score propagation
(:func:`propagate_node_scores`) gives every channel the max or noisy-OR of its
node's channels before card aggregation.
"""

from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd

NODE_RULE = {"min_joint": 3, "min_ratio": 0.8, "max_event_size": 60}
EVALUABLE = ("positive", "negative")


def _find(parent: dict, x):
    parent.setdefault(x, x)
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def cofailure_groups(
    members: pd.DataFrame,
    as_of,
    *,
    min_joint: int = NODE_RULE["min_joint"],
    min_ratio: float = NODE_RULE["min_ratio"],
    max_event_size: int = NODE_RULE["max_event_size"],
) -> dict[int, int]:
    """Channel → node id for channels in a co-failure group, from starts ``< as_of``."""
    past = members.loc[
        pd.to_datetime(members["start_at"]) < pd.Timestamp(as_of), ["event_id", "channel_id"]
    ]
    single, pairs = Counter(), Counter()
    for chans in past.groupby("event_id", sort=True)["channel_id"].unique():
        items = sorted(int(c) for c in chans)
        single.update(items)
        if 2 <= len(items) <= max_event_size:
            for i, a in enumerate(items):
                for b in items[i + 1 :]:
                    pairs[(a, b)] += 1
    parent: dict[int, int] = {}
    for (a, b), n in sorted(pairs.items()):
        if n >= min_joint and n / min(single[a], single[b]) >= min_ratio:
            ra, rb = _find(parent, a), _find(parent, b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[int]] = {}
    for x in list(parent):
        groups.setdefault(_find(parent, x), []).append(x)
    return {c: min(chans) for chans in groups.values() for c in chans}


def node_map(channels, groups: dict[int, int]) -> pd.DataFrame:
    """Every channel with its node; channels outside a group are their own node."""
    frame = pd.DataFrame({"channel_id": pd.Series(channels, dtype="int64").unique()})
    mapped = frame["channel_id"].map(groups)
    frame["in_node"] = mapped.notna()
    frame["node_id"] = mapped.fillna(frame["channel_id"]).astype("int64")
    frame["node_size"] = frame.groupby("node_id")["channel_id"].transform("size").astype("int64")
    return frame


def snapshot_summary(nodes: pd.DataFrame) -> dict:
    """Aggregate description of a node map (no identifiers)."""
    grouped = nodes[nodes["in_node"]].groupby("node_id").size()
    return {
        "nodes": len(grouped),
        "channels_in_nodes": int(grouped.sum()),
        "size_median": float(grouped.median()) if len(grouped) else None,
        "size_max": int(grouped.max()) if len(grouped) else None,
    }


def propagate_node_scores(
    channels: pd.DataFrame, score_col: str, nodes: pd.DataFrame, how: str = "max"
) -> np.ndarray:
    """Node score (``max`` or ``noisy_or`` over the node's scored channels at the same
    cutoff) given to every channel of the node; the channel keeps max(own, node)."""
    frame = channels[["channel_id", "issued_at", score_col]].merge(
        nodes[["channel_id", "node_id"]], on="channel_id", how="left"
    )
    frame["node_id"] = frame["node_id"].fillna(frame["channel_id"]).astype("int64")
    score = frame[score_col].astype(float)
    keys = [frame["node_id"], frame["issued_at"]]
    if how == "max":
        node = score.groupby(keys).transform("max")
    elif how == "noisy_or":
        keep = np.log1p(-np.clip(score.to_numpy(), 0.0, 1 - 1e-12))
        node = 1.0 - np.exp(pd.Series(keep, index=frame.index).groupby(keys).transform("sum"))
    else:
        raise ValueError(f"unknown node aggregation: {how}")
    return np.fmax(score.to_numpy(), node.to_numpy())


def select_rolling_release(
    cards: pd.DataFrame, score_col: str, policy: dict, *, causal: bool = True
) -> np.ndarray:
    """Rolling list whose card leaves the list at its event (team decision 26.09.2026).

    As the ``rolling`` policy of ``sensor_failure_evaluation.select_policy``: a card is
    open ``window_days`` from its issue, each cutoff adds cards by score for pairs
    without an open card until ``max_open`` are open, nothing is displaced. A card whose
    event starts inside its window (``first_event_start``) leaves the list at that start
    and frees its place for the next cutoff. The rule and the selection are
    ``sensor_failure_release.release_at`` / ``select_rolling`` (one implementation for
    selection, honest recall and holdout scripts).

    Eligibility is causal by default (``sensor_failure_release.eligibility``: only
    reasons known at the cutoff exclude a card); ``causal=False`` keeps the earlier
    selection by known outcome for comparison.
    """
    from infra_pulse_research.modeling import sensor_failure_release as rel  # noqa: PLC0415

    days = int(policy["window_days"])
    eligible, _ = rel.eligibility(cards, days, causal=causal)
    return rel.select_rolling(
        cards,
        score_col,
        k=int(policy["max_open"]),
        days=days,
        policy="release",
        eligible=eligible,
        first=cards["first_event_start"],
    )


def select_v4(cards: pd.DataFrame, score_col: str, policy: dict) -> np.ndarray:
    from infra_pulse_research.modeling import sensor_failure_evaluation as ev  # noqa: PLC0415

    if policy["type"] == "rolling_release":
        return select_rolling_release(cards, score_col, policy)
    return ev.select_policy(cards, score_col, policy)


def policy_metrics(
    cards: pd.DataFrame, links: pd.DataFrame, events: pd.DataFrame, score_col: str, policy: dict
) -> dict:
    """Card precision, event recall and lead of an issue policy without per-type breakdowns.

    Same definitions as ``sensor_failure_evaluation.evaluate_policy`` (selection by
    ``select_policy`` or :func:`select_rolling_release`, events by ``event_outcomes``);
    breakdowns by sensor type are skipped because unit N has one "type" per node.
    """
    from infra_pulse_research.modeling import sensor_failure_evaluation as ev  # noqa: PLC0415

    selected = select_v4(cards, score_col, policy)
    evaluable = cards["outcome"].isin(EVALUABLE).to_numpy()
    y = cards["y_card"].to_numpy(dtype=float)[evaluable]
    sel = selected[evaluable]
    out = ev.event_outcomes(cards, links, events, selected)
    out = out[out["evaluable"]]
    lead = out.loc[out["captured"], "lead_hours"].to_numpy(dtype=float)
    days = pd.Series(selected.astype(int)).groupby(cards["issued_at"].to_numpy()).sum()
    return {
        "cards_selected": int(sel.sum()),
        "card_precision": float(y[sel].mean()) if sel.any() else None,
        "event_recall": float(out["captured"].mean()) if len(out) else None,
        "events": len(out),
        "captured": int(out["captured"].sum()),
        "lead_hours_median": float(np.median(lead)) if len(lead) else None,
        "new_cards_per_day": float(days.mean()) if len(days) else None,
        "cards_evaluable": int(evaluable.sum()),
        "card_base_rate": float(y.mean()) if len(y) else None,
    }


def ceiling_metrics(
    cards: pd.DataFrame, links: pd.DataFrame, events: pd.DataFrame, policy: dict
) -> dict:
    """Greedy oracle under the policy, as ``sensor_failure_evaluation.ceilings``."""
    linked = links[links["candidate_member"].astype(bool)]
    linked = linked[linked["event_id"].isin(events.loc[events["qualifies"], "event_id"])]
    keys = ["object_id", "sensor_type", "issued_at"]
    counts = linked.groupby(keys).event_id.nunique().rename("_events")
    frame = cards.merge(counts, on=keys, how="left")
    frame["_oracle"] = frame["y_card"].fillna(0) * (1 + frame["_events"].fillna(0))
    frame.index = cards.index
    return policy_metrics(frame, links, events, "_oracle", policy)


def node_cards(
    channel_labels: pd.DataFrame,
    nodes: pd.DataFrame,
    members: pd.DataFrame,
    events: pd.DataFrame,
    *,
    horizon_hours: int = 24,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Unit N cards and event links from channel labels of one or more months.

    ``channel_labels`` needs ``channel_id, object_id, issued_at, split_period, known,
    candidate, window_reason_{H}h``; ``nodes`` maps channels to node ids (the
    month's snapshot). Cards reuse the evaluation keys: ``object_id`` is the node's
    object and ``sensor_type`` is ``"node:<node_id>"``.
    """
    h = int(horizon_hours)
    lab = channel_labels.merge(nodes[["channel_id", "node_id"]], on="channel_id", how="left")
    lab["node_id"] = lab["node_id"].fillna(lab["channel_id"]).astype("int64")
    lab["window_reason"] = lab[f"window_reason_{h}h"]
    keys = ["node_id", "issued_at"]
    cards = (
        lab.groupby(keys, sort=False)
        .agg(
            object_id=("object_id", "first"),
            split_period=("split_period", "first"),
            known_channels=("known", "sum"),
            candidate_channels=("candidate", "sum"),
            window_reason=("window_reason", "min"),
        )
        .reset_index()
    )
    cards = cards[cards["known_channels"] > 0].reset_index(drop=True)
    qualifying = events.loc[events["qualifies"].astype(bool), ["event_id", "event_start"]]
    mem = members[["event_id", "channel_id"]].merge(qualifying, on="event_id")
    cand = lab[["channel_id", "issued_at", "node_id", "object_id", "candidate"]]
    joined = mem.merge(cand, on="channel_id")
    start = pd.to_datetime(joined["event_start"])
    t = pd.to_datetime(joined["issued_at"])
    joined = joined[(start >= t) & (start < t + pd.Timedelta(hours=h))]
    links = (
        joined.groupby(["node_id", "issued_at", "event_id"], sort=False)
        .agg(
            object_id=("object_id", "first"),
            event_start=("event_start", "first"),
            candidate_member=("candidate", "any"),
        )
        .reset_index()
    )
    hits = (
        links[links["candidate_member"]]
        .groupby(keys)
        .agg(events_in_window=("event_id", "nunique"), first_event_start=("event_start", "min"))
        .reset_index()
    )
    cards = cards.merge(hits, on=keys, how="left")
    cards["events_in_window"] = cards["events_in_window"].fillna(0).astype("int64")
    reason = cards["window_reason"].where(
        cards["window_reason"].notna(),
        np.where(cards["candidate_channels"] == 0, "no_candidate_channels", None),
    )
    cards["exclusion_reason"] = reason
    cards["outcome"] = np.where(
        reason.notna(), "excluded", np.where(cards["events_in_window"] > 0, "positive", "negative")
    )
    cards["y_card"] = np.where(
        cards["outcome"].isin(EVALUABLE), (cards["outcome"] == "positive").astype(float), np.nan
    )
    cards["sensor_type"] = "node:" + cards["node_id"].astype(str)
    cards["system_type"] = "node"
    cards["object_failing_past_24h"] = False
    cards["weekend"] = pd.to_datetime(cards["issued_at"]).dt.dayofweek >= 5
    links["sensor_type"] = "node:" + links["node_id"].astype(str)
    return cards, links
