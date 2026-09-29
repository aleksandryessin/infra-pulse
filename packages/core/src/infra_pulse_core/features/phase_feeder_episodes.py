"""Causal, incremental detector of «Неисправен» episodes of «Состояние фазы» channels.

One implementation for research parity, the seed of the history and the runtime
worker (pure Python, no pandas). Definitions follow ``LABELS.md`` / ``sensor-failure-v1``
for the label ``connection_loss`` restricted to phase channels, all episodes (FINAL_V9):

- a record is a *candidate* when its text is «Неисправен»; *neutral* texts are
  «Неопределен», «Не определено», «Выключен», «Много неисправных устройств»,
  «Отключено устройство»; any other text is *clean* («Обесточен», «Есть питание» …);
- records of one channel at one timestamp are never ordered: the timestamp is a
  candidate when any record is, clean when it has clean records only, else neutral;
- an episode **starts** on a candidate timestamp whose previous candidate is at least
  ``Q`` (24 h) earlier *and* was followed by a clean timestamp before it (or on the first
  candidate of the channel); a channel stuck in the state stays in one episode;
- it **ends** at the first clean timestamp strictly after the start (``None`` = open);
- ``power_off_at`` is the first «Обесточен» record of the same channel within 10 min
  after the start (the ``power_off_followup`` fact of the card);
- starts of one object chain into an **event** when consecutive starts are at most
  ``W`` (10 min) apart (:func:`cluster_events`); the event is the unit of the list, the
  release and recall.

Everything is decided from records up to the moment itself, so a cutoff never sees
the future. The detector state is JSON-serializable and carried between uploads: a run
split into parts equals the run over the whole history. A record at exactly the
channel watermark is merged into that timestamp (the state before it is kept for this
purpose). A record earlier than the watermark cannot be applied incrementally: the
channel is reported in ``DetectorDelta.late_channels`` and rebuilt from its full history
(:func:`detect`).
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from itertools import groupby

DETECTOR_VERSION = "phase-feeder-episodes-all-q24h-w10m-v1"
PHASE_SENSOR_TYPE = "Состояние фазы"
CANDIDATE_STATES = frozenset({"Неисправен"})
NEUTRAL_STATES = frozenset(
    {
        "Неопределен",
        "Не определено",
        "Выключен",
        "Много неисправных устройств",
        "Отключено устройство",
    }
)
POWER_OFF_STATE = "Обесточен"
Q = timedelta(hours=24)
W = timedelta(minutes=10)
FOLLOWUP = timedelta(minutes=10)
# Europe/Moscow wall time of the source; a fixed offset (no DST since 2014).
MSK = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class Record:
    """One source record of a phase channel (text kept verbatim)."""

    channel_id: str
    event_at: datetime
    value_raw: str | None
    alarm: bool = False
    object_id: str | None = None


@dataclass(frozen=True)
class Episode:
    channel_id: str
    object_id: str | None
    start_at: datetime
    end_at: datetime | None = None
    start_alarm: bool = False
    power_off_at: datetime | None = None

    @property
    def key(self) -> tuple[str, datetime]:
        return (self.channel_id, self.start_at)


@dataclass(frozen=True)
class PhaseEvent:
    """W-cluster of episode starts of one object: the registered event."""

    event_id: str
    object_id: str
    start_at: datetime
    last_start_at: datetime
    channel_ids: tuple[str, ...]
    episode_starts: tuple[datetime, ...]

    @property
    def size(self) -> int:
        return len(self.channel_ids)


@dataclass(frozen=True)
class Flags:
    candidate: bool = False
    clean: bool = False
    alarm: bool = False
    power_off: bool = False

    def merge(self, other: Flags) -> Flags:
        return Flags(
            self.candidate or other.candidate,
            self.clean or other.clean,
            self.alarm or other.alarm,
            self.power_off or other.power_off,
        )

    @property
    def kind(self) -> str:
        if self.candidate:
            return "candidate"
        return "clean" if self.clean else "neutral"


@dataclass(frozen=True)
class _Core:
    """The part of a channel state that a timestamp changes."""

    last_candidate_at: datetime | None = None
    clean_after_candidate: bool = False
    latest: Episode | None = None


@dataclass
class ChannelState:
    """Carried state of one channel; ``pre`` is the state before ``last_ts``."""

    object_id: str | None = None
    first_seen: datetime | None = None
    last_ts: datetime | None = None
    last_flags: Flags = field(default_factory=Flags)
    core: _Core = field(default_factory=_Core)
    pre: _Core = field(default_factory=_Core)

    def to_json(self) -> dict:
        return {
            "object_id": self.object_id,
            "first_seen": _iso(self.first_seen),
            "last_ts": _iso(self.last_ts),
            "last_flags": [
                self.last_flags.candidate,
                self.last_flags.clean,
                self.last_flags.alarm,
                self.last_flags.power_off,
            ],
            "core": _core_json(self.core),
            "pre": _core_json(self.pre),
        }

    @classmethod
    def from_json(cls, data: dict) -> ChannelState:
        return cls(
            object_id=data.get("object_id"),
            first_seen=_when(data.get("first_seen")),
            last_ts=_when(data.get("last_ts")),
            last_flags=Flags(*data.get("last_flags", [False] * 4)),
            core=_core_from(data.get("core") or {}),
            pre=_core_from(data.get("pre") or {}),
        )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _when(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _core_json(core: _Core) -> dict:
    latest = core.latest
    return {
        "last_candidate_at": _iso(core.last_candidate_at),
        "clean_after_candidate": core.clean_after_candidate,
        "latest": None
        if latest is None
        else {
            "object_id": latest.object_id,
            "start_at": _iso(latest.start_at),
            "end_at": _iso(latest.end_at),
            "start_alarm": latest.start_alarm,
            "power_off_at": _iso(latest.power_off_at),
        },
    }


def _core_from(data: dict, channel_id: str = "") -> _Core:
    latest = data.get("latest")
    return _Core(
        last_candidate_at=_when(data.get("last_candidate_at")),
        clean_after_candidate=bool(data.get("clean_after_candidate")),
        latest=None
        if latest is None
        else Episode(
            channel_id=channel_id,
            object_id=latest.get("object_id"),
            start_at=_when(latest["start_at"]),
            end_at=_when(latest.get("end_at")),
            start_alarm=bool(latest.get("start_alarm")),
            power_off_at=_when(latest.get("power_off_at")),
        ),
    )


@dataclass
class DetectorDelta:
    """Changes of one :meth:`PhaseEpisodeDetector.process` call.

    ``episodes`` holds the current version of every new or changed episode (an end set
    or retracted by a merged timestamp, a «Обесточен» follow-up found); ``candidates``
    the candidate timestamps seen (the Q shadow at a cutoff); ``late_channels`` channels
    with records before their watermark (rebuild them from full history).
    """

    episodes: dict[tuple[str, datetime], Episode] = field(default_factory=dict)
    candidates: set[tuple[str, datetime]] = field(default_factory=set)
    late_channels: set[str] = field(default_factory=set)
    records: int = 0


def classify(value_raw: str | None) -> Flags:
    return Flags(
        candidate=value_raw in CANDIDATE_STATES,
        clean=value_raw is not None
        and value_raw not in CANDIDATE_STATES
        and value_raw not in NEUTRAL_STATES,
        power_off=value_raw == POWER_OFF_STATE,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("record time must be timezone-aware")
    return value


class PhaseEpisodeDetector:
    """Incremental detector; ``states`` persist between calls (see the module docstring)."""

    def __init__(self, states: dict[str, ChannelState] | None = None) -> None:
        self.states: dict[str, ChannelState] = states or {}

    def process(self, records: Iterable[Record]) -> DetectorDelta:
        delta = DetectorDelta()
        ordered = sorted(records, key=lambda record: (record.channel_id, _aware(record.event_at)))
        for channel_id, channel_records in groupby(ordered, key=lambda record: record.channel_id):
            items = list(channel_records)
            delta.records += len(items)
            state = self.states.get(channel_id)
            if state is not None and state.last_ts is not None:
                if items[0].event_at < state.last_ts:
                    delta.late_channels.add(channel_id)
                    continue
            if state is None:
                state = self.states[channel_id] = ChannelState()
            for event_at, group in groupby(items, key=lambda record: record.event_at):
                flags = Flags()
                for row in group:
                    row_flags = classify(row.value_raw)
                    if row.alarm and row_flags.candidate:
                        row_flags = replace(row_flags, alarm=True)
                    flags = flags.merge(row_flags)
                    if row.object_id is not None:
                        state.object_id = row.object_id
                if state.first_seen is None:
                    state.first_seen = event_at
                self._apply(channel_id, state, event_at, flags, delta)
        return delta

    def _apply(
        self, channel_id: str, state: ChannelState, at: datetime, flags: Flags, delta: DetectorDelta
    ) -> None:
        before = state.core.latest
        if state.last_ts == at:
            merged = state.last_flags.merge(flags)
            if merged == state.last_flags:
                return
            # The timestamp gained records: replay it from the state before it. Kinds
            # only grow (neutral < clean < candidate), so a start is never retracted;
            # an end or a follow-up set by this timestamp may be.
            state.core = state.pre
            flags = merged
        else:
            state.pre = state.core
        state.last_ts = at
        state.last_flags = flags
        core = state.core
        latest = core.latest
        if (
            latest is not None
            and flags.power_off
            and latest.power_off_at is None
            and latest.start_at <= at <= latest.start_at + FOLLOWUP
        ):
            latest = replace(latest, power_off_at=at)
        if flags.kind == "candidate":
            delta.candidates.add((channel_id, at))
            previous = core.last_candidate_at
            if previous is None or (at - previous >= Q and core.clean_after_candidate):
                latest = Episode(
                    channel_id=channel_id,
                    object_id=state.object_id,
                    start_at=at,
                    start_alarm=flags.alarm,
                    power_off_at=at if flags.power_off else None,
                )
            core = _Core(last_candidate_at=at, clean_after_candidate=False, latest=latest)
        elif flags.kind == "clean":
            if latest is not None and latest.end_at is None and at > latest.start_at:
                latest = replace(latest, end_at=at)
            core = _Core(
                last_candidate_at=core.last_candidate_at,
                clean_after_candidate=core.last_candidate_at is not None,
                latest=latest,
            )
        else:
            core = replace(core, latest=latest)
        if core.latest is not None and core.latest != before:
            delta.episodes[core.latest.key] = core.latest
        state.core = core

    # -- persistence -------------------------------------------------------------------

    def state_json(self) -> dict[str, dict]:
        return {channel: state.to_json() for channel, state in self.states.items()}

    @classmethod
    def from_state_json(cls, data: dict[str, dict]) -> PhaseEpisodeDetector:
        states = {}
        for channel, value in data.items():
            state = ChannelState.from_json(value)
            state.core = _with_channel(state.core, channel)
            state.pre = _with_channel(state.pre, channel)
            states[channel] = state
        return cls(states)


def _with_channel(core: _Core, channel_id: str) -> _Core:
    if core.latest is None:
        return core
    return replace(core, latest=replace(core.latest, channel_id=channel_id))


def detect(records: Iterable[Record]) -> tuple[list[Episode], set[tuple[str, datetime]]]:
    """Whole run: the reference for incremental equality, rebuilds and research parity."""
    delta = PhaseEpisodeDetector().process(records)
    episodes = sorted(delta.episodes.values(), key=lambda e: (e.start_at, e.channel_id))
    return episodes, delta.candidates


def cluster_events(episodes: Iterable[Episode], w: timedelta = W) -> list[PhaseEvent]:
    """Chain episode starts of each object into events (gap between starts <= ``w``).

    Starts are ordered by time, then channel (``LABELS.md``). The event ID is the object
    and its first start, so it stays stable when a later upload extends the chain.
    Episodes without an object are not events of any card.
    """
    per_object: dict[str, list[tuple[datetime, str]]] = {}
    for episode in episodes:
        if episode.object_id is None:
            continue
        per_object.setdefault(episode.object_id, []).append((episode.start_at, episode.channel_id))
    events: list[PhaseEvent] = []
    for object_id, starts in per_object.items():
        starts.sort()
        chain: list[tuple[datetime, str]] = []
        for start, channel in starts:
            if chain and start - chain[-1][0] > w:
                events.append(_event(object_id, chain))
                chain = []
            chain.append((start, channel))
        if chain:
            events.append(_event(object_id, chain))
    events.sort(key=lambda event: (event.start_at, event.object_id))
    return events


def _event(object_id: str, chain: list[tuple[datetime, str]]) -> PhaseEvent:
    first = chain[0][0]
    return PhaseEvent(
        event_id=f"{object_id}:{first.astimezone(MSK):%Y-%m-%dT%H:%M:%S}",
        object_id=object_id,
        start_at=first,
        last_start_at=chain[-1][0],
        channel_ids=tuple(channel for _, channel in chain),
        episode_starts=tuple(start for start, _ in chain),
    )


def count_in_window(sorted_times: list[datetime], start: datetime, end: datetime) -> int:
    """Number of times in ``[start, end)`` of a sorted list."""
    return bisect_left(sorted_times, end) - bisect_left(sorted_times, start)
