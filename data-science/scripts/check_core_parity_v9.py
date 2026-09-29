"""Parity of the core phase detector and list with the v9 research code (aggregated).

Compares on the working layer ``journal-curated-v4`` + ``alarm-spike-exclusions-v1``:

1. **Episode starts** of «Состояние фазы» (label ``connection_loss``, all episodes, Q = 24 h)
   of the core detector with ``sensor-failure-v1/tables/episodes_q24h.parquet`` — every
   year, starts and ends.
2. **Score**: the core static list (events of the object in ``[t − 365 d, t)``) with
   ``sensor_failure_evaluation.static_list_scores`` on the research cards of fold 2024
   (14 days); first with the research events (function parity), then with the core
   events (data parity: core events chain phase starts only).
3. **Open sets per day** on fold 2024: the runtime cutoff step
   (``forecast_publish.score_cutoff``) from an empty list on 2024-01-01 against
   ``sensor_failure_release.select_rolling`` with causal eligibility (v9).

The report holds counts only: no object, channel or event IDs.

    uv run --locked --group train --group research python \\
      data-science/scripts/check_core_parity_v9.py --data-root /path/to/checkout \\
      --output data-science/reports/core-research-parity-v9-2026-09-27.json
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from infra_pulse_backend.operations import forecast_publish as fp
from infra_pulse_core.features import incident_list as il
from infra_pulse_core.features.channel_names import classify_channel, name_stem
from infra_pulse_core.features.phase_feeder_episodes import (
    MSK,
    PHASE_SENSOR_TYPE,
    PhaseEpisodeDetector,
    Record,
    cluster_events,
)
from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling import sensor_failure_release as rel

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = "data-science/artifacts/curated-all-sensors-exclude-2021-q2"
POLICY = "data-science/artifacts/alarm-spike-exclusions-v1"
TABLES = "data-science/artifacts/sensor-failure-v1/tables"
E5 = "data-science/artifacts/next-2026-09-27/ds-exploration5/labels"
TARGET = "connection_loss_all"
FOLD = ("2024-01-01", "2025-01-01")
DAYS = 14
K = 10


def _q(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def msk(value) -> datetime:
    return pd.Timestamp(value).to_pydatetime().replace(tzinfo=MSK)


def load_records(con, root: Path, until: str) -> tuple[list[Record], dict]:
    manifest = json.loads((root / POLICY / "manifest.json").read_text(encoding="utf-8"))
    rules = [r for r in manifest["rules"] if r["sensor_type"] in (None, PHASE_SENSOR_TYPE)]
    day = "CAST(TRY_CAST(event_ts_local_raw AS TIMESTAMP) AS DATE)"
    excluded = " OR ".join(
        f"({day} >= DATE {_q(r['start'])} AND {day} < DATE {_q(r['end'])})" for r in rules
    )
    rows = con.execute(
        f"""SELECT CAST(channel_id AS VARCHAR), TRY_CAST(event_ts_local_raw AS TIMESTAMP),
                   value_raw, COALESCE(alarm, false), CAST(object_id AS VARCHAR)
            FROM read_parquet({_q(root / SNAPSHOT / "curated/*/*.parquet")})
            WHERE sensor_type = {_q(PHASE_SENSOR_TYPE)} AND channel_id IS NOT NULL
              AND TRY_CAST(event_ts_local_raw AS TIMESTAMP) IS NOT NULL
              AND TRY_CAST(event_ts_local_raw AS TIMESTAMP) < TIMESTAMP {_q(until)}
              AND NOT ({excluded or "false"})"""
    ).fetchall()
    records = [Record(c, t.replace(tzinfo=MSK), v, a, o) for c, t, v, a, o in rows]
    return records, {"excluded_rules": len(rules), "records": len(records)}


def episode_parity(con, root: Path, episodes) -> dict:
    research = con.execute(
        f"""SELECT CAST(channel_id AS VARCHAR), start_at, end_at
            FROM read_parquet({_q(root / TABLES / "episodes_q24h.parquet")})
            WHERE label = 'connection_loss' AND sensor_type = {_q(PHASE_SENSOR_TYPE)}"""
    ).fetchall()
    rs = {(c, msk(s)): (msk(e) if e is not None else None) for c, s, e in research}
    cs = {e.key: e.end_at for e in episodes}
    years = collections.defaultdict(lambda: collections.Counter())
    for key in set(rs) | set(cs):
        year = key[1].year
        years[year]["research" if key in rs else "core_only"] += key in rs
        if key in rs and key in cs:
            years[year]["matched"] += 1
            years[year]["end_mismatch"] += rs[key] != cs[key]
        elif key in rs:
            years[year]["research_only"] += 1
        else:
            years[year]["core_only"] += 1
    for counter in years.values():
        counter["research"] = counter["matched"] + counter["research_only"]
        counter["core"] = counter["matched"] + counter["core_only"]
    return {
        "research": len(rs),
        "core": len(cs),
        "matched": len(set(rs) & set(cs)),
        "research_only": len(set(rs) - set(cs)),
        "core_only": len(set(cs) - set(rs)),
        "end_mismatch": sum(1 for k in set(rs) & set(cs) if rs[k] != cs[k]),
        "by_year": {
            str(y): {k: int(v) for k, v in sorted(c.items())} for y, c in sorted(years.items())
        },
    }


def fold_frames(con, root: Path):
    months = [f"2024-{m:02d}" for m in range(1, 13)]
    out = []
    for kind in ("cards", "links"):
        files = (
            "["
            + ", ".join(_q(root / E5 / f"{kind}/{TARGET}/336h/{m}.parquet") for m in months)
            + "]"
        )
        out.append(
            con.execute(
                f"SELECT * FROM read_parquet({files}) WHERE sensor_type = {_q(PHASE_SENSOR_TYPE)}"
            ).df()
        )
    base = root / E5 / "events" / TARGET
    events = con.execute(f"SELECT * FROM read_parquet({_q(base / 'events.parquet')})").df()
    members = con.execute(f"SELECT * FROM read_parquet({_q(base / 'event_members.parquet')})").df()
    return out[0].reset_index(drop=True), out[1], events, members


def blocked(con, root: Path) -> pd.DataFrame:
    cov = con.execute(
        f"""SELECT day, CAST(NULL AS VARCHAR) AS sensor_type_scope
            FROM read_parquet({_q(root / SNAPSHOT / "coverage.parquet")})
            WHERE day < DATE '2025-01-01' AND NOT working_covered"""
    ).df()
    pol = con.execute(
        f"""SELECT day, sensor_type_scope
            FROM read_parquet({_q(root / POLICY / "policy_days.parquet")})
            WHERE day < DATE '2025-01-01'"""
    ).df()
    return pd.concat([cov, pol], ignore_index=True)


def score_parity(cards, members, core_events) -> dict:
    phase_members = members[members["sensor_type"] == PHASE_SENSOR_TYPE]
    research = ev.static_list_scores(cards, phase_members, None).to_numpy()
    # Function parity: the core score over the research events of each phase pair.
    pairs = phase_members.groupby(["event_id", "object_id"])["start_at"].min().reset_index()
    research_events = il.EventIndex(
        [
            _event(str(row.object_id), msk(row.start_at), row.event_id)
            for row in pairs.itertuples(index=False)
        ]
    )
    core_index = il.EventIndex(core_events)
    function, data = [], []
    for row, value in zip(cards.itertuples(index=False), research, strict=True):
        t = msk(row.issued_at)
        function.append(research_events.score(str(row.object_id), t) - value)
        data.append(core_index.score(str(row.object_id), t) - value)
    function, data = np.array(function), np.array(data)
    return {
        "cards": len(cards),
        "function_parity": {
            "equal": int((function == 0).sum()),
            "max_abs_diff": float(abs(function).max()),
        },
        "data_parity": {
            "equal": int((data == 0).sum()),
            "differ": int((data != 0).sum()),
            "max_abs_diff": float(abs(data).max()),
            "mean_diff": float(data.mean()),
        },
    }


def _event(object_id: str, start: datetime, event_id: str):
    from infra_pulse_core.features.phase_feeder_episodes import PhaseEvent

    return PhaseEvent(event_id, object_id, start, start, ("_",), (start,))


def research_open_sets(cards, links, events, members, blocked_days) -> dict[date, set[str]]:
    cards = cards.copy()
    phase_members = members[members["sensor_type"] == PHASE_SENSOR_TYPE]
    cards["static_list"] = ev.static_list_scores(cards, phase_members, None).to_numpy()
    first = rel.first_event_starts(cards, links, events)
    eligible, _ = rel.eligibility(cards, DAYS, period_end=FOLD[1], blocked_days=blocked_days)
    mask = rel.select_rolling(cards, "static_list", k=K, days=DAYS, eligible=eligible, first=first)
    selected = cards[mask].copy()
    selected["release"] = rel.release_at(selected["issued_at"], first[mask], DAYS).to_numpy()
    out = {}
    for day in pd.date_range(FOLD[0], "2024-12-18", freq="D"):
        open_ = rel.is_open([day] * len(selected), selected["issued_at"], selected["release"], DAYS)
        out[day.date()] = {str(o) for o in selected.loc[open_, "object_id"]}
    return out, int(mask.sum())


def core_open_sets(world: fp.World) -> tuple[dict[date, set[str]], int]:
    issued: list[fp.CardRow] = []
    releases: dict[str, datetime | None] = {}
    out = {}
    t = il.cutoff_of(date(2024, 1, 1))
    end = il.cutoff_of(date(2024, 12, 18))
    data_as_of = il.cutoff_of(date(2025, 2, 1))
    count = 0
    while t <= end:
        step = fp.score_cutoff(
            world,
            mode="replay",
            t=t,
            data_as_of=data_as_of,
            run_id="parity",
            issued=issued,
            releases=releases,
        )
        for card, candidates in step.cards:
            if card.status != "scored":
                continue
            row = fp.CardRow(card.id, card.object_id, card.status, card.issued_at, candidates)
            issued.append(row)
            releases[card.id] = fp.card_outcome(world, row, data_as_of).release_at
            count += 1
        open_cards = [
            il.IssuedCard(card.object_id, card.issued_at, releases[card.card_id]) for card in issued
        ]
        out[il.msk_day(t)] = il.held_objects(t, open_cards)
        t += timedelta(days=1)
    return out, count


def build_world(con, root: Path, episodes, candidates, detector) -> fp.World:
    channels = con.execute(
        f"""SELECT CAST(channel_id AS VARCHAR), CAST(object_id AS VARCHAR), name_raw, tag_raw,
                   system_type
            FROM read_parquet({_q(root / SNAPSHOT / "channels.parquet")})
            WHERE sensor_type = {_q(PHASE_SENSOR_TYPE)} AND object_id IS NOT NULL"""
    ).fetchall()
    per_object: dict[str, list[fp.ChannelInfo]] = {}
    for channel_id, object_id, name, tag, system in channels:
        layout = classify_channel(name)
        state = detector.states.get(channel_id)
        per_object.setdefault(object_id, []).append(
            fp.ChannelInfo(
                channel_id,
                object_id,
                name,
                tag,
                layout.role,
                layout.feeder_kind,
                layout.picket.form,
                layout.picket.picket_from,
                layout.picket.picket_to,
                layout.picket.basis,
                state.first_seen if state else None,
                system,
            )
        )
    coverage_rows = con.execute(
        f"SELECT day, working_covered FROM read_parquet({_q(root / SNAPSHOT / 'coverage.parquet')})"
    ).fetchall()
    policy = {
        d
        for d, s in con.execute(
            "SELECT day, sensor_type_scope FROM "
            f"read_parquet({_q(root / POLICY / 'policy_days.parquet')})"
        ).fetchall()
        if s in (None, PHASE_SENSOR_TYPE)
    }
    events = cluster_events(episodes)
    first_seen = {}
    for object_id, infos in per_object.items():
        seen = [i.first_seen for i in infos if i.first_seen is not None]
        if seen:
            first_seen[object_id] = min(seen)
    return fp.World(
        channels=per_object,
        episodes=il.EpisodeIndex(episodes, candidates),
        events=il.EventIndex(events),
        power_off={(e.channel_id, e.start_at): e.power_off_at is not None for e in episodes},
        coverage={d: (bool(c), d in policy) for d, c in coverage_rows},
        object_days={},
        first_seen=first_seen,
    ), events


def compare_sets(research: dict, core: dict) -> dict:
    days = sorted(research)
    same = [d for d in days if research[d] == core.get(d, set())]
    diff = [d for d in days if d not in same]
    sym = [len(research[d] ^ core.get(d, set())) for d in diff]
    return {
        "days": len(days),
        "days_equal": len(same),
        "days_differ": len(diff),
        "first_differ": diff[0].isoformat() if diff else None,
        "max_pairs_differ": max(sym) if sym else 0,
        "open_research_mean": round(float(np.mean([len(research[d]) for d in days])), 3),
        "open_core_mean": round(float(np.mean([len(core.get(d, set())) for d in days])), 3),
    }


def event_parity(members, core_events) -> dict:
    """Phase event starts: research events (all label types) restricted to phase pairs vs
    core events (phase starts only)."""
    phase = members[members["sensor_type"] == PHASE_SENSOR_TYPE]
    research = phase.groupby(["event_id", "object_id"])["start_at"].min().reset_index()
    rs = collections.Counter((str(r.object_id), msk(r.start_at)) for r in research.itertuples())
    cs = collections.Counter((e.object_id, e.start_at) for e in core_events)
    mixed = members.groupby("event_id")["sensor_type"].nunique()
    mixed_phase = mixed[mixed > 1].index.intersection(phase["event_id"].unique())
    return {
        "research_phase_events": sum(rs.values()),
        "core_events": sum(cs.values()),
        "first_phase_start_equal": sum((rs & cs).values()),
        "research_only": sum((rs - cs).values()),
        "core_only": sum((cs - rs).values()),
        "research_events_mixing_types_with_phase": int(len(mixed_phase)),
    }


def layout_review(con, root: Path) -> dict:
    names = [
        n
        for (n,) in con.execute(
            f"""SELECT name_raw FROM read_parquet({_q(root / SNAPSHOT / "channels.parquet")})
                WHERE sensor_type = {_q(PHASE_SENSOR_TYPE)}"""
        ).fetchall()
    ]
    kinds = collections.Counter()
    pickets = collections.Counter()
    other = collections.Counter()
    for name in names:
        layout = classify_channel(name)
        kinds[
            layout.landmark_kind
            and f"landmark:{layout.landmark_kind}"
            or f"feeder:{layout.feeder_kind}"
        ] += 1
        pickets[layout.picket.form] += 1
        if layout.role == "feeder" and layout.feeder_kind == "other":
            other[name_stem(name)] += 1
    return {
        "phase_channels": len(names),
        "by_kind": dict(sorted(kinds.items())),
        "picket_forms": dict(sorted(pickets.items())),
        "feeders_other_stems": [
            {"stem": stem, "channels": count} for stem, count in other.most_common()
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.data_root
    started = time.perf_counter()
    con = duckdb.connect()
    con.execute("SET threads=4")
    records, source = load_records(con, root, "2026-07-01")
    detector = PhaseEpisodeDetector()
    delta = detector.process(records)
    episodes = sorted(delta.episodes.values(), key=lambda e: (e.start_at, e.channel_id))
    report: dict = {
        "check": "core-research-parity-v9",
        "config": il.CONFIG_VERSION,
        "detector": il.LABEL_VERSION,
        "source": {**source, "layer": "journal-curated-v4 + alarm-spike-exclusions-v1"},
        "episodes": episode_parity(con, root, episodes),
    }
    world, core_events = build_world(con, root, episodes, delta.candidates, detector)
    cards, links, events, members = fold_frames(con, root)
    report["events"] = event_parity(members, core_events)
    report["score_fold_2024"] = score_parity(cards, members, core_events)
    research_sets, research_cards = research_open_sets(
        cards, links, events, members, blocked(con, root)
    )
    core_sets, core_cards = core_open_sets(world)
    report["open_sets_fold_2024"] = {
        **compare_sets(research_sets, core_sets),
        "cards_issued_research": research_cards,
        "cards_issued_core": core_cards,
        "k": K,
        "days_window": DAYS,
        "period": [FOLD[0], "2024-12-18"],
    }
    report["layout"] = layout_review(con, root)
    report["seconds"] = round(time.perf_counter() - started, 1)
    try:
        report["git_sha"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, cwd=ROOT
        ).stdout.strip()
        report["git_dirty"] = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
                cwd=ROOT,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        report["git_sha"] = None
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
