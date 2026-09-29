"""Rolling-card replay; rank-only model outputs never masquerade as probabilities."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np

from infra_pulse_core.features import incident_list as il


def replay(
    rows: list[dict], scores: np.ndarray, events: list[dict], *, start: str, end: str, k: int = 10
) -> tuple[dict, list[dict]]:
    """Issue until end-14d, retain unknown outcomes and release at registered event.

    All methods share causal eligibility/minimum historical count. Model scores are
    converted to integer ranks >=1 only to reuse the canonical select_new policy.
    Incident recall denominator: covered incident heads in [start, end), including
    unforecastable ones; captured only by a still-open card with a candidate member.
    """
    if len(rows) != len(scores) or not np.isfinite(scores).all():
        raise ValueError("one finite score per replay row required")
    by_day = defaultdict(list)
    for i, row in enumerate(rows):
        if start <= row["day"] and date.fromisoformat(row["day"]) + timedelta(days=14) <= (
            date.fromisoformat(end)
        ):
            by_day[row["day"]].append(i)
    open_cards, issued, daily_load = [], [], []
    for day, indexes in sorted(by_day.items()):
        t = il.cutoff_of(date.fromisoformat(day))
        order = sorted(
            indexes, key=lambda i: (-scores[i], il.object_sort_key(rows[i]["object_id"]))
        )
        pairs = [
            il.PairAtCutoff(
                rows[i]["object_id"],
                len(order) - rank,
                rows[i]["eligible"],
                frozenset(rows[i]["candidate_channels"]),
            )
            for rank, i in enumerate(order)
        ]
        lookup = {rows[i]["object_id"]: i for i in indexes}
        for pair, _, _ in il.select_new(t, pairs, open_cards, k=k):
            row = rows[lookup[pair.object_id]]
            release = datetime.fromisoformat(row["first_event"]) if row["first_event"] else None
            open_cards.append(il.IssuedCard(pair.object_id, t, release))
            issued.append(dict(row, release_at=release, issued_at=t))
        held = il.held_objects(t, open_cards)
        daily_load.append(len(held))
        open_cards = [c for c in open_cards if c.object_id in held and il.is_open(t, c)]
    known = [r for r in issued if r["y"] >= 0]
    blocks = defaultdict(lambda: [0, 0, 0, 0])  # positive cards, known cards, caught heads, heads
    for r in known:
        week = date.fromisoformat(r["day"]) - timedelta(days=date.fromisoformat(r["day"]).weekday())
        blocks[str(week)][0] += r["y"]
        blocks[str(week)][1] += 1
    per_object = defaultdict(list)
    for e in events:
        per_object[e["object_id"]].append(e)
    hits = heads = fresh_hits = fresh_heads = 0
    leads = []
    object_issued = defaultdict(list)
    for card in issued:
        object_issued[card["object_id"]].append(card)
    for obj, items in per_object.items():
        items.sort(key=lambda e: e["at"])
        times = [datetime.fromisoformat(e["at"]) for e in items]
        head_flags = il.incident_heads(times)
        for j, (event, at) in enumerate(zip(items, times, strict=True)):
            if (
                not (
                    il.cutoff_of(date.fromisoformat(start))
                    <= at
                    < il.cutoff_of(date.fromisoformat(end))
                )
                or not event["covered"]
            ):
                continue
            if not head_flags[j]:
                continue
            caught = []
            for r in object_issued[obj]:
                card = il.IssuedCard(obj, r["issued_at"], r["release_at"])
                if il.is_open(at, card) and set(r["candidate_channels"]) & set(event["channels"]):
                    caught.append((at - r["issued_at"]).total_seconds() / 3600)
            hit = bool(caught)
            heads += 1
            hits += hit
            fresh = il.fresh_incident(times, j)
            fresh_heads += fresh
            fresh_hits += hit and fresh
            if caught:
                leads.append(max(caught))
            day = il.msk_day(at)
            week = day - timedelta(days=day.weekday())
            blocks[str(week)][2] += hit
            blocks[str(week)][3] += 1
    metrics = {
        "cards_issued": len(issued),
        "cards_known": len(known),
        "cards_unknown": len(issued) - len(known),
        "cards_positive": sum(r["y"] for r in known),
        "card_precision": sum(r["y"] for r in known) / len(known) if known else None,
        "incident_heads": heads,
        "incident_heads_caught": hits,
        "incident_recall": hits / heads if heads else None,
        "fresh_heads": fresh_heads,
        "fresh_heads_caught": fresh_hits,
        "fresh_incident_recall": fresh_hits / fresh_heads if fresh_heads else None,
        "max_open": max(daily_load, default=0),
        "mean_open": float(np.mean(daily_load)) if daily_load else 0.0,
        "lead_hours_median": float(np.median(leads)) if leads else None,
        "objects_issued": len(object_issued),
        "max_object_card_share": max(map(len, object_issued.values()), default=0)
        / max(1, len(issued)),
    }
    for group in ("fresh", "chronic"):
        subset = [r for r in known if r.get("recurrence") == group]
        metrics[group + "_cards_known"] = len(subset)
        metrics[group + "_card_precision"] = (
            sum(r["y"] for r in subset) / len(subset) if subset else None
        )
    return metrics, [{"week": w, "counts": counts} for w, counts in sorted(blocks.items())]


def paired_intervals(
    candidate: list[dict], baseline: list[dict], *, seed: int, replicates: int = 1000
) -> dict:
    """Descriptive paired weekly bootstrap; same sampled weeks for both methods."""
    maps = [{r["week"]: r["counts"] for r in rows} for rows in (candidate, baseline)]
    weeks = sorted(set(maps[0]) | set(maps[1]))
    if len(weeks) < 2:
        return {"weeks": len(weeks), "precision_delta_95": None, "recall_delta_95": None}
    arrays = [np.asarray([m.get(w, [0] * 4) for w in weeks]) for m in maps]
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(weeks), (replicates, len(weeks)))
    sums = [a[draws].sum(1) for a in arrays]
    result = {"weeks": len(weeks)}
    for name, num, den in (("precision", 0, 1), ("recall", 2, 3)):
        valid = (sums[0][:, den] > 0) & (sums[1][:, den] > 0)
        delta = [s[valid, num] / s[valid, den] for s in sums]
        result[name + "_delta_95"] = (
            np.quantile(delta[0] - delta[1], [0.025, 0.975]).tolist() if valid.any() else None
        )
    return result
