"""Reference baselines of the v9 production scope ("phase"), computed AFTER the freeze and the
single holdout run (data analyst request 27.09.2026). **Справочно, после заморозки:** nothing is
selected here, the frozen v9 config and code are not changed, the holdout labels were
already read by the v9 run.

Question: with 18 phase pairs and K = 10 more than half of the pairs hold a card, so a random
list may reach R_incident ≈ 10/18 "for free". Same policy as v9 (14 days, release, daily
issue at 00:00, causal eligibility, honest capture), periods dev 2023, dev 2024 and holdout:

1. random list at K = 5, 7, 10: 1000 random issues among the eligible pairs (a uniform random
   score per card and day), seed 17; P, R_incident and R_incident of fresh incidents —
   median and 2.5–97.5% range, and the share of random issues at least as good as the list;
2. persistence at the same K: only pairs with an event in the past 14 days, the most recent
   first;
3. the v9 static list at the same K (K = 10 and 7 are the frozen points);
4. the base rate: share of pair-days (known outcome) with an event in the 14-day window, and
   the number of eligible pairs;
5. dev only, descriptive: which phase channels take part in the events, by channel name
   (inputs / АВР / ЩАП / section breakers vs feeders such as РО, ГРО, ФАО, ФВ, ФАНС).

Aggregates only; the section ``posthoc_baselines`` of ``reports/sensor-failure-final-v9-...``.

    python data-science/scripts/posthoc_sensor_failure_v9_baselines.py
"""

from __future__ import annotations

import json
import re
import runpy
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling import sensor_failure_release as rel
from infra_pulse_research.modeling.sensor_failure_fe_v7 import pair_event_times

HERE = Path(__file__).resolve().parent
V9 = runpy.run_path(str(HERE / "final_sensor_failure_v9.py"), run_name="v9")
ROOT = V9["ROOT"]
REPORT, OUT = V9["REPORT"], V9["OUT"]
CHANNELS = ROOT / "data-science/artifacts/curated-all-sensors-exclude-2021-q2/channels.parquet"
KEYS, UNIT = rel.CARD_KEYS, rel.UNIT
DAYS = 14
KS = (5, 7, 10)
REPLICATES, SEED = 1000, 17
PERIODS = ("2023", "2024", "holdout")
SCOPE = "phase"
LABEL = "справочно, после заморозки"

# Channel-name classes of the phase sensor (current snapshot names, no validity dates).
INPUT_PATTERNS = {
    "АВР": re.compile(r"авр", re.I),
    "ЩАП": re.compile(r"щап", re.I),
    "межсекционный": re.compile(r"межсекц|секц\.|секцион", re.I),
    "ввод": re.compile(r"ввод|вв\.", re.I),
}
FEEDER = re.compile(
    r"^\W*(\d+\s*)?(группа\s+)?(г?ро|фв|фао|фтс|фро|фанс|фр|фрез|фидер|озк|фозк|"
    r"ф\.?\s*(пит\.\s*)?резерв|ф[а-я]{1,4}\d)",
    re.I,
)
SUPPLY = re.compile(r"питание\s+пуи", re.I)


def channel_class(name) -> str:
    """input (ввод, АВР, ЩАП, section breaker), feeder, ПУИ supply or other."""
    s = str(name)
    if any(p.search(s) for p in INPUT_PATTERNS.values()):
        return "input"
    if FEEDER.search(s):
        return "feeder"
    if SUPPLY.search(s):
        return "pui_supply"
    return "other"


def input_kind(name) -> str | None:
    s = str(name)
    for kind, pattern in INPUT_PATTERNS.items():
        if pattern.search(s):
            return kind
    return None


def persistence_scores(cards: pd.DataFrame, members: pd.DataFrame, days: int = DAYS) -> np.ndarray:
    """Minus the hours since the last event of the pair if it counted in ``[t − days, t)``
    (the static-list moment), otherwise NaN (the pair is not issued)."""
    times = pair_event_times(members, None)
    stamps = {
        k: np.sort(g["count_at"].to_numpy(dtype="datetime64[ns]"))
        for k, g in times.groupby(UNIT, sort=False)
    }
    issued = pd.to_datetime(cards["issued_at"]).to_numpy(dtype="datetime64[ns]")
    out = np.full(len(cards), np.nan)
    window = np.timedelta64(int(days), "D")
    for key, pos in cards.groupby(UNIT, sort=False).indices.items():
        s = stamps.get(key)
        if s is None:
            continue
        t = issued[pos]
        idx = np.searchsorted(s, t, "left")
        has = idx > 0
        last = s[np.maximum(idx - 1, 0)]
        recent = has & (last >= t - window)
        hours = (t - last) / np.timedelta64(1, "h")
        out[pos] = np.where(recent, -hours, np.nan)
    return out


class FastEvaluator:
    """P, R_incident (all and fresh) and R_strict of an issue mask, exactly as
    ``sensor_failure_release.honest_events`` / ``recall_summary`` / ``card_summary`` but
    vectorised for many masks of one card table."""

    def __init__(self, cards, links, events, first, incidents):
        none = np.zeros(len(cards), dtype=bool)
        h = rel.honest_events(cards, links, events, none, DAYS, "release", first)
        self.event_ids = h["event_id"].to_numpy()
        code = {e: i for i, e in enumerate(self.event_ids)}
        inc = h[["event_id"]].merge(
            incidents[["event_id", "incident_first", "incident_fresh"]], on="event_id", how="left"
        )
        self.heads = inc["incident_first"].astype(bool).to_numpy()
        self.fresh = self.heads & inc["incident_fresh"].astype(bool).to_numpy()
        pos = pd.Series(np.arange(len(cards)), index=pd.MultiIndex.from_frame(cards[KEYS]))
        lk = links[links["candidate_member"].astype(bool) & links["event_id"].isin(code)]
        lk = lk.merge(
            events[["event_id", "event_start"]].rename(columns={"event_start": "_start"}),
            on="event_id",
        )
        self.card = pos.reindex(pd.MultiIndex.from_frame(lk[KEYS])).to_numpy()
        self.event = lk["event_id"].map(code).to_numpy()
        start = pd.to_datetime(lk["_start"]).to_numpy()
        f = pd.to_datetime(pd.Series(first).reset_index(drop=True))
        release = rel.release_at(cards["issued_at"], f, DAYS).to_numpy()
        issued = pd.to_datetime(cards["issued_at"]).to_numpy()
        self.open = rel.is_open(start, issued[self.card], release[self.card], DAYS)
        self.first = start == f.to_numpy()[self.card]
        self.known = cards["outcome"].isin(rel.EVALUABLE).to_numpy()
        self.y = np.nan_to_num(cards["y_card"].to_numpy(dtype=float))
        self.pairs = cards[UNIT].apply(tuple, axis=1).to_numpy()

    def metrics(self, mask: np.ndarray) -> dict:
        hit = mask[self.card] & self.open
        n = len(self.event_ids)
        caught = np.bincount(self.event, weights=hit, minlength=n) > 0
        strict = np.bincount(self.event, weights=hit & self.first, minlength=n) > 0
        sel = mask & self.known

        def share(values):
            return float(values.mean()) if len(values) else None

        return {
            "card_precision": share(self.y[sel]),
            "R_incident": share(caught[self.heads]),
            "R_incident_fresh": share(caught[self.fresh]),
            "R_strict": share(strict),
            "cards_issued": int(mask.sum()),
            "pairs_issued": len(set(self.pairs[mask])),
        }


def phase_tables(con, name: str) -> dict:
    """Phase cards of a period with the v9 static list, persistence, eligibility, first
    events and incidents (same loaders and rules as the v9 run)."""
    spec = V9["PERIODS"][name]
    events, members = V9["load_events"](con, spec["data_end"], spec["labels"])
    blocked = V9["blocked_days"](con, spec["data_end"])
    cards, links = V9["load_cards"](con, spec["labels"], DAYS, spec["start"], spec["end"])
    cards = cards[V9["scope_mask"](cards, SCOPE)].reset_index(drop=True)
    links = links.merge(cards[KEYS], on=KEYS, how="inner")
    cards["static_list"] = ev.static_list_scores(cards, members, None).to_numpy()
    cards["persistence"] = persistence_scores(cards, members)
    first = rel.first_event_starts(cards, links, events)
    eligible, excluded = rel.eligibility(
        cards, DAYS, period_end=spec["period_end"], blocked_days=blocked
    )
    phase_members = members[V9["scope_mask"](members, SCOPE)]
    incidents = rel.incidents(rel.event_pairs(phase_members, events))
    return {
        "cards": cards,
        "links": links,
        "events": events,
        "members": members,
        "first": first,
        "eligible": eligible,
        "excluded": excluded,
        "incidents": incidents,
    }


def base_rate(t: dict) -> dict:
    cards, eligible = t["cards"], t["eligible"]
    known = cards["outcome"].isin(rel.EVALUABLE).to_numpy()
    day = pd.to_datetime(cards["issued_at"]).dt.normalize()
    per_day = pd.Series(eligible).groupby(day.to_numpy()).sum()
    per_day = per_day[per_day > 0]
    return {
        "pair_days_known": int(known.sum()),
        "share_with_event_in_14d": float(cards.loc[known, "y_card"].mean()),
        "share_with_event_eligible": float(cards.loc[known & eligible, "y_card"].mean()),
        "eligible_pairs": int(cards.loc[eligible, UNIT].drop_duplicates().shape[0]),
        "eligible_pairs_per_day_median": float(per_day.median()) if len(per_day) else None,
        "eligible_pairs_per_day_min": int(per_day.min()) if len(per_day) else None,
    }


def interval(values: np.ndarray) -> dict:
    v = np.asarray([x for x in values if x is not None], dtype=float)
    return {
        "median": float(np.median(v)),
        "low": float(np.percentile(v, 2.5)),
        "high": float(np.percentile(v, 97.5)),
    }


def evaluate_period(t: dict, reps: int = REPLICATES, seed: int = SEED) -> dict:
    cards, first, eligible = t["cards"], t["first"], t["eligible"]
    fast = FastEvaluator(cards, t["links"], t["events"], first, t["incidents"])
    rng = np.random.default_rng(seed)
    draws = rng.random((reps, len(cards)))
    out = {"base": base_rate(t), "k": {}}
    for k in KS:
        block = {}
        for score in ("static_list", "persistence"):
            mask = rel.select_rolling(cards, score, k=k, days=DAYS, eligible=eligible, first=first)
            block[score] = fast.metrics(mask)
        rows = []
        frame = cards[KEYS + ["outcome"]].copy()
        for r in range(reps):
            frame["_random"] = draws[r]
            mask = rel.select_rolling(
                frame, "_random", k=k, days=DAYS, eligible=eligible, first=first
            )
            rows.append(fast.metrics(mask))
        listed = block["static_list"]
        block["random"] = {
            m: interval([row[m] for row in rows])
            for m in ("card_precision", "R_incident", "R_incident_fresh", "R_strict")
        }
        block["random"]["share_at_least_list"] = {
            m: float(np.mean([row[m] >= listed[m] for row in rows]))
            for m in ("card_precision", "R_incident", "R_incident_fresh")
            if listed[m] is not None
        }
        block["random"]["replicates"] = reps
        block["random"]["seed"] = seed
        block["list_minus_random_median"] = {
            m: listed[m] - block["random"][m]["median"]
            for m in ("card_precision", "R_incident", "R_incident_fresh")
            if listed[m] is not None
        }
        out["k"][str(k)] = block
    return out


def check_fast(t: dict) -> float:
    """Max |fast − v9 point()| over the list at K = 10 and 5 random masks (must be 0)."""
    cards, first, eligible = t["cards"], t["first"], t["eligible"]
    fast = FastEvaluator(cards, t["links"], t["events"], first, t["incidents"])
    prev = np.zeros(len(cards), dtype=bool)
    rng = np.random.default_rng(99)
    frame = cards[KEYS + ["outcome"]].copy()
    masks = [
        rel.select_rolling(cards, "static_list", k=10, days=DAYS, eligible=eligible, first=first)
    ]
    for _ in range(5):
        frame["_r"] = rng.random(len(cards))
        masks.append(
            rel.select_rolling(frame, "_r", k=7, days=DAYS, eligible=eligible, first=first)
        )
    worst = 0.0
    for mask in masks:
        ref = V9["point"](
            cards, t["links"], t["events"], mask, DAYS, first, prev, t["incidents"], t["excluded"]
        )
        got = fast.metrics(mask)
        for m in ("card_precision", "R_incident", "R_incident_fresh", "R_strict", "pairs_issued"):
            worst = max(worst, abs((got[m] or 0) - (ref[m] or 0)))
    return worst


def composition(con, t2023: dict, t2024: dict) -> dict:
    """Dev 2023–2024: channel classes of the phase members of the events (by channel name)."""
    names = con.execute(
        f"SELECT channel_id, name_raw FROM read_parquet({V9['_q'](CHANNELS)}) "
        f"WHERE sensor_type = {V9['_q'](V9['PHASE'])}"
    ).df()
    names["cls"] = names["name_raw"].map(channel_class)
    names["kind"] = names["name_raw"].map(input_kind)
    members = t2024["members"]
    m = members[members["sensor_type"].eq(V9["PHASE"])]
    start = pd.to_datetime(m["start_at"])
    m = m[(start >= pd.Timestamp("2023-01-01")) & (start < pd.Timestamp("2025-01-01"))]
    m = m.merge(names[["channel_id", "cls", "kind"]], on="channel_id", how="left")
    m["cls"] = m["cls"].fillna("no_name")
    per_event = m.groupby("event_id").agg(
        input=("cls", lambda s: bool((s == "input").any())),
        feeder=("cls", lambda s: bool((s == "feeder").any())),
        channels=("channel_id", "nunique"),
    )
    kinds = m.dropna(subset=["kind"]).groupby("event_id")["kind"].agg(set)
    n = len(per_event)
    out = {
        "period": "dev 2023-2024, phase members of the events (all episodes)",
        "names": "current channel names of the curated snapshot (no validity dates); classes "
        "by name: input = ввод / АВР / ЩАП / section breaker; feeder = РО, ГРО, ФВ, ФАО, ФТС, "
        "ФРО, ФАНС, ФР, Фрез., ОЗК, reserve feeders",
        "phase_channels_by_class": names["cls"].value_counts().to_dict(),
        "events": n,
        "share_events_with_input_channel": float(per_event["input"].mean()),
        "share_events_only_feeders": float((per_event["feeder"] & ~per_event["input"]).mean()),
        "share_events_neither": float((~per_event["feeder"] & ~per_event["input"]).mean()),
        "share_events_input_and_feeders": float((per_event["feeder"] & per_event["input"]).mean()),
        "events_with_input_by_kind": {
            k: int(kinds.map(lambda s, k=k: k in s).sum()) for k in INPUT_PATTERNS
        },
        "members_by_class": m["cls"].value_counts().to_dict(),
        "phase_channels_per_event_median": float(per_event["channels"].median()),
        "share_events_single_channel": float((per_event["channels"] == 1).mean()),
    }
    single_input = per_event[per_event["channels"] == 1]["input"]
    out["share_single_channel_events_on_input"] = (
        float(single_input.mean()) if len(single_input) else None
    )
    by_year = {}
    for year in (2023, 2024):
        ids = m.loc[pd.to_datetime(m["start_at"]).dt.year == year, "event_id"].unique()
        pe = per_event.loc[per_event.index.isin(ids)]
        by_year[str(year)] = {
            "events": len(pe),
            "share_with_input": float(pe["input"].mean()) if len(pe) else None,
            "share_only_feeders": float((pe["feeder"] & ~pe["input"]).mean()) if len(pe) else None,
        }
    out["by_year"] = by_year
    # Share of the object's phase feeders that take part in the event (object-wide outage?).
    feeders = names[names["cls"] == "feeder"].merge(
        con.execute(f"SELECT channel_id, object_id FROM read_parquet({V9['_q'](CHANNELS)})").df(),
        on="channel_id",
    )
    per_object = feeders.groupby("object_id")["channel_id"].nunique()
    ev_obj = m.groupby("event_id").agg(
        object_id=("object_id", "first"), n=("channel_id", "nunique")
    )
    cover = ev_obj["n"] / ev_obj["object_id"].map(per_object)
    out["feeder_coverage_of_object"] = {
        "median": float(cover.median()),
        "share_events_all_feeders": float((cover >= 1).mean()),
        "share_events_half_or_more": float((cover >= 0.5).mean()),
        "share_events_one_feeder": float((ev_obj["n"] == 1).mean()),
    }
    out["journal_states_by_class"] = journal_states(names)
    return out


def journal_states(names: pd.DataFrame) -> dict:
    """Dev 2023–2024 journal rows of the phase channels by name class and state."""
    from infra_pulse_research.modeling.fire_source import open_fire_source  # noqa: PLC0415

    con, _ = open_fire_source(ROOT)
    con.execute("SET threads=2; SET memory_limit='4GB'")
    con.register("nm", names[["channel_id", "cls"]])
    df = con.execute(
        f"""SELECT nm.cls, e.value_raw, count(*) AS n, count(DISTINCT e.channel_id) AS ch
        FROM working_events e JOIN nm USING (channel_id)
        WHERE e.sensor_type = {V9["_q"](V9["PHASE"])} AND e.month BETWEEN '2023-01' AND '2024-12'
          AND TRY_CAST(e.event_ts_local_raw AS TIMESTAMP) < TIMESTAMP '2025-01-01'
        GROUP BY 1, 2 ORDER BY 1, 3 DESC"""
    ).df()
    con.close()
    return {
        cls: {
            v: {"rows": int(n), "channels": int(c)}
            for v, n, c in zip(g.value_raw, g.n, g.ch, strict=True)
        }
        for cls, g in df.groupby("cls")
    }


def main() -> None:
    started = perf_counter()
    con = V9["connect"]()
    tables = {p: phase_tables(con, p) for p in PERIODS}
    fast_check = {p: check_fast(t) for p, t in tables.items()}
    if max(fast_check.values()) > 1e-12:
        raise SystemExit(f"fast evaluator differs from v9: {fast_check}")
    result = {
        "label": LABEL,
        "note": "post-hoc reference after the freeze and the single holdout run; no selection, "
        "the v9 config and code are unchanged; same policy (14 days, release, daily issue, "
        "causal eligibility, honest capture)",
        "scope": "phase ('Состояние фазы')",
        "ks": list(KS),
        "random": f"{REPLICATES} random issues per period and K (uniform score per card and "
        f"day among the eligible pairs), seed {SEED}; median and 2.5-97.5% range",
        "persistence": "pairs with an event in the past 14 days, the most recent first",
        "fast_evaluator_max_abs_diff_vs_v9": fast_check,
        "periods": {p: evaluate_period(t) for p, t in tables.items()},
        "composition_dev": composition(con, tables["2023"], tables["2024"]),
    }
    frozen = json.loads(REPORT.read_text(encoding="utf-8"))["holdout_v9"]["configurations"]
    result["check_frozen_points"] = {
        f"K{k}": {
            "posthoc": result["periods"]["holdout"]["k"][str(k)]["static_list"]["R_incident"],
            "v9_holdout": frozen[name]["holdout"]["R_incident"],
        }
        for k, name in ((10, "prod_phase"), (7, "prod_phase_k7"))
    }
    result["seconds"] = round(perf_counter() - started, 1)
    V9["write_json"](OUT / "posthoc_baselines.json", result)
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    points = report.pop("points")
    report["posthoc_baselines"] = result
    report["points"] = points
    V9["write_json"](REPORT, report, one_line="points")
    print(json.dumps(ev._py(result["check_frozen_points"]), ensure_ascii=False))
    print(f"posthoc baselines ({result['seconds']} s)")


if __name__ == "__main__":
    main()
