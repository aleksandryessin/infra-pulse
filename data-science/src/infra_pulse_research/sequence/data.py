"""Causal snapshots and separate censored labels, using the canonical phase detector."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from infra_pulse_core.features import incident_list as il
from infra_pulse_core.features.phase_feeder_episodes import Record, cluster_events, detect

DAY = timedelta(days=1)


def dates(start: date, end: date):
    while start < end:
        yield start
        start += DAY


@dataclass
class Prepared:
    states: np.ndarray
    numeric: np.ndarray
    lengths: np.ndarray
    tabular: np.ndarray
    rows: list[dict]
    events: list[dict]


def prepare_object(
    records: list[Record],
    covered: set[date],
    start: date,
    end: date,
    *,
    max_steps: int = 128,
    history_days: int = 90,
) -> Prepared:
    """One object's history in memory. No future observation enters an input tensor.

    Availability is simulated at source time. Coverage is archive coverage, not proof
    of an individual device's health. Full 14d coverage is required for either label.
    """
    if not records or max_steps < 1 or history_days < 1 or start >= end:
        raise ValueError("nonempty records/range and positive sequence limits required")
    objects = {r.object_id for r in records}
    if len(objects) != 1 or None in objects:
        raise ValueError("prepare_object requires exactly one known object")
    if any(r.event_at.tzinfo is None for r in records):
        raise ValueError("timezone-aware observations required")
    # Stable ordering for simultaneous records: no arbitrary input-file ordering.
    records = sorted(
        set(records),
        key=lambda r: (
            r.event_at,
            r.channel_id,
            r.value_raw or "",
            r.alarm,
        ),
    )
    object_id = records[0].object_id
    times = [r.event_at for r in records]
    episodes, candidates = detect(records)
    events = cluster_events(episodes)
    event_index = il.EventIndex(events)
    episode_index = il.EpisodeIndex(episodes, candidates)
    first_seen = {}
    previous = {}
    gaps = []
    for record in records:
        first_seen.setdefault(record.channel_id, record.event_at)
        prev = previous.get(record.channel_id)
        gaps.append((record.event_at - prev).total_seconds() / 86400 if prev else 0.0)
        previous[record.channel_id] = record.event_at
    states, numeric, lengths, tabular, rows = [], [], [], [], []
    for day in dates(start, end):
        t = il.cutoff_of(day)
        hi = bisect_left(times, t)
        lo_all = bisect_left(times, t - DAY * history_days)
        lo = max(lo_all, hi - max_steps)
        history = records[lo:hi]
        n = max(1, len(history))
        raw = [r.value_raw if r.value_raw is not None else "<NULL>" for r in history]
        raw = raw or ["<NO_HISTORY>"]
        states.append(raw + [""] * (max_steps - n))
        history_coverage = (
            sum(d in covered for d in dates(day - DAY * history_days, day)) / history_days
        )
        values = np.zeros((max_steps, 4), dtype=np.float32)
        for j, r in enumerate(history):
            values[j] = [
                np.log1p((t - r.event_at).total_seconds() / 86400),
                np.log1p(gaps[lo + j]),
                float(r.alarm),
                history_coverage,
            ]
        numeric.append(values)
        lengths.append(n)
        candidate_channels = frozenset(
            c
            for c, first in first_seen.items()
            if il.channel_at_cutoff(
                c,
                t,
                first_seen=first,
                last_candidate_before=episode_index.last_candidate_before(c, t),
                active_episode=episode_index.active_at(c, t),
            ).candidate
        )
        score = event_index.score(object_id, t)
        eligible = (
            times[0] <= t - DAY * 365
            and day - DAY in covered
            and bool(candidate_channels)
            and score >= il.MIN_EVENTS_FOR_CARD
        )
        # Eligibility never uses the future outcome or coverage.
        known = all(d in covered for d in dates(day, day + DAY * 14))
        first_event = event_index.first_event(object_id, t, candidate_channels)
        past = event_index.between(object_id, t - DAY * 365, t)
        age = (t - past[-1].start_at).total_seconds() / 86400 if past else 366.0
        tabular.append(
            [
                *(event_index.count(object_id, t - DAY * d, t) for d in (7, 30, 90, 365)),
                age,
                *(
                    event_index.count(object_id, t - DAY * k, t - DAY * (k - 1))
                    for k in range(1, 8)
                ),
                history_coverage,
                len(history),
                float(hi - lo_all > max_steps),
                day.weekday(),
            ]
        )
        rows.append(
            {
                "object_id": object_id,
                "day": day.isoformat(),
                "eligible": bool(eligible),
                "candidate_channels": sorted(candidate_channels),
                "static_score": score,
                "recurrence": event_index.recurrence(object_id, t),
                "y": int(first_event is not None) if known else -1,
                "first_event": first_event.start_at.isoformat() if first_event else None,
                "truncated": hi - lo_all > max_steps,
            }
        )
    event_rows = [
        {
            "event_id": e.event_id,
            "object_id": e.object_id,
            "at": e.start_at.isoformat(),
            "channels": list(e.channel_ids),
            "covered": il.msk_day(e.start_at) in covered,
        }
        for e in events
    ]
    return Prepared(
        np.asarray(states),
        np.asarray(numeric),
        np.asarray(lengths),
        np.asarray(tabular, dtype=np.float32),
        rows,
        event_rows,
    )


def combine(parts: list[Prepared]) -> Prepared:
    if not parts:
        raise ValueError("no phase objects in input")
    return Prepared(
        *(
            np.concatenate([getattr(p, name) for p in parts])
            for name in ("states", "numeric", "lengths", "tabular")
        ),
        [r for p in parts for r in p.rows],
        [e for p in parts for e in p.events],
    )


def split_indices(rows: list[dict], fold: dict) -> dict[str, np.ndarray]:
    """Half-open periods, with 14d label purge at train/validation/test boundaries."""
    boundaries = [
        date.fromisoformat(fold[k])
        for k in ("train_start", "validation_start", "test_start", "test_end")
    ]
    if boundaries != sorted(set(boundaries)):
        raise ValueError("fold must progress strictly from past to future")
    result = {}
    for name, left, right in zip(
        ("train", "validation", "test"), boundaries[:-1], boundaries[1:], strict=True
    ):
        result[name] = np.asarray(
            [
                i
                for i, r in enumerate(rows)
                if left <= date.fromisoformat(r["day"])
                and date.fromisoformat(r["day"]) + DAY * 14 <= right
                and r["eligible"]
                and r["y"] >= 0
            ],
            dtype=np.int64,
        )
        if not len(result[name]):
            raise ValueError(f"empty evaluable {name} split after label purge")
    return result


class Vocabulary:
    """Fit exact textual states on training snapshots only. 0=padding, 1=unknown."""

    def __init__(self, tokens: dict[str, int]):
        self.tokens = tokens

    @classmethod
    def fit(cls, states: np.ndarray, lengths: np.ndarray) -> Vocabulary:
        values = {str(s) for row, n in zip(states, lengths, strict=True) for s in row[:n]}
        return cls({s: i + 2 for i, s in enumerate(sorted(values))})

    def transform(self, states: np.ndarray, lengths: np.ndarray) -> np.ndarray:
        out = np.zeros(states.shape, dtype=np.int64)
        for i, (row, n) in enumerate(zip(states, lengths, strict=True)):
            out[i, :n] = [self.tokens.get(str(s), 1) for s in row[:n]]
        return out
