"""Static list «обесточивание электрооборудования объекта» (ID: phase feeders): score,
rolling list with release, «k из n».

Frozen production point of ``sensor_failure_final_v9.json`` (``prod_phase``): one card
per object (object × «Состояние фазы»), issued daily at 00:00 MSK, a 14-day rolling list
of at most K = 10 open cards; a card leaves the list at the start of the first event of
its pair in the window (release) and its place is filled from the next cutoff.
Pure Python, shared by the runtime worker and the research parity check.

- **Score** (``static_list_365``): events of the object in ``[t − 365 d, t)``, counted
  at the event start (all episodes: an event is known at its first start).
- **Candidate channel at t** (``LABELS.md``, model protocol): known before t, not in an
  active episode at t and not in the Q shadow (a candidate in ``(t − 24 h, t)``).
- **Causal eligibility** (``sensor_failure_release.eligibility``): at least one
  candidate channel and a covered lookback: the day before the cutoff has data and is
  not a policy day. Coverage of the future window never blocks an issue.
- **Selection** (``select_rolling``): pairs without an open card are added highest
  score first (ties by object ID, numeric when it is a number) until K are open; an
  open card is never displaced. A pair with no event in the 365 days before t
  (score 0) gets no card, so the list may hold fewer than K cards (product rule 27.09,
  C0.3; for v9 the issue does not change, checked by DS). A card is open at ``x`` iff
  ``t <= x < t + D`` and
  ``x <= release_at``; an event exactly at a cutoff keeps the card in that issue.
- **Release / realized**: the first event starting in ``[t, t + D)`` with a member on a
  channel that was a candidate of the card at t.
- **«k из n»**: the share of dev cards (2023 + 2024, v9 table) with an event in their
  window among cards of the same large bin of the 365-day count, with the Wilson
  interval. Shown as «Оценка вероятности для карточек этого уровня»: an empirical
  probability of the level, not of the object; on the 2025-07..2026-06 holdout the table
  is calibrated (ECE 0.028, data-science/experiments/sensor-failure/PROBABILITY_V11.md).
- **Recurrence**: ``chronic`` when the object had an event in the 14 days before t
  (``chronic_share_issued`` of v9), else ``fresh``.
- **Recommendation** (``recommend``, v5): one main text for every scored card and up to three
  details — a realized repeat of the pair within 30 days and up to two groups of the card
  lines (``channel_names.recommendation_group``) or the object's section switch. The API
  assembles it at read time from the issued card and what was known at its issue.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from infra_pulse_core.features.channel_names import recommendation_group
from infra_pulse_core.features.phase_feeder_episodes import (
    DETECTOR_VERSION,
    MSK,
    PhaseEvent,
    Q,
)

CONFIG_VERSION = "sensor-failure-final-v9"
CONFIGURATION = "prod_phase"
WINDOW_DAYS = 14
MAX_OPEN = 10
SCORE_WINDOW_DAYS = 365
RECURRENCE_DAYS = 14
INCIDENT_GAP = timedelta(hours=24)
FRESH_DAYS = 14
# v2 (C0.3): no card for a pair without events in the past 365 days.
LIST_POLICY_VERSION = "phase-feeders-rolling-14d-k10-release-min1-v2"
MIN_EVENTS_FOR_CARD = 1
SCORE_VERSION = "phase-feeders-static-list-365d-v1"
FEATURE_VERSION = "phase-feeder-events-365d-v1"
LABEL_VERSION = DETECTOR_VERSION
RECURRENCE_RULE_VERSION = "phase-feeders-recurrence-event-14d-v1"
RECURRING_RULE_VERSION = "phase-feeders-recurring-events-365d-ge38-v1"
RECURRING_MIN_EVENTS = 38
METRIC_DEFINITION_VERSION = "sensor-failure-final-v9"
# v2 (C0.3): the same action worded with «линии электропитания» instead of «фидеры».
# v3 (28.09, independent audit): in 56% of events (dev 2023–2024; shifted control 0.2–0.6%)
# the panel (ЩАП/АВР) reports «Обесточен» in the same minutes, so the inspection starts at the
# panel; the card lives 14 days, so the action is due by the card's term, not «за последние
# сутки». Regulation still unconfirmed.
# v4 (29.09): the customer answered on 28.09 that a failure may lie anywhere, including the
# utility (ресурсоснабжающая организация); in 16.7% of v9 events another object has an event
# within ±10 min (shifted control 1.6–3.1%, dev 2023–2024, data analyst). Hence «уточнить плановые
# отключения у ресурсоснабжающей организации»; the text stays within 300 characters (C0.4).
# v5 (29.09, ТЗ §18 п. 4: one text for every card was the weakest point; technologist spec
# recommendation-v5): the main text is one for every scored card (the level branch of the spec
# is not adopted, ``decision_code`` stays None) without the repeated «электропитания»; up to three
# details follow it (``recommend``): a realized repeat and at most two groups of the card lines
# by ``RECOMMENDATION_GROUP_ORDER``. Every input is known at the issue. Regulation unconfirmed.
RECOMMENDATION_POLICY_VERSION = "phase-feeders-recommendation-v5"
RECOMMENDATION_TEXT = (
    "Передать дежурному энергетику для осмотра до срока карточки: щит объекта (ЩАП/АВР) — "
    "переключения, автоматы, контакты — и линии из карточки; проверить резервное питание "
    "шкафа ДУ/СМВУ; уточнить плановые отключения у ресурсоснабжающей организации. "
    "Результат записать в карточке"
)
ABSTAINED_RECOMMENDATION_TEXT = "Прогноз не выдан: проверить поступление данных по объекту"
# Repeat: the latest earlier scored card of the pair issued within 30 days left the list at its
# event not later than this issue. n counts the pair's cards in [t − 30 d, t] with this one, as
# the «Повторы» row of the card. On the stand copy (rehearsal export 29.09, 1 696 scored cards
# 2023–2026) the repeat holds for 73.6% of cards; with n ≥ 3 — 73.1%, n ≥ 4 — 63.7%, n ≥ 5 —
# 48.1% (holdout 07.2025–06.2026: 51.7%), n ≥ 6 — 33.0%. The detail marks really frequent cards,
# not «3 of 4»: n ≥ 5 (coordinator decision 29.09, DECISION_LOG).
RECOMMENDATION_REPEAT_DAYS = 30
RECOMMENDATION_REPEAT_MIN_CARDS = 5
RECOMMENDATION_REPEAT_TEXT = (
    "Повторная карточка ({n}-я за 30 сут), прошлая сбылась: при осмотре выяснить, что нашли "
    "тогда, и начать с линий прошлого события."
)
# Groups: safety of the descent first (water, air, temperature), then fire dampers, then light;
# lighting is in almost every card and would hide the difference between objects. «section» is
# a section switch («Межсекционный», landmark ``other``) in the object's layout, not a card line.
RECOMMENDATION_GROUP_ORDER = ("pumps", "ventilation", "heating", "ozk", "lighting", "section")
RECOMMENDATION_MAX_GROUPS = 2
RECOMMENDATION_GROUP_TEXT = {
    "pumps": (
        "Насосы (ФАНС): уточнить, работает ли откачка, и уровень воды в приямке насосной станции."
    ),
    "ventilation": (
        "Вентиляция (ФВ): перед спуском персонала — проветривание отсека и показания "
        "газоанализаторов."
    ),
    "heating": (
        "Теплосеть (ФТС): линия питает оборудование теплосети — сообщить её дежурной службе; "
        "перед спуском проверить температуру в отсеке."
    ),
    "ozk": (
        "ОЗК: проверить положение огнезадерживающих клапанов — после отключения питания они "
        "могут остаться закрытыми и перекрыть вентиляцию."
    ),
    # «аварийное освещение» is the normative term of СП 265.1325800.2016 (card wording allows it).
    "lighting": (
        "Освещение (ФАО, ФРО): для безопасного осмотра проверить, что аварийное освещение на "
        "пути бригады включается из диспетчерского пункта."
    ),
    "section": "Межсекционный автомат: проверить его положение после переключения АВР.",
}

# v9 «k из n» of prod_phase on dev 2023 + 2024 (FINAL_V9, «Калибровка k из n»): the fine
# bins of the report merged into three large bins of the 365-day count, as DS asked
# («по крупным бинам, как нижнюю оценку»). (events_from, events_to, n, k, level)
FREQUENCY_TABLE_VERSION = "phase-feeders-k-of-n-dev2023-2024-3bins-v1"
FREQUENCY_PERIOD = (date(2023, 1, 1), date(2025, 1, 1))
FREQUENCY_SOURCE = (
    "data-science/reports/sensor-failure-final-v9-2026-09-27.json"
    "#holdout_v9.calibration_k_of_n_prod_phase (dev bins merged)"
)
FREQUENCY_BINS: tuple[tuple[int, int | None, int, int, str], ...] = (
    (0, 37, 335, 181, "medium"),
    (38, 62, 264, 202, "high"),
    (63, None, 324, 272, "high"),
)


def cutoff_of(day: date) -> datetime:
    """The 00:00 MSK cutoff of a calendar day."""
    return datetime(day.year, day.month, day.day, tzinfo=MSK)


def msk_day(moment: datetime) -> date:
    return moment.astimezone(MSK).date()


def object_sort_key(object_id: str) -> tuple:
    """Research orders ties by the numeric object ID."""
    return (0, int(object_id), object_id) if object_id.isdigit() else (1, 0, object_id)


# -- channels at a cutoff ---------------------------------------------------------------


@dataclass(frozen=True)
class ChannelAtCutoff:
    channel_id: str
    known: bool
    active_episode: bool
    q_shadow: bool

    @property
    def candidate(self) -> bool:
        return self.known and not self.active_episode and not self.q_shadow


def channel_at_cutoff(
    channel_id: str,
    t: datetime,
    *,
    first_seen: datetime | None,
    last_candidate_before: datetime | None,
    active_episode: bool,
) -> ChannelAtCutoff:
    """``last_candidate_before`` is the latest candidate timestamp strictly before t."""
    return ChannelAtCutoff(
        channel_id=channel_id,
        known=first_seen is not None and first_seen < t,
        active_episode=active_episode,
        q_shadow=last_candidate_before is not None and last_candidate_before > t - Q,
    )


class EpisodeIndex:
    """Per-channel sorted episode starts/ends and candidate timestamps for cutoff queries."""

    def __init__(self, episodes: Iterable, candidates: Iterable[tuple[str, datetime]]):
        self.starts: dict[str, list[datetime]] = {}
        self.ends: dict[str, list[datetime | None]] = {}
        self.candidates: dict[str, list[datetime]] = {}
        rows = sorted(episodes, key=lambda e: (e.channel_id, e.start_at))
        for episode in rows:
            self.starts.setdefault(episode.channel_id, []).append(episode.start_at)
            self.ends.setdefault(episode.channel_id, []).append(episode.end_at)
        for channel_id, at in sorted(candidates):
            self.candidates.setdefault(channel_id, []).append(at)

    def last_candidate_before(self, channel_id: str, t: datetime) -> datetime | None:
        times = self.candidates.get(channel_id, [])
        index = bisect_left(times, t)
        return times[index - 1] if index else None

    def active_at(self, channel_id: str, t: datetime) -> bool:
        """An episode started before t that has not ended before t."""
        starts = self.starts.get(channel_id, [])
        index = bisect_left(starts, t)
        if not index:
            return False
        end = self.ends[channel_id][index - 1]
        return end is None or end >= t

    def episodes_between(self, channel_id: str, start: datetime, end: datetime) -> int:
        starts = self.starts.get(channel_id, [])
        return bisect_left(starts, end) - bisect_left(starts, start)

    def last_start_before(self, channel_id: str, t: datetime) -> datetime | None:
        starts = self.starts.get(channel_id, [])
        index = bisect_left(starts, t)
        return starts[index - 1] if index else None


# -- score, eligibility, selection -------------------------------------------------------


class EventIndex:
    """Events of each object sorted by start (the static list and release lookups)."""

    def __init__(self, events: Iterable[PhaseEvent]):
        self.by_object: dict[str, list[PhaseEvent]] = {}
        for event in sorted(events, key=lambda e: (e.start_at, e.event_id)):
            self.by_object.setdefault(event.object_id, []).append(event)
        self.starts = {key: [e.start_at for e in value] for key, value in self.by_object.items()}

    def count(self, object_id: str, start: datetime, end: datetime) -> int:
        times = self.starts.get(object_id, [])
        return bisect_left(times, end) - bisect_left(times, start)

    def between(self, object_id: str, start: datetime, end: datetime) -> list[PhaseEvent]:
        times = self.starts.get(object_id, [])
        return self.by_object.get(object_id, [])[
            bisect_left(times, start) : bisect_left(times, end)
        ]

    def score(self, object_id: str, t: datetime, days: int = SCORE_WINDOW_DAYS) -> int:
        return self.count(object_id, t - timedelta(days=days), t)

    def recurrence(self, object_id: str, t: datetime) -> str:
        chronic = self.count(object_id, t - timedelta(days=RECURRENCE_DAYS), t) > 0
        return "chronic" if chronic else "fresh"

    def first_event(
        self, object_id: str, t: datetime, candidates: Iterable[str], days: int = WINDOW_DAYS
    ) -> PhaseEvent | None:
        """First event in ``[t, t + D)`` with a member on a card candidate channel."""
        wanted = set(candidates)
        for event in self.between(object_id, t, t + timedelta(days=days)):
            if wanted.intersection(event.channel_ids):
                return event
        return None


@dataclass(frozen=True)
class PairAtCutoff:
    object_id: str
    score: int
    eligible: bool
    candidate_channels: frozenset[str]
    reason: str | None = None


def eligibility_reason(
    t: datetime,
    *,
    candidate_channels: int,
    lookback_covered: bool,
) -> str | None:
    """Causal eligibility (``sensor_failure_release.eligibility``, lookback of one day)."""
    if candidate_channels <= 0:
        return "no_candidate_channels"
    if not lookback_covered:
        return "lookback_coverage"
    return None


def rank_pairs(pairs: Iterable[PairAtCutoff]) -> list[PairAtCutoff]:
    """Eligible pairs by score (descending), ties by object ID."""
    return sorted(
        (pair for pair in pairs if pair.eligible),
        key=lambda pair: (-pair.score, object_sort_key(pair.object_id)),
    )


@dataclass(frozen=True)
class IssuedCard:
    object_id: str
    issued_at: datetime
    release_at: datetime | None  # first event start in the window, if any


def is_open(x: datetime, card: IssuedCard, days: int = WINDOW_DAYS) -> bool:
    """``sensor_failure_release.is_open``: inside the window and not after the release."""
    end = card.issued_at + timedelta(days=days)
    released = card.release_at if card.release_at is not None else end
    return card.issued_at <= x < end and x <= released


def held_objects(t: datetime, cards: Iterable[IssuedCard], days: int = WINDOW_DAYS) -> set[str]:
    return {card.object_id for card in cards if is_open(t, card, days)}


def select_new(
    t: datetime,
    pairs: Sequence[PairAtCutoff],
    open_cards: Iterable[IssuedCard],
    *,
    k: int = MAX_OPEN,
    days: int = WINDOW_DAYS,
) -> list[tuple[PairAtCutoff, int, int]]:
    """New cards of cutoff t as ``(pair, rank, ranked)``; ``rank`` is 1-based among the
    eligible pairs of the cutoff (the order of checking, not severity)."""
    if k < 1:
        raise ValueError("k must be positive")
    held = held_objects(t, open_cards, days)
    slots = k - len(held)
    ranked = rank_pairs(pairs)
    chosen = []
    for position, pair in enumerate(ranked, start=1):
        if slots <= 0 or pair.score < MIN_EVENTS_FOR_CARD:
            break  # ranked by score: every later pair has no past event either
        if pair.object_id in held:
            continue
        chosen.append((pair, position, len(ranked)))
        held.add(pair.object_id)
        slots -= 1
    return chosen


# -- «k из n» -------------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson interval rounded outwards to 4 decimals (it always contains k/n)."""
    share = k / n
    denominator = 1 + z * z / n
    centre = (share + z * z / (2 * n)) / denominator
    half = z * math.sqrt(share * (1 - share) / n + z * z / (4 * n * n)) / denominator
    return (
        max(0.0, math.floor((centre - half) * 1e4) / 1e4),
        min(1.0, math.ceil((centre + half) * 1e4) / 1e4),
    )


@dataclass(frozen=True)
class FrequencyRow:
    events_from: int
    events_to: int | None
    cards: int
    positive_cards: int
    level: str

    @property
    def share(self) -> float:
        return round(self.positive_cards / self.cards, 4)

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.positive_cards, self.cards)


def frequency_row(events_365d: int) -> FrequencyRow:
    for low, high, n, k, level in FREQUENCY_BINS:
        if low <= events_365d and (high is None or events_365d <= high):
            return FrequencyRow(low, high, n, k, level)
    raise ValueError(f"no frequency bin for {events_365d} events")


# -- incidents and recall (research definitions of v9) ---------------------------------------


def incident_heads(starts: Sequence[datetime], gap: timedelta = INCIDENT_GAP) -> list[bool]:
    """For sorted event starts of one pair: whether each event starts an incident
    (events less than ``gap`` apart are chained)."""
    heads = []
    previous = None
    for start in starts:
        heads.append(previous is None or start - previous >= gap)
        previous = start
    return heads


def fresh_incident(starts: Sequence[datetime], index: int, days: int = FRESH_DAYS) -> bool:
    """An incident is fresh when the pair had no event in the ``days`` before its start."""
    start = starts[index]
    return index == 0 or start - starts[index - 1] >= timedelta(days=days)


# -- recommendation of a card (v5) -------------------------------------------------------------


@dataclass(frozen=True)
class PriorCard:
    """A card of the same pair as the recommendation sees it: issue, status, release."""

    issued_at: datetime
    scored: bool
    release_at: datetime | None


@dataclass(frozen=True)
class Repeat:
    """``cards_30d`` — n of «n-я за 30 сут» (this card included); ``realized`` — the latest
    earlier scored card within 30 days left the list at its event by this issue."""

    cards_30d: int = 1
    realized: bool = False


@dataclass(frozen=True)
class Recommendation:
    text: str
    details: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    policy_version: str = RECOMMENDATION_POLICY_VERSION


NO_REPEAT = Repeat()


def repeat_at(issued_at: datetime, cards: Iterable[PriorCard]) -> Repeat:
    """Repeat of a card issued at ``issued_at`` among the cards of its pair (point in time:
    cards issued before it and releases not later than it; later cards are ignored)."""
    since = issued_at - timedelta(days=RECOMMENDATION_REPEAT_DAYS)
    earlier = [card for card in cards if since <= card.issued_at < issued_at]
    scored = [card for card in earlier if card.scored]
    realized = False
    if scored:
        latest = max(scored, key=lambda card: card.issued_at)
        realized = latest.release_at is not None and latest.release_at <= issued_at
    return Repeat(cards_30d=len(earlier) + 1, realized=realized)


def recommend(
    scored: bool,
    lines: Iterable[tuple[str | None, str | None]] = (),
    *,
    repeat: Repeat = NO_REPEAT,
    section_switch: bool = False,
) -> Recommendation:
    """Main text and details of a card: ``lines`` are (name, feeder kind) of the card lines,
    ``section_switch`` — the object's layout has a section switch. An abstained card gets only
    its text. ``rule_ids`` name the details in their order (audit of why the text is such)."""
    if not scored:
        return Recommendation(text=ABSTAINED_RECOMMENDATION_TEXT)
    details: list[str] = []
    rule_ids: list[str] = []
    if repeat.realized and repeat.cards_30d >= RECOMMENDATION_REPEAT_MIN_CARDS:
        details.append(RECOMMENDATION_REPEAT_TEXT.format(n=repeat.cards_30d))
        rule_ids.append("repeat_realized")
    groups: set[str] = {recommendation_group(name, kind) for name, kind in lines} - {None}
    if section_switch:
        groups.add("section")
    chosen = [group for group in RECOMMENDATION_GROUP_ORDER if group in groups]
    for group in chosen[:RECOMMENDATION_MAX_GROUPS]:
        details.append(RECOMMENDATION_GROUP_TEXT[group])
        rule_ids.append(f"group_{group}")
    return Recommendation(
        text=RECOMMENDATION_TEXT, details=tuple(details), rule_ids=tuple(rule_ids)
    )
