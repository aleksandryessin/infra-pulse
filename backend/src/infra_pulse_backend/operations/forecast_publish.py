"""Recompute and publish «обесточивание электрооборудования объекта» (ID: phase feeders)
after new observations (B2).

``recompute(connection, *, data_as_of, import_id)`` is called by the ingestion worker (B1)
inside its transaction after a journal file is loaded, and by
``backend/scripts/seed_forecast_history.py`` for the history. One call is one run:

1. **detect** — the core detector reads the new «Состояние фазы» records of the scope
   (``received_position`` after the detector watermark), carries every channel state,
   rebuilds channels that received records older than their watermark, and re-chains
   the events of the touched objects; coverage days and per-object record days follow;
2. **score** — every 00:00 MSK cutoff after the last one and not later than
   ``data_as_of``: candidate channels, causal eligibility, the 365-day static list and
   the rolling list step (``infra_pulse_core.features.incident_list``);
3. **publish** — immutable card snapshots, outcomes and window events of every card as
   known at ``data_as_of``, the lifecycle log, a new run with ``generation + 1``.

No HTTP, pandas, numpy or research imports; nothing is trained here.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from infra_pulse_backend.config import Settings
from infra_pulse_backend.storage import forecast_pg as pg
from infra_pulse_backend.storage.forecast_pg import Scope
from infra_pulse_core.contracts.forecast import (
    FEEDER_TARGET,
    TARGET_LABELS,
    ForecastCard,
    ForecastChannel,
    ForecastFact,
    ForecastFreshness,
    ForecastScore,
    ForecastVersions,
    FrequencyBucket,
)
from infra_pulse_core.features import incident_list as il
from infra_pulse_core.features.channel_names import (
    LAYOUT_VERSION,
    classify_channel,
    normalize_name,
)
from infra_pulse_core.features.phase_feeder_episodes import (
    MSK,
    PHASE_SENSOR_TYPE,
    ChannelState,
    Episode,
    PhaseEpisodeDetector,
    PhaseEvent,
    Record,
    cluster_events,
    detect,
)

HORIZON = "336h"
WINDOW = timedelta(days=il.WINDOW_DAYS)
YEAR = timedelta(days=il.SCORE_WINDOW_DAYS)
TOP_CHANNELS = 5
MIN_CHANNELS = 3
DEFAULT_SYSTEM_TYPE = "Диспетчерский контроль"


@dataclass(frozen=True)
class PublishResult:
    """What one run changed (B1 writes it into ``import_files``)."""

    generation: int
    new_card_ids: list[str]
    released_card_ids: list[str]
    run_id: str
    data_as_of: datetime
    cutoffs: int
    timings: dict[str, float] = field(default_factory=dict)
    records_read: int = 0
    late_channels: int = 0


@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    object_id: str
    name: str | None
    tag: str | None
    role: str
    feeder_kind: str | None
    picket_form: str
    picket_from: float | None
    picket_to: float | None
    picket_basis: str | None
    first_seen: datetime | None
    system_type: str | None


@dataclass(frozen=True)
class CardRow:
    card_id: str
    object_id: str | None
    status: str
    issued_at: datetime
    candidates: frozenset[str]


def floor_cutoff(moment: datetime) -> datetime:
    return il.cutoff_of(il.msk_day(moment))


def run_id_for(scope: Scope, generation: int) -> str:
    return f"phase-feeders-{scope.snapshot_id}-g{generation}"


def card_id_for(object_id: str, cutoff: datetime) -> str:
    return f"feeders-14d-{object_id}-{cutoff.astimezone(MSK):%Y%m%d}"


def resolve_scope(
    connection: psycopg.Connection,
    *,
    import_id: str | None,
    namespace_id: str | None,
    snapshot_id: str | None,
    settings: Settings | None,
) -> Scope:
    if namespace_id and snapshot_id:
        return Scope(namespace_id, snapshot_id)
    if import_id and pg.table_exists(connection, "import_files"):
        row = (
            connection.cursor(row_factory=tuple_row)
            .execute(
                "SELECT target_namespace, target_stream FROM import_files WHERE import_id = %s",
                (import_id,),
            )
            .fetchone()
        )
        if row is not None and row[0] and row[1]:
            return Scope(row[0], row[1])
    settings = settings or Settings()
    if settings.mode == "replay" and settings.replay_snapshot_id:
        return Scope(settings.replay_namespace, settings.replay_snapshot_id)
    if settings.received_stream_id:
        return Scope(settings.received_namespace, settings.received_stream_id)
    raise ValueError("forecast scope is not configured (INFRA_RECEIVED_STREAM_ID)")


# -- layout ------------------------------------------------------------------------------------


def layout_row(
    channel_id: str,
    *,
    name: str | None,
    object_id: str | None,
    sensor_type: str | None,
    system_type: str | None,
    tag: str | None,
    reference_version: str | None,
) -> dict:
    """Layout of any channel: the picket of every name, role and kind of phase channels."""
    layout = classify_channel(name)
    phase = sensor_type == PHASE_SENSOR_TYPE
    picket = layout.picket
    return {
        "channel_id": channel_id,
        "object_id": object_id,
        "sensor_type": sensor_type,
        "system_type": system_type,
        "name": name,
        "tag": tag,
        "role": layout.role if phase else None,
        "feeder_kind": layout.feeder_kind if phase else None,
        "landmark_kind": layout.landmark_kind if phase else None,
        "picket_form": picket.form,
        "picket_from": picket.picket_from,
        "picket_to": picket.picket_to,
        "picket_basis": picket.basis,
        "layout_version": LAYOUT_VERSION,
        "reference_version": reference_version,
    }


def refresh_layout_from_reference(connection: psycopg.Connection) -> str | None:
    """Rebuild the layout from the active B1 channel reference once per version."""
    if not pg.table_exists(connection, "ref_channels"):
        return None
    cursor = connection.cursor(row_factory=tuple_row)
    active = cursor.execute(
        """SELECT version_id FROM ref_versions WHERE kind = 'channels'
           ORDER BY activation_seq DESC LIMIT 1"""
    ).fetchone()
    if active is None or active[0] in pg.layout_reference_versions(connection):
        return None if active is None else active[0]
    rows = cursor.execute(
        """SELECT DISTINCT ON (channel_id) channel_id, name, object_id, sensor_type,
                  system_type, tag
           FROM ref_channels WHERE version_id = %s ORDER BY channel_id, line_no""",
        (active[0],),
    ).fetchall()
    pg.upsert_layout(
        connection,
        [
            layout_row(
                channel_id,
                name=normalize_name(name),
                object_id=object_id,
                sensor_type=sensor_type,
                system_type=system_type,
                tag=tag,
                reference_version=active[0],
            )
            for channel_id, name, object_id, sensor_type, system_type, tag in rows
        ],
    )
    objects = cursor.execute(
        """SELECT version_id FROM ref_versions WHERE kind = 'objects'
           ORDER BY activation_seq DESC LIMIT 1"""
    ).fetchone()
    if objects is not None:
        names = cursor.execute(
            """SELECT DISTINCT ON (object_id) object_id, name FROM ref_objects
               WHERE version_id = %s ORDER BY object_id, line_no""",
            (objects[0],),
        ).fetchall()
        pg.upsert_objects(connection, [(oid, name, objects[0]) for oid, name in names])
    return active[0]


# -- detect ------------------------------------------------------------------------------------


def _records(rows: Iterable[tuple]) -> list[Record]:
    return [
        Record(channel_id, event_at.astimezone(MSK), value_raw, bool(alarm), object_id)
        for channel_id, event_at, value_raw, alarm, object_id in rows
    ]


def detect_step(
    connection: psycopg.Connection, scope: Scope, row: pg.ScopeRow
) -> tuple[int, set[str], int, int]:
    """Run the detector over the new records. Returns (position, touched objects,
    records read, late channels)."""
    after = None if row.mode == "replay" and row.detector_position == 0 else row.detector_position
    if row.mode == "replay" and row.detector_position > 0:
        return row.detector_position, set(), 0, 0  # a replay snapshot is immutable
    rows, top = pg.read_new_phase_rows(connection, scope, after_position=after)
    pg.aggregate_new_rows(connection, scope, after_position=after)
    detector = PhaseEpisodeDetector.from_state_json(pg.read_states(connection, scope))
    delta = detector.process(_records(rows))
    touched = {episode.object_id for episode in delta.episodes.values() if episode.object_id}
    first_run = not detector.states or row.detector_position == 0
    if first_run and not delta.late_channels:
        pg.copy_episodes(connection, scope, delta.episodes.values())
    else:
        pg.upsert_episodes(connection, scope, delta.episodes.values())
    pg.insert_candidates(connection, scope, delta.candidates)
    changed = {channel for channel, _ in delta.episodes} | {c for c, _ in delta.candidates}
    changed |= {record[0] for record in rows}
    for channel_id in sorted(delta.late_channels):
        history = _records(pg.read_channel_rows(connection, scope, channel_id))
        rebuilt = PhaseEpisodeDetector()
        rebuilt_delta = rebuilt.process(history)
        episodes = sorted(rebuilt_delta.episodes.values(), key=lambda e: e.start_at)
        pg.replace_channel_history(
            connection, scope, channel_id, episodes, rebuilt_delta.candidates
        )
        detector.states[channel_id] = rebuilt.states.get(channel_id, ChannelState())
        touched |= {episode.object_id for episode in episodes if episode.object_id}
        state_object = detector.states[channel_id].object_id
        if state_object:
            touched.add(state_object)
        changed.add(channel_id)
    pg.write_states(
        connection,
        scope,
        {channel: detector.states[channel] for channel in changed if channel in detector.states},
    )
    if touched:
        episodes = pg.read_episodes(connection, scope, objects=touched)
        pg.replace_events(connection, scope, touched, cluster_events(episodes))
    return top, touched, len(rows), len(delta.late_channels)


# -- score and publish -------------------------------------------------------------------------


@dataclass
class World:
    """Everything a cutoff needs, loaded once per run."""

    channels: dict[str, list[ChannelInfo]]
    episodes: il.EpisodeIndex
    events: il.EventIndex
    power_off: dict[tuple[str, datetime], bool]
    coverage: dict[date, tuple[bool, bool]]
    object_days: dict[str, list[tuple[date, datetime]]]
    first_seen: dict[str, datetime]


def load_world(connection: psycopg.Connection, scope: Scope) -> World:
    layout = pg.read_layout(connection, sensor_type=None)
    states = {
        channel: ChannelState.from_json(value)
        for channel, value in pg.read_states(connection, scope).items()
    }
    channels: dict[str, list[ChannelInfo]] = {}
    phase_ids = {cid for cid, row in layout.items() if row.sensor_type == PHASE_SENSOR_TYPE}
    for channel_id in phase_ids | set(states):
        ref = layout.get(channel_id)
        state = states.get(channel_id)
        if ref is not None and ref.sensor_type not in (None, PHASE_SENSOR_TYPE):
            continue
        object_id = (ref.object_id if ref is not None else None) or (
            state.object_id if state is not None else None
        )
        if object_id is None:
            continue
        name = ref.name if ref is not None else None
        fallback = classify_channel(name)
        channels.setdefault(object_id, []).append(
            ChannelInfo(
                channel_id=channel_id,
                object_id=object_id,
                name=name,
                tag=ref.tag if ref is not None else None,
                role=(ref.role if ref is not None and ref.role else fallback.role),
                feeder_kind=(
                    ref.feeder_kind if ref is not None and ref.role else fallback.feeder_kind
                ),
                picket_form=ref.picket_form if ref is not None else fallback.picket.form,
                picket_from=ref.picket_from if ref is not None else fallback.picket.picket_from,
                picket_to=ref.picket_to if ref is not None else fallback.picket.picket_to,
                picket_basis=ref.picket_basis if ref is not None else fallback.picket.basis,
                first_seen=state.first_seen if state is not None else None,
                system_type=ref.system_type if ref is not None else None,
            )
        )
    for items in channels.values():
        items.sort(key=lambda info: info.channel_id)
    episodes = pg.read_episodes(connection, scope)
    events = pg.read_events(connection, scope)
    power_off = {(e.channel_id, e.start_at): e.power_off_at is not None for e in episodes}
    first_seen = {}
    for object_id, items in channels.items():
        seen = [info.first_seen for info in items if info.first_seen is not None]
        if seen:
            first_seen[object_id] = min(seen)
    return World(
        channels=channels,
        episodes=il.EpisodeIndex(episodes, pg.read_candidates(connection, scope)),
        events=il.EventIndex(events),
        power_off=power_off,
        coverage=pg.read_coverage(connection, scope),
        object_days=pg.read_object_days(connection, scope),
        first_seen=first_seen,
    )


def _usable(world: World, day: date) -> bool:
    covered, policy = world.coverage.get(day, (False, False))
    return covered and not policy


def _last_record_before(world: World, object_id: str, t: datetime) -> datetime | None:
    days = world.object_days.get(object_id, [])
    target = il.msk_day(t)
    best = None
    for day, last in days:
        if day >= target:
            break
        best = last
    return best if best is not None and best < t else None


def _fact(kind, text, start, end, *, number=None, at=None, unit=None) -> ForecastFact:
    return ForecastFact(
        kind=kind,
        text=text,
        period_start=start,
        period_end=end,
        value_number=number,
        value_at=at,
        unit=unit,
    )


def _channel_entry(world: World, info: ChannelInfo, t: datetime, rank: int, facts: bool):
    year_ago = t - YEAR
    episodes = world.episodes.episodes_between(info.channel_id, year_ago, t)
    last = world.episodes.last_start_before(info.channel_id, t)
    reasons = []
    if facts:
        reasons.append(
            _fact(
                "events_365d",
                f"Потерь связи линии электропитания за 365 сут: {episodes}",
                year_ago,
                t,
                number=episodes,
            )
        )
        if last is not None and last >= year_ago:
            reasons.append(
                _fact(
                    "last_connection_loss_at",
                    "Последнее начало потери связи линии электропитания",
                    year_ago,
                    t,
                    at=last,
                )
            )
    known_picket = info.picket_form != "unknown"
    return ForecastChannel(
        channel_id=info.channel_id,
        channel_name=info.name,
        engineering_tag=info.tag,
        rank_in_card=rank,
        picket_form=info.picket_form,
        picket_from=info.picket_from if known_picket else None,
        picket_to=info.picket_to if info.picket_form == "range" else None,
        picket_basis=info.picket_basis if known_picket else None,
        feeder_kind=info.feeder_kind if info.role == "feeder" else None,
        reason_facts=reasons,
    )


def _top_channels(world: World, infos: list[ChannelInfo], t: datetime) -> list[ChannelInfo]:
    """3–5 candidate feeders with most episodes in 365 days (latest episode first on ties)."""
    year_ago = t - YEAR

    def key(info: ChannelInfo):
        count = world.episodes.episodes_between(info.channel_id, year_ago, t)
        last = world.episodes.last_start_before(info.channel_id, t)
        return (-count, -(last.timestamp() if last else 0), info.name or "", info.channel_id)

    ranked = sorted(infos, key=key)
    with_events = [
        info for info in ranked if world.episodes.episodes_between(info.channel_id, year_ago, t)
    ]
    chosen = with_events[:TOP_CHANNELS]
    if len(chosen) < MIN_CHANNELS:
        chosen += [info for info in ranked if info not in chosen][: MIN_CHANNELS - len(chosen)]
    return chosen


def _coverage(world: World, object_id: str, t: datetime) -> tuple[str, int]:
    first = world.first_seen.get(object_id)
    days = [il.msk_day(t) - timedelta(days=offset) for offset in range(1, 366)]
    if first is None or first > t - YEAR:
        history = 0 if first is None else max(0, (il.msk_day(t) - il.msk_day(first)).days)
        return "insufficient", min(history, il.SCORE_WINDOW_DAYS - 1)
    usable = sum(1 for day in days if _usable(world, day))
    return ("complete" if usable == len(days) else "partial"), usable


def _power_off_share(world: World, object_id: str, t: datetime) -> tuple[int, int]:
    """Past events (fully observed 10 min after the start) followed by «Обесточен»."""
    events = world.events.between(object_id, t - YEAR, t)
    done = [event for event in events if event.last_start_at + timedelta(minutes=10) <= t]
    hits = sum(
        1
        for event in done
        if any(
            world.power_off.get((channel, start), False)
            for channel, start in zip(event.channel_ids, event.episode_starts, strict=True)
        )
    )
    return hits, len(done)


def _versions(run_id: str, scored: bool) -> ForecastVersions:
    return ForecastVersions(
        scorer="static_list",
        model_version=il.SCORE_VERSION,
        feature_version=f"{il.FEATURE_VERSION}+{LAYOUT_VERSION}",
        label_version=il.LABEL_VERSION,
        policy_version=il.LIST_POLICY_VERSION,
        recurrence_rule_version=il.RECURRENCE_RULE_VERSION if scored else None,
        release_id=f"{il.CONFIG_VERSION}/{il.CONFIGURATION}",
        run_id=run_id,
    )


def build_card(
    world: World,
    *,
    mode: str,
    object_id: str,
    t: datetime,
    data_as_of: datetime,
    run_id: str,
    candidates: list[ChannelInfo],
    active: list[str],
    pair: il.PairAtCutoff | None,
    rank: int | None = None,
    ranked: int | None = None,
) -> ForecastCard:
    year_ago = t - YEAR
    feeders = [info for info in candidates if info.role == "feeder"] or candidates
    top = _top_channels(world, feeders, t)
    coverage, usable = _coverage(world, object_id, t)
    system_types = {info.system_type for info in candidates if info.system_type}
    common = {
        "id": card_id_for(object_id, t),
        "mode": mode,
        "target_spec_id": FEEDER_TARGET,
        "target_label": TARGET_LABELS[FEEDER_TARGET],
        "object_id": object_id,
        "sensor_type": PHASE_SENSOR_TYPE,
        "system_type": min(system_types) if system_types else DEFAULT_SYSTEM_TYPE,
        "horizon": HORIZON,
        "issued_at": t,
        "window_start": t,
        "window_end": t + WINDOW,
        "published_at": t,
        "freshness": ForecastFreshness(
            data_as_of=min(data_as_of, t),
            last_object_record_at=_last_record_before(world, object_id, t),
            lookback_days=il.SCORE_WINDOW_DAYS,
            history_days_available=usable,
            coverage=coverage,
        ),
        "channels_total": len(feeders),
        "active_episode_channel_ids": sorted(active),
    }
    if pair is None:
        history = usable
        return ForecastCard(
            **common,
            status="abstained",
            abstention_reason="insufficient_history",
            abstention_detail="Прогноз не выдан: история объекта меньше 365 сут.",
            score=None,
            risk_level="unknown",
            channels=[_channel_entry(world, info, t, i, False) for i, info in enumerate(top, 1)],
            facts=[
                _fact(
                    "history_coverage",
                    f"История объекта: {history} сут из 365 требуемых",
                    year_ago,
                    t,
                    number=round(history / il.SCORE_WINDOW_DAYS, 4),
                    unit="share",
                )
            ],
            versions=_versions(run_id, scored=False),
        )
    row = il.frequency_row(pair.score)
    low, high = row.interval
    frequency = FrequencyBucket(
        table_version=il.FREQUENCY_TABLE_VERSION,
        events_from=row.events_from,
        events_to=row.events_to,
        cards=row.cards,
        positive_cards=row.positive_cards,
        share=row.share,
        wilson_low=low,
        wilson_high=high,
        period_start=il.FREQUENCY_PERIOD[0],
        period_end=il.FREQUENCY_PERIOD[1],
        level=row.level,
        source_report=il.FREQUENCY_SOURCE,
    )
    score = ForecastScore(
        kind="frequency_share",
        label="доля k из n",
        value=row.share,
        rank=rank,
        ranked_cards=ranked,
        card_budget=il.MAX_OPEN,
        frequency=frequency,
    )
    facts = [
        _fact(
            "events_365d",
            f"Событий потери связи линий электропитания объекта за 365 сут: {pair.score}",
            year_ago,
            t,
            number=pair.score,
        )
    ]
    hits, done = _power_off_share(world, object_id, t)
    if done:
        facts.append(
            _fact(
                "power_off_followup",
                "После потери связи линия электропитания «Обесточен» в течение 10 мин: "
                f"{hits} из {done}",
                year_ago,
                t,
                number=round(hits / done, 4),
                unit="share",
            )
        )
    if coverage == "partial":
        facts.append(
            _fact(
                "history_coverage",
                f"Дней с данными за 365 сут: {usable} из 365",
                year_ago,
                t,
                number=round(usable / il.SCORE_WINDOW_DAYS, 4),
                unit="share",
            )
        )
    return ForecastCard(
        **common,
        status="scored",
        score=score,
        risk_level=score.level,
        channels=[_channel_entry(world, info, t, i, True) for i, info in enumerate(top, 1)],
        facts=facts,
        recurrence=world.events.recurrence(object_id, t),
        versions=_versions(run_id, scored=True),
    )


@dataclass
class CutoffOutcome:
    cards: list[tuple[ForecastCard, frozenset[str]]]
    ranked: int
    held: int
    abstained: int


def score_cutoff(
    world: World,
    *,
    mode: str,
    t: datetime,
    data_as_of: datetime,
    run_id: str,
    issued: list[CardRow],
    releases: dict[str, datetime | None],
) -> CutoffOutcome:
    """One cutoff: pairs, eligibility, list step and card snapshots."""
    lookback_ok = _usable(world, il.msk_day(t) - timedelta(days=1))
    pairs: list[il.PairAtCutoff] = []
    per_object: dict[str, tuple[list[ChannelInfo], list[str]]] = {}
    abstain: list[str] = []
    for object_id, infos in sorted(world.channels.items()):
        known, candidates, active = [], [], []
        for info in infos:
            status = il.channel_at_cutoff(
                info.channel_id,
                t,
                first_seen=info.first_seen,
                last_candidate_before=world.episodes.last_candidate_before(info.channel_id, t),
                active_episode=world.episodes.active_at(info.channel_id, t),
            )
            if not status.known:
                continue
            known.append(info)
            if status.candidate:
                candidates.append(info)
            elif status.active_episode and info.role == "feeder":
                active.append(info.channel_id)
        if not known:
            continue
        per_object[object_id] = (candidates, active)
        first = world.first_seen.get(object_id)
        if first is None or first > t - YEAR:
            abstain.append(object_id)
            continue
        reason = il.eligibility_reason(
            t, candidate_channels=len(candidates), lookback_covered=lookback_ok
        )
        pairs.append(
            il.PairAtCutoff(
                object_id=object_id,
                score=world.events.score(object_id, t),
                eligible=reason is None,
                candidate_channels=frozenset(info.channel_id for info in candidates),
                reason=reason,
            )
        )
    open_cards = [
        il.IssuedCard(card.object_id, card.issued_at, releases.get(card.card_id))
        for card in issued
        if card.status == "scored" and card.issued_at <= t < card.issued_at + WINDOW
    ]
    held = len(il.held_objects(t, open_cards))
    chosen = il.select_new(t, pairs, open_cards)
    cards = []
    for pair, rank, ranked in chosen:
        candidates, active = per_object[pair.object_id]
        card = build_card(
            world,
            mode=mode,
            object_id=pair.object_id,
            t=t,
            data_as_of=data_as_of,
            run_id=run_id,
            candidates=candidates,
            active=active,
            pair=pair,
            rank=rank,
            ranked=ranked,
        )
        cards.append((card, pair.candidate_channels))
    recent_abstained = {
        card.object_id
        for card in issued
        if card.status == "abstained" and card.issued_at <= t < card.issued_at + WINDOW
    }
    abstained = 0
    for object_id in abstain:
        if object_id in recent_abstained:
            continue
        candidates, active = per_object[object_id]
        card = build_card(
            world,
            mode=mode,
            object_id=object_id,
            t=t,
            data_as_of=data_as_of,
            run_id=run_id,
            candidates=candidates,
            active=active,
            pair=None,
        )
        cards.append((card, frozenset(info.channel_id for info in candidates)))
        abstained += 1
    return CutoffOutcome(
        cards=cards, ranked=len(il.rank_pairs(pairs)), held=held, abstained=abstained
    )


@dataclass
class Outcome:
    status: str
    release_at: datetime | None
    resolved_at: datetime | None
    first_event: PhaseEvent | None
    event_channel_ids: list[str]
    unknown_reason: str | None
    other_events: int | None
    window_events: list[tuple[PhaseEvent, bool, bool]]


def card_outcome(world: World, card: CardRow, data_as_of: datetime) -> Outcome:
    """Outcome and window events of a card as known at ``data_as_of``."""
    t, end = card.issued_at, card.issued_at + WINDOW
    if card.object_id is None:
        return Outcome("pending", None, None, None, [], None, None, [])
    events = [
        event
        for event in world.events.between(card.object_id, t, end)
        if event.start_at <= data_as_of
    ]
    first = next((e for e in events if card.candidates.intersection(e.channel_ids)), None)
    release = first.start_at if first is not None and card.status == "scored" else None
    window = [
        (
            event,
            release is None or event.start_at <= release,
            bool(card.candidates.intersection(event.channel_ids)),
        )
        for event in events
    ]
    closed = data_as_of >= end
    others = sum(1 for _, _, member in window if not member)
    channels = sorted(card.candidates.intersection(first.channel_ids)) if first else []
    if card.status == "scored":
        if first is not None:
            return Outcome("realized", release, release, first, channels, None, others, window)
        if not closed:
            return Outcome("pending", None, None, None, [], None, None, window)
        days = [il.msk_day(t) + timedelta(days=offset) for offset in range(il.WINDOW_DAYS)]
        missing = [day for day in days if not world.coverage.get(day, (False, False))[0]]
        policy = [day for day in days if world.coverage.get(day, (False, False))[1]]
        if missing or policy:
            reason = "source_coverage" if missing else "policy_day"
            return Outcome("unknown", None, end, None, [], reason, None, window)
        return Outcome("not_realized", None, end, None, [], None, others, window)
    if not closed:
        return Outcome("pending", None, None, None, [], None, None, window)
    if first is not None:
        return Outcome("event_without_forecast", None, end, first, channels, None, others, window)
    return Outcome("no_event_without_forecast", None, end, None, [], None, others, window)


def _load_cards(connection: psycopg.Connection, scope: Scope) -> list[CardRow]:
    rows = (
        connection.cursor(row_factory=tuple_row)
        .execute(
            """SELECT card_id, object_id, status, issued_at, candidate_channel_ids
               FROM forecast_cards WHERE namespace_id = %s AND snapshot_id = %s
               ORDER BY issued_at, card_id""",
            scope.key,
        )
        .fetchall()
    )
    return [
        CardRow(cid, oid, status, issued, frozenset(cands))
        for cid, oid, status, issued, cands in rows
    ]


def _load_outcomes(connection: psycopg.Connection, scope: Scope) -> dict[str, tuple]:
    rows = connection.cursor(row_factory=tuple_row).execute(
        """SELECT card_id, status, release_at, resolved_at, first_event_id,
                  event_channel_ids, unknown_reason, other_events_on_object_count
           FROM forecast_card_outcomes WHERE namespace_id = %s AND snapshot_id = %s""",
        scope.key,
    )
    return {row[0]: row[1:] for row in rows}


def _write_outcomes(
    connection: psycopg.Connection,
    scope: Scope,
    generation: int,
    cards: list[CardRow],
    outcomes: dict[str, Outcome],
    previous: dict[str, tuple],
) -> list[CardRow]:
    """Upsert changed outcomes and window events; return the cards whose outcome changed."""
    changed = []
    for card in cards:
        o = outcomes[card.card_id]
        row = (
            o.status,
            o.release_at,
            o.resolved_at,
            o.first_event.event_id if o.first_event else None,
            o.event_channel_ids,
            o.unknown_reason,
            o.other_events,
        )
        if previous.get(card.card_id) == row:
            continue
        changed.append(card)
    cursor = connection.cursor(row_factory=tuple_row)
    ids = [card.card_id for card in changed]
    if ids:
        cursor.execute(
            """DELETE FROM forecast_card_window_events
               WHERE namespace_id = %s AND snapshot_id = %s AND card_id = ANY(%s)""",
            (*scope.key, ids),
        )
    with connection.cursor() as writer:
        writer.executemany(
            """INSERT INTO forecast_card_outcomes
                 (namespace_id, snapshot_id, card_id, status, release_at, resolved_at,
                  first_event_id, first_event_at, event_channel_ids, event_cluster_size,
                  unknown_reason, other_events_on_object_count, generation)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (namespace_id, snapshot_id, card_id) DO UPDATE SET
                 status = EXCLUDED.status, release_at = EXCLUDED.release_at,
                 resolved_at = EXCLUDED.resolved_at, first_event_id = EXCLUDED.first_event_id,
                 first_event_at = EXCLUDED.first_event_at,
                 event_channel_ids = EXCLUDED.event_channel_ids,
                 event_cluster_size = EXCLUDED.event_cluster_size,
                 unknown_reason = EXCLUDED.unknown_reason,
                 other_events_on_object_count = EXCLUDED.other_events_on_object_count,
                 generation = EXCLUDED.generation""",
            [
                (
                    *scope.key,
                    card.card_id,
                    (o := outcomes[card.card_id]).status,
                    o.release_at,
                    o.resolved_at,
                    o.first_event.event_id if o.first_event else None,
                    o.first_event.start_at if o.first_event else None,
                    Jsonb(o.event_channel_ids),
                    o.first_event.size if o.first_event else None,
                    o.unknown_reason,
                    o.other_events,
                    generation,
                )
                for card in changed
            ],
        )
        writer.executemany(
            """INSERT INTO forecast_card_window_events
                 (namespace_id, snapshot_id, card_id, event_id, started_at, channel_ids,
                  cluster_size, while_open, candidate_member)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    *scope.key,
                    card.card_id,
                    event.event_id,
                    event.start_at,
                    Jsonb(list(event.channel_ids)),
                    event.size,
                    while_open,
                    member,
                )
                for card in changed
                for event, while_open, member in outcomes[card.card_id].window_events
            ],
        )
        log = []
        for card in changed:
            o = outcomes[card.card_id]
            if o.release_at is not None:
                log.append((*scope.key, card.card_id, "released", o.release_at, generation))
            if o.resolved_at is not None:
                log.append((*scope.key, card.card_id, "resolved", o.resolved_at, generation))
        writer.executemany(
            """INSERT INTO forecast_card_log (namespace_id, snapshot_id, card_id, kind, at,
                 generation)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (namespace_id, snapshot_id, card_id, kind) DO UPDATE SET
                 at = EXCLUDED.at, generation = EXCLUDED.generation""",
            log,
        )
    return changed


def _insert_cards(
    connection: psycopg.Connection,
    scope: Scope,
    generation: int,
    run_id: str,
    start_position: int,
    cards: list[tuple[ForecastCard, frozenset[str]]],
) -> None:
    with connection.cursor() as writer:
        writer.executemany(
            """INSERT INTO forecast_cards
                 (namespace_id, snapshot_id, card_id, journal_position, object_id,
                  target_spec_id, horizon, status, issued_at, window_end, published_at,
                  generation, run_id, candidate_channel_ids, card)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    *scope.key,
                    card.id,
                    start_position + index,
                    card.object_id,
                    card.target_spec_id,
                    card.horizon,
                    card.status,
                    card.issued_at,
                    card.window_end,
                    card.published_at,
                    generation,
                    run_id,
                    Jsonb(sorted(candidates)),
                    Jsonb(card.model_dump(mode="json")),
                )
                for index, (card, candidates) in enumerate(cards)
            ],
        )
        writer.executemany(
            """INSERT INTO forecast_card_log (namespace_id, snapshot_id, card_id, kind, at,
                 generation)
               VALUES (%s, %s, %s, 'issued', %s, %s) ON CONFLICT DO NOTHING""",
            [(*scope.key, card.id, card.issued_at, generation) for card, _ in cards],
        )


def recompute(
    connection: psycopg.Connection,
    *,
    data_as_of: datetime,
    import_id: str | None = None,
    namespace_id: str | None = None,
    snapshot_id: str | None = None,
    list_from: date | None = None,
    settings: Settings | None = None,
) -> PublishResult:
    """One transactional run: detect, score new cutoffs, publish ``generation + 1``."""
    if data_as_of.tzinfo is None or data_as_of.utcoffset() is None:
        raise ValueError("data_as_of must be timezone-aware")
    timings: dict[str, float] = {}
    with connection.transaction():
        scope = resolve_scope(
            connection,
            import_id=import_id,
            namespace_id=namespace_id,
            snapshot_id=snapshot_id,
            settings=settings,
        )
        mode = pg.scope_mode(connection, scope) or "received"
        row = pg.lock_scope(connection, scope, mode)
        if list_from is not None and row.list_from is None:
            pg.update_scope(connection, scope, list_from=list_from)
            row.list_from = list_from
        started = time.perf_counter()
        refresh_layout_from_reference(connection)
        position, _touched, records, late = detect_step(connection, scope, row)
        timings["detect"] = time.perf_counter() - started

        started = time.perf_counter()
        generation = row.generation + 1
        run_id = run_id_for(scope, generation)
        effective = max(data_as_of, row.data_as_of) if row.data_as_of else data_as_of
        world = load_world(connection, scope)
        issued = _load_cards(connection, scope)
        previous_outcomes = _load_outcomes(connection, scope)
        releases = {card_id: values[1] for card_id, values in previous_outcomes.items()}
        # Releases known now (events up to data_as_of) decide which places are held.
        for card in issued:
            outcome = card_outcome(world, card, effective)
            releases[card.card_id] = outcome.release_at
        if row.last_cutoff is not None:
            first_cutoff = row.last_cutoff + timedelta(days=1)
        elif row.list_from is not None:
            first_cutoff = il.cutoff_of(row.list_from)
        else:
            seen = [value for value in world.first_seen.values()]
            first_cutoff = floor_cutoff(min(seen)) + timedelta(days=1) if seen else None
        new_cards: list[tuple[ForecastCard, frozenset[str]]] = []
        cutoff_rows = []
        t = first_cutoff
        last_cutoff = row.last_cutoff
        while t is not None and t <= effective:
            outcome = score_cutoff(
                world,
                mode=mode,
                t=t,
                data_as_of=effective,
                run_id=run_id,
                issued=issued,
                releases=releases,
            )
            for card, candidates in outcome.cards:
                entry = CardRow(card.id, card.object_id, card.status, card.issued_at, candidates)
                issued.append(entry)
                releases[card.id] = card_outcome(world, entry, effective).release_at
            new_cards.extend(outcome.cards)
            scored_new = sum(1 for card, _ in outcome.cards if card.status == "scored")
            cutoff_rows.append(
                (
                    *scope.key,
                    t,
                    generation,
                    run_id,
                    outcome.ranked,
                    outcome.held,
                    scored_new,
                    outcome.abstained,
                )
            )
            last_cutoff = t
            t = t + timedelta(days=1)
        timings["score"] = time.perf_counter() - started

        started = time.perf_counter()
        _insert_cards(connection, scope, generation, run_id, row.next_journal_position, new_cards)
        with connection.cursor() as writer:
            writer.executemany(
                """INSERT INTO forecast_cutoffs (namespace_id, snapshot_id, cutoff_at, generation,
                     run_id, ranked, held, new_cards, abstained)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                cutoff_rows,
            )
        outcomes = {card.card_id: card_outcome(world, card, effective) for card in issued}
        _write_outcomes(connection, scope, generation, issued, outcomes, previous_outcomes)
        previous_as_of = row.data_as_of
        released = sorted(
            card.card_id
            for card in issued
            if outcomes[card.card_id].release_at is not None
            and (previous_as_of is None or outcomes[card.card_id].release_at > previous_as_of)
        )
        new_ids = [card.id for card, _ in new_cards]
        timings["publish"] = time.perf_counter() - started
        connection.cursor(row_factory=tuple_row).execute(
            """INSERT INTO forecast_runs (namespace_id, snapshot_id, generation, run_id,
                 import_id, data_as_of, first_cutoff, last_cutoff, cutoffs, new_card_ids,
                 released_card_ids, records_read, late_channels, timings, versions)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                *scope.key,
                generation,
                run_id,
                import_id,
                effective,
                cutoff_rows[0][2] if cutoff_rows else None,
                cutoff_rows[-1][2] if cutoff_rows else None,
                len(cutoff_rows),
                Jsonb(new_ids),
                Jsonb(released),
                records,
                late,
                Jsonb({key: round(value, 4) for key, value in timings.items()}),
                Jsonb(
                    {
                        "config": il.CONFIG_VERSION,
                        "configuration": il.CONFIGURATION,
                        "detector": il.LABEL_VERSION,
                        "score": il.SCORE_VERSION,
                        "list_policy": il.LIST_POLICY_VERSION,
                        "frequency_table": il.FREQUENCY_TABLE_VERSION,
                        "recurrence": il.RECURRENCE_RULE_VERSION,
                        "layout": LAYOUT_VERSION,
                        "recommendation": il.RECOMMENDATION_POLICY_VERSION,
                    }
                ),
            ),
        )
        pg.update_scope(
            connection,
            scope,
            generation=generation,
            detector_position=max(position, 1) if mode == "replay" else position,
            last_cutoff=last_cutoff,
            data_as_of=effective,
            next_journal_position=row.next_journal_position + len(new_cards),
        )
    return PublishResult(
        generation=generation,
        new_card_ids=new_ids,
        released_card_ids=released,
        run_id=run_id,
        data_as_of=effective,
        cutoffs=len(cutoff_rows),
        timings=timings,
        records_read=records,
        late_channels=late,
    )


def detect_history(records: Iterable[Record]) -> tuple[list[Episode], list[PhaseEvent]]:
    """Whole-history detection (tests and parity): episodes and their events."""
    episodes, _ = detect(records)
    return episodes, cluster_events(episodes)
