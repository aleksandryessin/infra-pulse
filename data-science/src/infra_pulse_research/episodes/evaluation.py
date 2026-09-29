"""One-to-one new-episode/card matching and paired uncertainty at fixed workload."""

from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd


def ratios(counts):
    p, n, h, d = counts
    return {
        "card_precision": p / n if n else None,
        "incident_recall": h / d if d else None,
        "cards_positive": float(p),
        "cards_known": float(n),
        "incident_heads_caught": float(h),
        "incident_heads": float(d),
    }


def replay(rows, scores, heads, fold, *, horizon, mask, k=10, target="A"):
    if len(scores) != len(rows) or not np.isfinite(scores).all():
        raise ValueError("one finite score per row required")
    by_pair = defaultdict(list)
    for e in heads:
        by_pair[(e["object_id"], e["family"])].append(e)
    for es in by_pair.values():
        es.sort(key=lambda e: e["at"])
    by_day = defaultdict(list)
    start, end = pd.Timestamp(fold["test_start"]), pd.Timestamp(fold["test_end"])
    for i, row in enumerate(rows):
        day = pd.Timestamp(row["day"])
        if start <= day and day + pd.Timedelta(days=horizon + int(target == "B")) <= end:
            by_day[day].append(i)
    cards, active, loads = [], {}, []
    for day, indexes in sorted(by_day.items()):
        active = {
            p: c
            for p, c in active.items()
            if day < c["until"] and (c["release"] is None or day <= c["release"])
        }
        for i in sorted(
            indexes, key=lambda i: (-scores[i], rows[i]["object_id"], rows[i]["family"])
        ):
            row = rows[i]
            pair = (row["object_id"], row["family"])
            if len(active) >= k:
                break
            if not row["eligible"] or pair in active:
                continue
            until = day + pd.Timedelta(days=horizon)
            hits = [e for e in by_pair[pair] if day < pd.Timestamp(e["at"]) < until]
            first = hits[0] if hits else None
            # A registered event at exactly the cutoff has zero lead and is not a prediction.
            release = pd.Timestamp(first["available_at"]) if first else None
            card = dict(
                row,
                issued=day,
                until=until,
                release=release,
                matched=first,
                positive=bool(first),
                pair=pair,
            )
            active[pair] = card
            cards.append(card)
        loads.append(len(active))
    temporal, objects, families = (defaultdict(lambda: np.zeros(4)) for _ in range(3))
    for family in sorted({r["family"] for r in rows}):
        families[family] += np.zeros(4)
    per_object, unknown, leads, matched_ids = defaultdict(int), 0, [], set()

    def add(day, obj, family, values):
        block = str((pd.Timestamp(day) - start).days // 28)
        temporal[block] += values
        objects[obj] += values
        families[family] += values

    for c in cards:
        per_object[c["object_id"]] += 1
        unknown += c["y"] < 0
        if c["y"] >= 0:
            add(c["issued"], c["object_id"], c["family"], [int(c["positive"]), 1, 0, 0])
        if c["matched"] is not None:
            e = c["matched"]
            eid = (e["object_id"], e["family"], e["at"])
            if eid in matched_ids:
                raise AssertionError("an episode was matched to multiple cards")
            matched_ids.add(eid)
            leads.append((pd.Timestamp(e["at"]) - c["issued"]).total_seconds() / 3600)
    unknown_heads = 0
    for e in heads:
        at = pd.Timestamp(e["at"])
        if not start <= at < end:
            continue
        if not e["covered"] or (mask == "structural_unknown" and e["uncertain"]):
            unknown_heads += 1
            continue
        eid = (e["object_id"], e["family"], e["at"])
        add(at, e["object_id"], e["family"], [0, 0, int(eid in matched_ids), 1])
    total = sum(temporal.values(), np.zeros(4))
    by_family = {f: ratios(v) for f, v in sorted(families.items())}
    metrics = ratios(total) | {
        "cards_issued": len(cards),
        "cards_unknown": int(unknown),
        "known_card_fraction": (len(cards) - unknown) / len(cards) if cards else None,
        "unknown_heads": unknown_heads,
        "max_open": max(loads, default=0),
        "mean_open": float(np.mean(loads)) if loads else 0.0,
        "objects_issued": len(per_object),
        "max_object_card_share": max(per_object.values(), default=0) / max(1, len(cards)),
        "lead_hours_median": float(np.median(leads)) if leads else None,
        "lead_hours_p10": float(np.quantile(leads, 0.1)) if leads else None,
        "caught_with_lead_ge_24h": sum(x >= 24 for x in leads),
    }
    for metric in ("card_precision", "incident_recall"):
        values = [m[metric] for m in by_family.values() if m[metric] is not None]
        metrics["macro_" + metric] = float(np.mean(values)) if values else None
        metrics["macro_" + metric + "_families"] = len(values)
    return {
        "metrics": metrics,
        "families": by_family,
        "blocks": {
            "days28": {k: v.tolist() for k, v in temporal.items()},
            "objects": {k: v.tolist() for k, v in objects.items()},
        },
    }


def mean_blocks(items):
    result = {}
    for unit in ("days28", "objects"):
        keys = sorted({k for x in items for k in x["blocks"][unit]})
        result[unit] = {
            k: np.mean([x["blocks"][unit].get(k, [0] * 4) for x in items], axis=0).tolist()
            for k in keys
        }
    return result


def intervals(candidate, baseline, *, replicates=2000, seed=20260929):
    result = {}
    for unit in ("days28", "objects"):
        keys = sorted(set(candidate[unit]) | set(baseline[unit]))
        item = {"units": len(keys)}
        if len(keys) < 2:
            item.update(precision_delta_95=None, recall_delta_95=None)
        else:
            arrays = [
                np.asarray([b[unit].get(k, [0] * 4) for k in keys]) for b in (candidate, baseline)
            ]
            draws = np.random.default_rng(seed).integers(0, len(keys), (replicates, len(keys)))
            sums = [a[draws].sum(1) for a in arrays]
            for name, n, d in (("precision", 0, 1), ("recall", 2, 3)):
                valid = (sums[0][:, d] > 0) & (sums[1][:, d] > 0)
                delta = (
                    sums[0][valid, n] / sums[0][valid, d] - sums[1][valid, n] / sums[1][valid, d]
                )
                item[name + "_delta_95"] = (
                    np.quantile(delta, [0.025, 0.975]).tolist() if len(delta) else None
                )
        result[unit] = item
    return result


def gate(candidate, baselines, acceptance, replicates):
    """Mean of three seed metrics; pass BOTH baselines with BOTH interval units."""
    if len(candidate) != 3:
        return {"passed": False, "reason": "three completed seeds required"}
    means = {
        m: np.mean([c["metrics"][m] for c in candidate])
        if all(c["metrics"][m] is not None for c in candidate)
        else None
        for m in ("card_precision", "incident_recall")
    }
    comparisons = {}
    for name, base in baselines.items():
        ci = intervals(mean_blocks(candidate), base["blocks"], replicates=replicates)
        p, r = means["card_precision"], means["incident_recall"]
        bp, br = base["metrics"]["card_precision"], base["metrics"]["incident_recall"]
        known = None not in (p, r, bp, br)
        point = (
            known
            and r - br >= acceptance["recall_gain"]
            and p - bp >= -acceptance["precision_loss"]
        )
        interval = all(
            v["precision_delta_95"] is not None
            and v["recall_delta_95"] is not None
            and v["precision_delta_95"][0] >= -acceptance["precision_loss"]
            and v["recall_delta_95"][0] > 0
            for v in ci.values()
        )
        comparisons[name] = {
            "passed": bool(point and interval),
            "intervals": ci,
            "precision_delta": float(p - bp) if known else None,
            "recall_delta": float(r - br) if known else None,
        }
    return {
        "passed": all(c["passed"] for c in comparisons.values()),
        "means": means,
        "absolute_point_pass": bool(
            None not in means.values()
            and means["card_precision"] >= acceptance["absolute_precision"]
            and means["incident_recall"] >= acceptance["absolute_recall"]
        ),
        "comparisons": comparisons,
    }
