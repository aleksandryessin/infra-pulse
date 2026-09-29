"""Read-only legacy reconciliation and provenance audit; outputs are new aggregates."""

from __future__ import annotations

import json
import runpy
from collections import Counter

import duckdb
import numpy as np
import pandas as pd

from infra_pulse_research.episodes.data import code_identity, verify
from infra_pulse_research.episodes.protocol import mapping_table
from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling import sensor_failure_release as rel
from infra_pulse_research.sequence.evaluation import replay
from infra_pulse_research.sequence.io import ROOT
from infra_pulse_research.sequence.training import digest, write_json


def baseline_audit():
    """Recompute the old selection, then join issued card identities and label denominators."""
    legacy = ROOT / "data-science/artifacts/sequence-v1"
    run = json.loads((legacy / "run/run.json").read_text())
    raw = json.loads((legacy / "prepared/private.json").read_text())
    api = runpy.run_path(str(ROOT / "data-science/scripts/final_sensor_failure_v9.py"))
    report = {
        "sequence_run_sha256": digest(legacy / "run/run.json"),
        "sequence_signature": run["signature"],
        "status": run["status"],
        "folds": [],
    }
    # Copy only the original aggregates into the new audit; no overwrite of any v9 output.
    report["original_models"] = run["models"]
    con = duckdb.connect()
    con.execute("SET threads=2; SET memory_limit='2GB'")
    try:
        for fold in run["config"]["folds"]:
            labelroot = (
                api["HOLDOUT_LABELS"] if fold["name"].startswith("viewed") else api["E5_LABELS"]
            )
            events, members = api["load_events"](con, fold["test_end"], labelroot)
            cards, links = api["load_cards"](
                con, labelroot, 14, fold["test_start"], fold["test_end"]
            )
            cards = cards[cards.sensor_type == "Состояние фазы"].reset_index(drop=True)
            links = links.merge(cards[rel.CARD_KEYS], on=rel.CARD_KEYS, how="inner")
            mem = members[members.sensor_type == "Состояние фазы"]
            cards["static_list"] = ev.static_list_scores(cards, mem, None).to_numpy()
            first = rel.first_event_starts(cards, links, events)
            eligible, excluded = rel.eligibility(
                cards,
                14,
                period_end=fold["test_end"],
                blocked_days=api["blocked_days"](con, fold["test_end"]),
            )
            selected = rel.select_rolling(
                cards, "static_list", k=10, days=14, eligible=eligible, first=first
            )
            inc = rel.incidents(rel.event_pairs(mem, events))
            captured = rel.honest_events(cards, links, events, selected, 14, "release", first)
            old = rel.card_summary(cards, selected) | rel.recall_summary(captured, inc)
            seq, _ = replay(
                raw["rows"],
                np.asarray([r["static_score"] for r in raw["rows"]]),
                raw["events"],
                start=fold["test_start"],
                end=fold["test_end"],
            )
            # Reconstruct the sequence selector exactly using its stored future release
            # and past-only eligibility. This is a parity audit, never training data.
            from infra_pulse_core.features import incident_list as il

            issued, open_cards = [], []
            for day in sorted(
                {
                    r["day"]
                    for r in raw["rows"]
                    if fold["test_start"] <= r["day"]
                    and pd.Timestamp(r["day"]) + pd.Timedelta(days=14)
                    <= pd.Timestamp(fold["test_end"])
                }
            ):
                rs = [r for r in raw["rows"] if r["day"] == day]
                order = sorted(
                    rs, key=lambda r: (-r["static_score"], il.object_sort_key(r["object_id"]))
                )
                cutoff = il.cutoff_of(pd.Timestamp(day).date())
                pairs = [
                    il.PairAtCutoff(
                        r["object_id"],
                        len(order) - rank,
                        r["eligible"],
                        frozenset(r["candidate_channels"]),
                    )
                    for rank, r in enumerate(order)
                ]
                lookup = {r["object_id"]: r for r in rs}
                for pair, _, _ in il.select_new(cutoff, pairs, open_cards, k=10):
                    row = lookup[pair.object_id]
                    release = (
                        pd.Timestamp(row["first_event"]).to_pydatetime()
                        if row["first_event"]
                        else None
                    )
                    issued.append(row)
                    open_cards.append(il.IssuedCard(pair.object_id, cutoff, release))
                open_cards = [c for c in open_cards if il.is_open(cutoff, c)]
            olds = {
                (str(r.object_id), str(pd.Timestamp(r.issued_at).date())): r
                for r in cards[selected].itertuples(index=False)
            }
            news = {(r["object_id"], r["day"]): r for r in issued}
            shared = olds.keys() & news.keys()
            known_disagreement = sum(
                (o.outcome in rel.EVALUABLE) != (news[key]["y"] >= 0)
                for key, o in olds.items()
                if key in shared
            )
            sequence_rows = {(r["object_id"], r["day"]): r for r in raw["rows"]}
            differences = Counter()
            for j, card in enumerate(cards.itertuples(index=False)):
                key = (str(card.object_id), str(pd.Timestamp(card.issued_at).date()))
                row = sequence_rows.get(key)
                if row is None:
                    differences["v9_row_absent_in_sequence"] += 1
                    continue
                differences["eligible_disagreement"] += bool(eligible[j]) != row["eligible"]
                if card.static_list > 0 and bool(eligible[j]) != row["eligible"]:
                    if pd.Timestamp(row["day"]) + pd.Timedelta(days=14) <= pd.Timestamp(
                        fold["test_end"]
                    ):
                        category = (
                            "no_candidate_channels"
                            if not row["candidate_channels"]
                            else "extra_history_or_past_coverage"
                        )
                        differences["issuable_positive_eligibility:" + category] += 1
                    reason = str(card.exclusion_reason)
                    differences[
                        f"positive_score_eligibility:v9={bool(eligible[j])}:seq={row['eligible']}:{reason}"
                    ] += 1
                score = card.static_list
                differences["score_disagreement"] += not np.isclose(
                    score, row["static_score"], equal_nan=True
                )
                oldfirst = first.iloc[j]
                newfirst = (
                    pd.Timestamp(row["first_event"]).tz_localize(None)
                    if row["first_event"]
                    else pd.NaT
                )
                differences["first_event_disagreement"] += not (
                    (pd.isna(oldfirst) and pd.isna(newfirst)) or oldfirst == newfirst
                )
                if pd.Timestamp(row["day"]) + pd.Timedelta(days=14) == pd.Timestamp(
                    fold["test_end"]
                ):
                    differences["eligible_last_boundary_v9"] += bool(eligible[j])
                    differences["eligible_last_boundary_sequence"] += row["eligible"]
            report["folds"].append(
                {
                    "fold": fold["name"],
                    "cutoff_comparison": dict(differences),
                    "published_protocol_recomputed": old,
                    "sequence_protocol_recomputed": seq,
                    "issued_shared": len(shared),
                    "issued_v9_only": len(olds.keys() - news.keys()),
                    "issued_sequence_only": len(news.keys() - olds.keys()),
                    "shared_known_disagreement": known_disagreement,
                    "sequence_unknown_v9_positive": sum(
                        olds[k].outcome == "positive" and news[k]["y"] < 0 for k in shared
                    ),
                    "v9_denominator": "evaluable event_outcomes with candidate members",
                    "sequence_denominator": "covered heads, including ineligible pairs",
                    "warm_start": "both empty; dev here starts July, published dev starts January",
                    "censoring": "sequence requires full window coverage even for a known positive",
                }
            )
    finally:
        con.close()
    return report


def audit(config, prepared, output):
    verify(prepared, config)
    output.mkdir(parents=True, exist_ok=True)
    raw = json.loads((prepared / "private.json").read_text())
    session_counts = Counter()
    for s in raw["sessions"]:
        month = s["start"][:7]
        session_counts[(month, "structural")] += 1
        session_counts[(month, "legacy")] += s["legacy"]
    schedules = ROOT / "data-science/artifacts/maintenance-2026-planned/manifest.json"
    historical = ROOT / "data-science/artifacts/next-2026-09-27/ds-a-schedules"
    schedule_audit = {
        "manifest_sha256": digest(schedules),
        "manifest": json.loads(schedules.read_text()),
        "mapping_status": "inferred from outcome dates/counts, not independently confirmed",
        "historical_availability": "unknown; not PIT feature",
        "work_execution": "planned dates do not prove execution",
        "to_precision": "month; cannot identify a particular daily event",
        "crosswalk_risk": "2026 outcome-informed matching; not independent validation",
        "object_vs_site": "controlHouse != guardObject; current parent mapping",
        "topology": "consumer/picket names do not identify feeder source",
        "existing_analyses": {
            p.name: {"sha256": digest(p), "result": json.loads(p.read_text())}
            for p in historical.glob("s[234]*.json")
        },
    }
    write_json(
        output / "audit.json",
        {
            "identity": code_identity(),
            "mapping": mapping_table(),
            "schedules": schedule_audit,
            "maintenance_by_month": [
                {
                    "month": m,
                    "structural": session_counts[m, "structural"],
                    "legacy": session_counts[m, "legacy"],
                }
                for m in sorted({k[0] for k in session_counts})
            ],
            "event_components": [
                {"component": c, "family": f, "events": n}
                for (c, f), n in sorted(
                    Counter((e["component"], e["family"]) for e in raw["events"]).items()
                )
            ],
            "fire_risk": {
                "status": "not_identifiable",
                "reason": "no confirmed fire outcomes or verified work execution",
            },
            "customer_evidence": "six answers supplied 2026-09-29; no row-level outcomes",
            "baseline_reconciliation": baseline_audit(),
        },
    )
