"""Forecast reads from PostgreSQL (replay/received). **Owned by package B2.**

Reads the tables of migration 0013 written by ``operations.forecast_publish.recompute``
for the scope of the settings (``replay_namespace``/``replay_snapshot_id`` or
``received_namespace``/``received_stream_id``). ``as_of`` defaults to ``data_as_of`` of
the latest run and is never later than it; nothing after ``as_of`` is shown: cards are
visible from their cutoff, a release from its event, an outcome from its resolution.

The stored outcome is the one known at the scope's ``data_as_of``; a view at an earlier
``as_of`` derives the state at that moment from it (pending until resolved). No pandas,
numpy or research imports (HTTP runtime).

A journal entry carries the newest dispatcher decision (B3, migration 0014) and check
result (C0.4, migration 0019) of its card. They are records on the wall clock, not on
the data time of ``as_of``: the journal reads them up to ``records_as_of`` — the read
itself, or the requested ``as_of`` when one is given (a view «as it was at that
moment»). ``as_of`` is still clamped to ``data_as_of`` for cards and outcomes only. On a
historical scope (the stand's data end 30.06.2026) every decision is taken after the
data and is shown all the same; the card snapshot, its outcome and the counters and
quality metrics do not depend on decisions. The journal cursor pins ``records_as_of``,
so «без решения» pages do not shift when a decision is saved between them. A decision
older than the card's publication (impossible through the API) is skipped.

Reads bound to the publication (the cards with their outcomes, the list, the journal
counters, the schemes) are cached in the process by the publication their transaction
sees (``api/read_cache.py``, OPS-01): a new generation is read at once, decisions and
check results are read on every request. The card JSON is parsed only for the cards a
response returns; the list reads risk and rank only for the cards it selects.

The recommendation (``incident_list.recommend``, policy v5) is assembled at read time from the
issued card, the earlier cards of its pair with their releases (not later than its issue) and
the section switch of the object's layout, so a new policy needs no republication.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from infra_pulse_backend.api.read_cache import ReadCache
from infra_pulse_backend.config import Settings
from infra_pulse_backend.storage import check_results_pg, decisions_pg
from infra_pulse_backend.storage import forecast_pg as pg
from infra_pulse_backend.storage.forecast_pg import Scope
from infra_pulse_core.contracts.forecast import (
    FEEDER_TARGET,
    ForecastCard,
    ForecastCardView,
    ForecastCheckResult,
    ForecastDecisionSummary,
    ForecastJournalCounts,
    ForecastJournalCountsRow,
    ForecastJournalEntry,
    ForecastJournalList,
    ForecastList,
    ForecastOutcome,
    ForecastQualityRow,
    ForecastQualitySummary,
    ForecastRecommendation,
    ForecastState,
    ForecastWindowEvent,
    Horizon,
    HorizonState,
    ListPolicy,
    ListState,
    OutcomeStatus,
    PublishedRun,
    RatioMetric,
    RecurringPlace,
    RecurringPlaceList,
    RiskLevel,
    TargetSpecId,
)
from infra_pulse_core.contracts.scheme import (
    ObjectScheme,
    ObjectSchemeList,
    ObjectSchemeSummary,
    SchemeAlarm,
    SchemeFeeder,
    SchemeLandmark,
    SchemeNamedLink,
    SchemePicket,
    SchemeReleasedCard,
)
from infra_pulse_core.features import incident_list as il
from infra_pulse_core.features.channel_names import parse_named_link
from infra_pulse_core.features.phase_feeder_episodes import PHASE_SENSOR_TYPE

HORIZON: Horizon = "336h"
WINDOW = timedelta(days=il.WINDOW_DAYS)
LIST_POLICY = ListPolicy(
    kind="rolling_release",
    max_open=il.MAX_OPEN,
    window_days=il.WINDOW_DAYS,
    release_on="first_event",
    policy_version=il.LIST_POLICY_VERSION,
)
ALARMS_PER_FEEDER = 20
# Journal cursors carry the records bound as microseconds since the epoch.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# B-3 (F6): a later record with one of these texts returns the channel to normal. Only
# «Норма»: «Обесточен» after «Неисправен» is not a return to normal.
NORMAL_VALUES = ("норма",)


class ForecastNotImplemented(RuntimeError):
    """Database-backed forecast reads are not available (no DSN, scope or publication)."""


class ForecastCursorError(ValueError):
    """Cursor is malformed or outside the committed range (422)."""


class StaleForecastCursor(ForecastCursorError):
    """Cursor belongs to an older publication (409)."""


@dataclass(frozen=True)
class CardLight:
    card_id: str
    position: int
    object_id: str | None
    target_spec_id: str
    horizon: str
    status: str
    issued_at: datetime
    window_end: datetime
    outcome_status: str | None
    release_at: datetime | None
    resolved_at: datetime | None
    published_at: datetime

    def released(self, as_of: datetime) -> bool:
        return self.status == "scored" and self.release_at is not None and self.release_at <= as_of

    def list_state(self, as_of: datetime) -> ListState | None:
        if self.status != "scored":
            return None
        if self.released(as_of):
            return "released"
        return "expired" if as_of >= self.window_end else "open"

    def outcome_at(self, as_of: datetime) -> OutcomeStatus:
        if self.status == "scored":
            if self.released(as_of):
                return "realized"
            if as_of >= self.window_end and self.outcome_status in ("not_realized", "unknown"):
                return self.outcome_status
            return "pending"
        if as_of >= self.window_end and self.outcome_status in (
            "event_without_forecast",
            "no_event_without_forecast",
            "unknown",
        ):
            return self.outcome_status
        return "pending"


# -- connection, scope, as_of ------------------------------------------------------------------


def _scope(settings: Settings) -> Scope:
    if settings.mode == "replay" and settings.replay_snapshot_id:
        return Scope(settings.replay_namespace, settings.replay_snapshot_id)
    if settings.mode == "received" and settings.received_stream_id:
        return Scope(settings.received_namespace, settings.received_stream_id)
    raise ForecastNotImplemented("forecast scope is not configured")


def _connect(settings: Settings) -> psycopg.Connection:
    if settings.db_dsn is None:
        raise ForecastNotImplemented("database is not configured")
    connection = psycopg.connect(settings.db_dsn.get_secret_value(), row_factory=tuple_row)
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    return connection


def _published(connection: psycopg.Connection, scope: Scope) -> pg.ScopeRow:
    row = pg.read_scope(connection, scope)
    if row is None or row.generation == 0 or row.data_as_of is None:
        raise ForecastNotImplemented("no forecast publication in this scope")
    return row


def published_generation(settings: Settings) -> int | None:
    """Generation of the forecast the worker published in the configured scope.

    Readiness of ``/health/ready`` and ``/api/v1/capabilities``; ``None`` without a scope,
    a database, migration 0013 or a publication.
    """
    if settings.db_dsn is None:
        return None
    try:
        scope = _scope(settings)
        with psycopg.connect(
            settings.db_dsn.get_secret_value(), row_factory=tuple_row, connect_timeout=5
        ) as connection:
            row = pg.read_scope(connection, scope)
    except (ForecastNotImplemented, psycopg.Error):
        return None
    if row is None or row.generation == 0 or row.data_as_of is None:
        return None
    return row.generation


def _effective_as_of(row: pg.ScopeRow, as_of: datetime | None) -> datetime:
    """The requested moment, never later than the data (``data_as_of``)."""
    if as_of is None:
        return row.data_as_of
    return min(as_of, row.data_as_of)


# Publication-bound reads of this process (OPS-01); keys come from ``_cache_key``.
READ_CACHE = ReadCache()
# Parsed card snapshots (~28 KB each) shared by the cached answers: a list entry refers
# to them instead of holding its own copies. 2 048 cards ≈ 60 MB at most.
CARD_CACHE = ReadCache(max_entries=2048)


def _publication(connection: psycopg.Connection, scope: Scope) -> tuple | None:
    """Database, scope and run of the publication this transaction sees (cache identity)."""
    found = connection.execute(
        """SELECT s.generation, s.data_as_of, r.created_at
           FROM forecast_scopes AS s
           JOIN forecast_runs AS r
             ON r.namespace_id = s.namespace_id AND r.snapshot_id = s.snapshot_id
            AND r.generation = s.generation
           WHERE s.namespace_id = %s AND s.snapshot_id = %s""",
        scope.key,
    ).fetchone()
    if found is None:
        return None
    # The connection string without password: database and search_path (test schemas).
    return (connection.info.dsn, *scope.key, *found)


def _cache_key(connection: psycopg.Connection, scope: Scope, *parts) -> tuple | None:
    """Key of a publication-bound read; ``None`` (uncached) without a publication."""
    publication = _publication(connection, scope)
    return None if publication is None else (*parts, *publication)


# Card columns without the card JSON: reading ``card ->> ...`` of every card detoasts
# each JSON twice (17 ms of 18 ms of the query on 1 727 cards, 28.09.2026).
_LIGHT_COLUMNS = """c.card_id, c.journal_position, c.object_id, c.target_spec_id, c.horizon,
                    c.status, c.issued_at, c.window_end, o.status, o.release_at,
                    o.resolved_at, c.published_at"""


def _scope_lights(connection: psycopg.Connection, scope: Scope) -> tuple[CardLight, ...]:
    """Every card of the publication with its stored outcome, cached per publication."""

    def read() -> tuple[CardLight, ...]:
        rows = connection.execute(
            f"""SELECT {_LIGHT_COLUMNS}
                FROM forecast_cards AS c
                LEFT JOIN forecast_card_outcomes AS o
                  ON o.namespace_id = c.namespace_id AND o.snapshot_id = c.snapshot_id
                 AND o.card_id = c.card_id
                WHERE c.namespace_id = %s AND c.snapshot_id = %s
                ORDER BY c.journal_position""",
            scope.key,
        ).fetchall()
        return tuple(CardLight(*row) for row in rows)

    return READ_CACHE.get_or_compute(_cache_key(connection, scope, "lights"), read)


def _light_cards(
    connection: psycopg.Connection, scope: Scope, as_of: datetime, object_id: str | None = None
) -> list[CardLight]:
    """Cards published at or before ``as_of`` (of one object), by journal position."""

    def of_object(light: CardLight) -> bool:
        if object_id is None:
            return True
        if object_id == "__unknown__":
            return light.object_id is None
        return light.object_id == object_id

    return [
        light
        for light in _scope_lights(connection, scope)
        if light.published_at <= as_of and of_object(light)
    ]


def _risk_ranks(
    connection: psycopg.Connection, scope: Scope, card_ids: list[str]
) -> dict[str, tuple[str | None, int | None]]:
    """Risk level and rank of the given cards only (the list filters and sorts by them)."""
    if not card_ids:
        return {}
    rows = connection.execute(
        """SELECT card_id, card ->> 'risk_level', (card -> 'score' ->> 'rank')::integer
           FROM forecast_cards
           WHERE namespace_id = %s AND snapshot_id = %s AND card_id = ANY(%s)""",
        (*scope.key, card_ids),
    ).fetchall()
    return {card_id: (risk, rank) for card_id, risk, rank in rows}


def _cards_json(
    connection: psycopg.Connection, scope: Scope, card_ids: list[str]
) -> dict[str, ForecastCard]:
    """Parsed card snapshots, shared by the cached answers of one publication."""
    if not card_ids:
        return {}
    publication = _publication(connection, scope)
    cards: dict[str, ForecastCard] = {}
    missing = []
    for card_id in card_ids:
        found, card = (
            (False, None) if publication is None else CARD_CACHE.get((publication, card_id))
        )
        if found:
            cards[card_id] = card
        else:
            missing.append(card_id)
    if missing:
        rows = connection.execute(
            """SELECT card_id, card FROM forecast_cards
               WHERE namespace_id = %s AND snapshot_id = %s AND card_id = ANY(%s)""",
            (*scope.key, missing),
        ).fetchall()
        for card_id, card in rows:
            cards[card_id] = ForecastCard.model_validate(card)
            if publication is not None:
                CARD_CACHE.put((publication, card_id), cards[card_id])
    return cards


@dataclass(frozen=True)
class RecommendationInputs:
    """What the recommendation (v5) reads besides the card, per publication: the cards of
    each pair (issue, status, release) and the objects whose layout has a section switch."""

    cards: dict[tuple[str | None, str], tuple[il.PriorCard, ...]]
    section_objects: frozenset[str]


def _recommendation_inputs(connection: psycopg.Connection, scope: Scope) -> RecommendationInputs:
    def read() -> RecommendationInputs:
        cards: dict[tuple[str | None, str], list[il.PriorCard]] = {}
        for light in _scope_lights(connection, scope):
            cards.setdefault((light.object_id, light.target_spec_id), []).append(
                il.PriorCard(
                    issued_at=light.issued_at,
                    scored=light.status == "scored",
                    release_at=light.release_at,
                )
            )
        sections = connection.execute(
            """SELECT DISTINCT object_id FROM forecast_channel_layout
               WHERE sensor_type = %s AND role = 'landmark' AND landmark_kind = 'other'
                 AND object_id IS NOT NULL""",
            (PHASE_SENSOR_TYPE,),
        ).fetchall()
        return RecommendationInputs(
            cards={key: tuple(value) for key, value in cards.items()},
            section_objects=frozenset(object_id for (object_id,) in sections),
        )

    return READ_CACHE.get_or_compute(_cache_key(connection, scope, "recommendation"), read)


def _recommendation(card: ForecastCard, inputs: RecommendationInputs) -> ForecastRecommendation:
    """Policy v5 at read time: the issued card, the pair's earlier cards and releases not later
    than its issue, the section switch of the object's layout. The same card always reads the
    same text, whatever ``as_of`` (a later card or release is never used)."""
    rule = il.recommend(
        card.status == "scored",
        [(channel.channel_name, channel.feeder_kind) for channel in card.channels],
        repeat=il.repeat_at(
            card.issued_at, inputs.cards.get((card.object_id, card.target_spec_id), ())
        ),
        section_switch=card.object_id in inputs.section_objects,
    )
    return ForecastRecommendation(
        decision_code=None,
        text=rule.text,
        details=list(rule.details),
        rule_ids=list(rule.rule_ids),
        policy_version=rule.policy_version,
        regulation_confirmed=False,
    )


def _view(
    card: ForecastCard, light: CardLight, as_of: datetime, inputs: RecommendationInputs
) -> ForecastCardView:
    state = light.list_state(as_of)
    days_total = il.WINDOW_DAYS
    day_index = None
    if state == "open":
        day_index = min((as_of - card.issued_at) // timedelta(days=1) + 1, days_total)
    return ForecastCardView(
        card=card,
        list_state=state,
        released_at=light.release_at if state == "released" else None,
        day_index=day_index,
        days_total=days_total if day_index is not None else None,
        recommendation=_recommendation(card, inputs),
    )


def _cutoff_run(
    connection: psycopg.Connection, scope: Scope, as_of: datetime
) -> tuple[datetime, str, int] | None:
    return connection.execute(
        """SELECT cutoff_at, run_id, generation FROM forecast_cutoffs
           WHERE namespace_id = %s AND snapshot_id = %s AND cutoff_at <= %s
           ORDER BY cutoff_at DESC LIMIT 1""",
        (*scope.key, as_of),
    ).fetchone()


def _runs(connection: psycopg.Connection, scope: Scope, as_of: datetime) -> list[PublishedRun]:
    found = _cutoff_run(connection, scope, as_of)
    if found is None:
        return []
    cutoff, run_id, generation = found
    return [
        PublishedRun(
            target_spec_id=FEEDER_TARGET,
            horizon=HORIZON,
            run_id=run_id,
            generation=generation,
            issued_at=cutoff,
            published_at=cutoff,
            list_policy=LIST_POLICY,
        )
    ]


def _token(scope: Scope, generation: int, as_of: datetime) -> str:
    material = f"{scope.namespace_id}|{scope.snapshot_id}|{generation}|{as_of.isoformat()}"
    return "pt-" + hashlib.sha256(material.encode()).hexdigest()[:16]


def _parse_cursor(cursor: str | None, pattern: str) -> tuple[str, int] | None:
    if cursor is None:
        return None
    match = re.fullmatch(rf"({pattern})\.(\d{{1,9}})", cursor)
    if match is None:
        raise ForecastCursorError("invalid_forecast_cursor")
    return match.group(1), int(match.group(2))


# -- list and card -----------------------------------------------------------------------------


def forecast_list(
    settings: Settings,
    *,
    as_of: datetime | None,
    horizon: Horizon | None,
    target_spec_id: TargetSpecId | None,
    risk_level: RiskLevel | None,
    status: Literal["scored", "abstained"] | None,
    sensor_type: str | None,
    object_id: str | None,
    list_state: ListState,
    released_since: datetime | None,
    cursor: str | None,
    limit: int,
) -> ForecastList:
    scope = _scope(settings)
    with _connect(settings) as connection:
        row = _published(connection, scope)
        moment = _effective_as_of(row, as_of)
        token = _token(scope, row.generation, moment)
        offset = 0
        parsed = _parse_cursor(cursor, r"pt-[0-9a-f]{16}")
        if parsed is not None:
            if parsed[0] != token:
                raise StaleForecastCursor("forecast_cursor_stale")
            offset = parsed[1]

        def read() -> ForecastList:
            runs = _runs(connection, scope, moment)
            lights = _light_cards(connection, scope, moment, object_id)

            def wanted(light: CardLight) -> bool:
                if status == "abstained":
                    if light.status != "abstained" or not (
                        light.issued_at <= moment < light.window_end
                    ):
                        return False
                elif light.status != "scored" or light.list_state(moment) != list_state:
                    return False
                if status == "scored" and light.status != "scored":
                    return False
                if horizon is not None and light.horizon != horizon:
                    return False
                if target_spec_id is not None and light.target_spec_id != target_spec_id:
                    return False
                if sensor_type is not None and sensor_type != PHASE_SENSOR_TYPE:
                    return False
                if released_since is not None and (
                    light.release_at is None
                    or not light.released(moment)
                    or light.release_at < released_since
                ):
                    return False
                return True

            selected = [light for light in lights if wanted(light)] if runs else []
            ranks = _risk_ranks(connection, scope, [light.card_id for light in selected])
            if risk_level is not None:
                selected = [
                    light
                    for light in selected
                    if ranks.get(light.card_id, (None, None))[0] == risk_level
                ]
            selected.sort(
                key=lambda light: (
                    light.issued_at,
                    ranks.get(light.card_id, (None, None))[1] or 0,
                    light.card_id,
                )
            )
            page = selected[offset : offset + limit]
            cards = _cards_json(connection, scope, [light.card_id for light in page])
            inputs = _recommendation_inputs(connection, scope)
            end = offset + len(page)
            return ForecastList(
                mode=row.mode,
                as_of=moment,
                publication_token=token,
                runs=runs,
                items=[_view(cards[light.card_id], light, moment, inputs) for light in page],
                total=len(selected),
                limit=limit,
                next_cursor=f"{token}.{end}" if end < len(selected) else None,
            )

        key = _cache_key(
            connection,
            scope,
            "forecast_list",
            moment,
            horizon,
            target_spec_id,
            risk_level,
            status,
            sensor_type,
            object_id,
            list_state,
            released_since,
            offset,
            limit,
        )
        return READ_CACHE.get_or_compute(key, read)


def forecast_card(
    settings: Settings, forecast_id: str, *, as_of: datetime | None
) -> ForecastCardView | None:
    """Card with its list state at ``as_of``; ``None`` when not issued (yet) in the scope."""
    scope = _scope(settings)
    with _connect(settings) as connection:
        row = _published(connection, scope)
        moment = _effective_as_of(row, as_of)
        found = connection.execute(
            f"""SELECT {_LIGHT_COLUMNS}, c.card
               FROM forecast_cards AS c
               LEFT JOIN forecast_card_outcomes AS o
                 ON o.namespace_id = c.namespace_id AND o.snapshot_id = c.snapshot_id
                AND o.card_id = c.card_id
               WHERE c.namespace_id = %s AND c.snapshot_id = %s AND c.card_id = %s
                 AND c.published_at <= %s""",
            (*scope.key, forecast_id, moment),
        ).fetchone()
        if found is None:
            return None
        inputs = _recommendation_inputs(connection, scope)
    light = CardLight(*found[:12])
    return _view(ForecastCard.model_validate(found[12]), light, moment, inputs)


# -- journal -----------------------------------------------------------------------------------


def _records_as_of(
    connection: psycopg.Connection, as_of: datetime | None, pinned: datetime | None
) -> datetime:
    """Wall-clock bound of the decisions and check results of a journal read.

    The cursor's pinned moment, else the requested ``as_of``, never later than the read.
    ``clock_timestamp()`` is taken after the transaction's snapshot: every decision or
    check result it sees was saved (``clock_timestamp()`` of its insert) before it.
    """
    now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
    wanted = pinned if pinned is not None else as_of
    return now if wanted is None else min(wanted, now)


def _decisions(
    connection: psycopg.Connection, cards: dict[str, ForecastCard], records_as_of: datetime
) -> dict[str, ForecastDecisionSummary]:
    """Newest decision per card saved by ``records_as_of`` (B3); none before migration 0014."""
    if not cards or not pg.table_exists(connection, "forecast_decisions"):
        return {}
    latest = decisions_pg.latest_decisions(connection, list(cards), as_of=records_as_of)
    return {
        card_id: decision
        for card_id, decision in latest.items()
        if decision.decided_at >= cards[card_id].published_at
    }


def _decided_ids(
    connection: psycopg.Connection, lights: list[CardLight], records_as_of: datetime
) -> set[str]:
    """Cards whose entry carries a decision (the «без решения» filter), as ``_decisions``."""
    if not lights or not pg.table_exists(connection, "forecast_decisions"):
        return set()
    published = {light.card_id: light.published_at for light in lights}
    rows = connection.execute(
        """SELECT forecast_id, max(decided_at) FROM forecast_decisions
           WHERE forecast_id = ANY(%s) AND decided_at <= %s
           GROUP BY forecast_id""",
        (list(published), records_as_of),
    ).fetchall()
    return {card_id for card_id, latest in rows if latest >= published[card_id]}


def _check_results(
    connection: psycopg.Connection, cards: dict[str, ForecastCard], records_as_of: datetime
) -> dict[str, ForecastCheckResult]:
    """Newest check result per card saved by ``records_as_of`` (C0.4); none before 0019."""
    if not cards or not pg.table_exists(connection, "forecast_check_results"):
        return {}
    latest = check_results_pg.latest_check_results(connection, list(cards), as_of=records_as_of)
    return {
        card_id: result
        for card_id, result in latest.items()
        if result.recorded_at >= cards[card_id].published_at
    }


POWER_OFF_STATE = "Обесточен"
POWER_OFF_WINDOW = timedelta(minutes=60)


def _power_offs(
    connection: psycopg.Connection,
    scope: Scope,
    probes: list[tuple[str, str, list[str], datetime]],
    as_of: datetime,
) -> dict[tuple[str, str], datetime | None]:
    """First «Обесточен» on the event channels within 60 min after the start (C0.4).

    ``probes`` are ``(card_id, event_id, channel_ids, started_at)`` of a journal page,
    read in one query (one per event before 28.09.2026: 37 queries for 25 cards).
    """
    wanted = [probe for probe in probes if probe[3] is not None and probe[2]]
    if not wanted:
        return {}
    document = [
        {"i": index, "channels": list(channels), "started": started.isoformat()}
        for index, (_card, _event, channels, started) in enumerate(wanted)
    ]
    rows = connection.execute(
        """SELECT probe.i, (
             SELECT min(o.event_at) FROM dispatch_observations AS o
             WHERE o.namespace_id = %s AND o.snapshot_id = %s
               AND o.channel_id = ANY(ARRAY(SELECT jsonb_array_elements_text(probe.channels)))
               AND o.value_raw = %s AND o.event_at >= probe.started
               AND o.event_at < probe.started + %s AND o.event_at <= %s)
           FROM jsonb_to_recordset(%s) AS probe(i integer, channels jsonb, started timestamptz)""",
        (*scope.key, POWER_OFF_STATE, POWER_OFF_WINDOW, as_of, Jsonb(document)),
    ).fetchall()
    return {(wanted[index][0], wanted[index][1]): first for index, first in rows}


def _window_events(
    connection: psycopg.Connection, scope: Scope, card_ids: list[str]
) -> dict[str, list[tuple]]:
    if not card_ids:
        return {}
    rows = connection.execute(
        """SELECT card_id, event_id, started_at, channel_ids, cluster_size, while_open,
                  candidate_member
           FROM forecast_card_window_events
           WHERE namespace_id = %s AND snapshot_id = %s AND card_id = ANY(%s)
           ORDER BY started_at, event_id""",
        (*scope.key, card_ids),
    ).fetchall()
    out: dict[str, list[tuple]] = {}
    for card_id, *rest in rows:
        out.setdefault(card_id, []).append(tuple(rest))
    return out


def _outcome_details(connection: psycopg.Connection, scope: Scope, card_ids: list[str]) -> dict:
    if not card_ids:
        return {}
    rows = connection.execute(
        """SELECT card_id, first_event_at, event_channel_ids, event_cluster_size, unknown_reason
           FROM forecast_card_outcomes
           WHERE namespace_id = %s AND snapshot_id = %s AND card_id = ANY(%s)""",
        (*scope.key, card_ids),
    ).fetchall()
    return {row[0]: row[1:] for row in rows}


def _outcome(
    card: ForecastCard, light: CardLight, as_of: datetime, details: tuple, events: list[tuple]
) -> ForecastOutcome:
    status = light.outcome_at(as_of)
    abstained = card.status == "abstained"
    base = {
        "label_version": card.versions.label_version,
        "excluded_from_quality_metrics": abstained,
    }
    if status == "pending":
        return ForecastOutcome(status="pending", **base)
    first_event_at, channel_ids, cluster_size, unknown_reason = details
    others = sum(
        1 for event in events if not event[5] and event[1] <= as_of
    )  # events without a candidate member
    if status == "realized":
        return ForecastOutcome(
            status="realized",
            resolved_at=light.release_at,
            first_event_at=first_event_at,
            lead_hours=round((first_event_at - card.issued_at).total_seconds() / 3600, 2),
            event_channel_ids=list(channel_ids),
            event_cluster_size=cluster_size,
            other_events_on_object_count=others,
            **base,
        )
    if status == "event_without_forecast":
        return ForecastOutcome(
            status=status,
            resolved_at=card.window_end,
            first_event_at=first_event_at,
            event_channel_ids=list(channel_ids),
            event_cluster_size=cluster_size,
            other_events_on_object_count=others,
            **base,
        )
    if status == "unknown":
        return ForecastOutcome(
            status="unknown",
            resolved_at=card.window_end,
            unknown_reason=unknown_reason or "source_coverage",
            **base,
        )
    return ForecastOutcome(
        status=status,
        resolved_at=card.window_end,
        other_events_on_object_count=others,
        **base,
    )


def forecast_journal(
    settings: Settings,
    *,
    as_of: datetime | None,
    outcome: OutcomeStatus | None,
    horizon: Horizon | None,
    target_spec_id: TargetSpecId | None,
    object_id: str | None,
    list_state: ListState | None,
    issued_from: date | None,
    issued_to: date | None,
    cursor: str | None,
    limit: int,
    decision_state: Literal["none", "any"] | None = None,
    records_as_of: datetime | None = None,
) -> ForecastJournalList:
    """Journal page: cards and outcomes at ``as_of`` (data), decisions and check results
    saved by ``records_as_of`` (wall clock; by default the requested ``as_of`` or, without
    it, the read). A cursor carries the watermark and the records bound of its first page.
    """
    scope = _scope(settings)
    with _connect(settings) as connection:
        row = _published(connection, scope)
        moment = _effective_as_of(row, as_of)
        lights = _light_cards(connection, scope, moment)
        latest = max((light.position for light in lights), default=0)
        watermark, offset, pinned = latest, 0, None
        parsed = _parse_cursor(cursor, r"w\d{1,9}(?:r\d{1,16})?")
        if parsed is not None:
            head, offset = parsed
            position, _, micros = head[1:].partition("r")
            watermark = int(position)
            pinned = _EPOCH + timedelta(microseconds=int(micros)) if micros else None
            if watermark > latest:
                raise ForecastCursorError("journal_watermark_out_of_range")
        recorded = _records_as_of(
            connection, as_of if records_as_of is None else records_as_of, pinned
        )

        def wanted(light: CardLight) -> bool:
            issued = il.msk_day(light.issued_at)
            return (
                light.position <= watermark
                and (outcome is None or light.outcome_at(moment) == outcome)
                and (horizon is None or light.horizon == horizon)
                and (target_spec_id is None or light.target_spec_id == target_spec_id)
                and (
                    object_id is None
                    or (
                        light.object_id is None
                        if object_id == "__unknown__"
                        else light.object_id == object_id
                    )
                )
                and (list_state is None or light.list_state(moment) == list_state)
                and (issued_from is None or issued >= issued_from)
                and (issued_to is None or issued <= issued_to)
            )

        selected = [light for light in lights if wanted(light)]
        if decision_state is not None:
            decided = _decided_ids(connection, selected, recorded)
            selected = [
                light
                for light in selected
                if (light.card_id in decided) == (decision_state == "any")
            ]
        selected.sort(key=lambda light: light.position, reverse=True)
        page = selected[offset : offset + limit]
        ids = [light.card_id for light in page]
        cards = _cards_json(connection, scope, ids)
        events = _window_events(connection, scope, ids)
        details = _outcome_details(connection, scope, ids)
        decisions = _decisions(connection, cards, recorded)
        results = _check_results(connection, cards, recorded)
        power_off = _power_offs(
            connection,
            scope,
            [
                (card_id, event_id, list(channels), started)
                for card_id in ids
                for event_id, started, channels, *_rest in events.get(card_id, [])
                if started <= moment
            ],
            moment,
        )
    items = []
    for light in page:
        card = cards[light.card_id]
        rows = [event for event in events.get(light.card_id, []) if event[1] <= moment]
        state = light.list_state(moment)
        outcome = _outcome(card, light, moment, details.get(light.card_id, (None,) * 4), rows)
        if outcome.first_event_at is not None:
            first = next(
                (
                    power_off.get((light.card_id, event_id))
                    for event_id, started, *_rest in rows
                    if started == outcome.first_event_at
                ),
                None,
            )
            if first is not None:
                outcome = outcome.model_copy(update={"power_off_at": first})
        items.append(
            ForecastJournalEntry(
                journal_position=light.position,
                card=card,
                outcome=outcome,
                decision=decisions.get(light.card_id),
                list_state=state,
                released_at=light.release_at if state == "released" else None,
                events=[
                    ForecastWindowEvent(
                        event_id=event_id,
                        started_at=started,
                        channel_ids=list(channels),
                        cluster_size=size,
                        while_open=while_open,
                        power_off_at=power_off.get((light.card_id, event_id)),
                    )
                    for event_id, started, channels, size, while_open, _member in rows
                ],
                check_result=results.get(light.card_id),
            )
        )
    end = offset + len(page)
    micros = (recorded - _EPOCH) // timedelta(microseconds=1)
    return ForecastJournalList(
        mode=row.mode,
        as_of=moment,
        records_as_of=recorded,
        journal_watermark=watermark,
        items=items,
        total=len(selected),
        limit=limit,
        next_cursor=f"w{watermark}r{micros}.{end}" if end < len(selected) else None,
    )


def _week(moment: datetime) -> date:
    day = il.msk_day(moment)
    return day - timedelta(days=day.weekday())


def journal_counts(settings: Settings, *, as_of: datetime | None) -> ForecastJournalCounts:
    scope = _scope(settings)
    with _connect(settings) as connection:
        row = _published(connection, scope)
        moment = _effective_as_of(row, as_of)
        key = _cache_key(connection, scope, "journal_counts", moment)
        return READ_CACHE.get_or_compute(
            key,
            lambda: _journal_counts(row.mode, moment, _light_cards(connection, scope, moment)),
        )


def _journal_counts(mode: str, moment: datetime, cards: list[CardLight]) -> ForecastJournalCounts:
    lights = [light for light in cards if light.status == "scored"]
    weeks: dict[date, list[CardLight]] = {}
    for light in lights:
        weeks.setdefault(_week(light.issued_at), []).append(light)
    rows = []
    for start, group in sorted(weeks.items()):
        states = [light.list_state(moment) for light in group]
        outcomes = [light.outcome_at(moment) for light in group]
        rows.append(
            ForecastJournalCountsRow(
                period_start=start,
                period_end=start + timedelta(days=7),
                target_spec_id=FEEDER_TARGET,
                horizon=HORIZON,
                cards_issued=len(group),
                cards_released=states.count("released"),
                cards_no_event=outcomes.count("not_realized"),
                cards_unknown=outcomes.count("unknown"),
                cards_open=states.count("open"),
            )
        )
    return ForecastJournalCounts(mode=mode, as_of=moment, rows=rows)


# -- quality (research permission) -------------------------------------------------------------


def _ratio(name: str, numerator: int, denominator: int, version: str) -> RatioMetric:
    return RatioMetric(
        name=name,
        definition_version=version,
        numerator=numerator,
        denominator=denominator,
        value=round(numerator / denominator, 4) if denominator else None,
    )


def quality_summary(settings: Settings, *, as_of: datetime | None) -> ForecastQualitySummary:
    """Weekly «прогноз против факта»: k из n of cards and the v9 recalls of events.

    ``R_incident`` (main, v9): events of an object less than 24 h apart form an incident;
    it is caught if a card of the object is open at the start of its first event and the
    event has a member on a candidate channel of the card. ``R_strict``: an event is
    caught only as the releasing (first) event of an open card. Events are counted by the
    week of their start (the head event for incidents), cards by the week of issue.
    """
    scope = _scope(settings)
    with _connect(settings) as connection:
        row = _published(connection, scope)
        moment = _effective_as_of(row, as_of)
        lights = _light_cards(connection, scope, moment)
        candidates = dict(
            connection.execute(
                """SELECT card_id, candidate_channel_ids FROM forecast_cards
                   WHERE namespace_id = %s AND snapshot_id = %s AND status = 'scored'""",
                scope.key,
            ).fetchall()
        )
        events = pg.read_events(connection, scope, until=moment)
        coverage = pg.read_coverage(connection, scope)
        seeded = connection.execute(
            """SELECT count(*) FILTER (WHERE import_id IS NULL OR import_id LIKE 'seed%%'),
                      count(*)
               FROM forecast_runs WHERE namespace_id = %s AND snapshot_id = %s""",
            scope.key,
        ).fetchone()
    scored = [light for light in lights if light.status == "scored"]
    first_cutoff = min((light.issued_at for light in lights), default=None)
    by_object: dict[str, list[CardLight]] = {}
    for light in scored:
        by_object.setdefault(light.object_id, []).append(light)

    def open_card(event) -> CardLight | None:
        for light in by_object.get(event.object_id, []):
            release = light.release_at if light.released(moment) else None
            if light.issued_at <= event.start_at < light.window_end and (
                release is None or event.start_at <= release
            ):
                if set(candidates.get(light.card_id, [])).intersection(event.channel_ids):
                    return light
        return None

    weeks: dict[date, dict] = {}
    for light in scored:
        weeks.setdefault(_week(light.issued_at), {"cards": [], "events": [], "heads": []})
        weeks[_week(light.issued_at)]["cards"].append(light)
    per_object: dict[str, list] = {}
    for event in events:
        per_object.setdefault(event.object_id, []).append(event)
    for object_events in per_object.values():
        starts = [event.start_at for event in object_events]
        heads = il.incident_heads(starts)
        for event, head in zip(object_events, heads, strict=True):
            if first_cutoff is None or event.start_at < first_cutoff:
                continue
            covered, policy = coverage.get(il.msk_day(event.start_at), (False, False))
            if not covered or policy:
                continue
            slot = weeks.setdefault(_week(event.start_at), {"cards": [], "events": [], "heads": []})
            card = open_card(event)
            caught_first = (
                card is not None
                and card.release_at is not None
                and card.released(moment)
                and card.release_at == event.start_at
            )
            slot["events"].append(caught_first)
            if head:
                slot["heads"].append(card is not None)
    rows = []
    for start, slot in sorted(weeks.items()):
        outcomes = [light.outcome_at(moment) for light in slot["cards"]]
        realized, not_realized = outcomes.count("realized"), outcomes.count("not_realized")
        rows.append(
            ForecastQualityRow(
                period_start=start,
                period_end=start + timedelta(days=7),
                target_spec_id=FEEDER_TARGET,
                horizon=HORIZON,
                cards_issued=len(slot["cards"]),
                cards_pending=outcomes.count("pending"),
                cards_realized=realized,
                cards_not_realized=not_realized,
                cards_unknown=outcomes.count("unknown"),
                card_share=_ratio(
                    "card_share", realized, realized + not_realized, il.METRIC_DEFINITION_VERSION
                ),
                event_metrics=[
                    _ratio(
                        "r_incident",
                        sum(slot["heads"]),
                        len(slot["heads"]),
                        il.METRIC_DEFINITION_VERSION,
                    ),
                    _ratio(
                        "r_strict",
                        sum(slot["events"]),
                        len(slot["events"]),
                        il.METRIC_DEFINITION_VERSION,
                    ),
                ],
            )
        )
    live = seeded is not None and seeded[0] == 0 and seeded[1] > 0
    return ForecastQualitySummary(
        mode=row.mode,
        as_of=moment,
        evidence_scope="live_uploads" if live else "demo_period",
        rows=rows,
    )


# -- state and registry ------------------------------------------------------------------------


def forecast_state(settings: Settings) -> ForecastState:
    scope = _scope(settings)
    checked_at = datetime.now(UTC)
    with _connect(settings) as connection:
        row = pg.read_scope(connection, scope)
        mode = settings.mode if settings.mode in ("replay", "received") else "received"
        messages = connection.execute(
            """SELECT max(event_at) FROM dispatch_observations
               WHERE namespace_id = %s AND snapshot_id = %s""",
            scope.key,
        ).fetchone()
        source_messages_as_of = None if messages is None else messages[0]
        if row is None or row.generation == 0 or row.data_as_of is None:
            return ForecastState(
                mode=mode,
                checked_at=checked_at,
                generation=0,
                source_messages_as_of=source_messages_as_of,
            )
        run = connection.execute(
            """SELECT run_id, import_id, created_at, last_cutoff FROM forecast_runs
               WHERE namespace_id = %s AND snapshot_id = %s AND generation = %s""",
            (*scope.key, row.generation),
        ).fetchone()
        previous = connection.execute(
            """SELECT data_as_of FROM forecast_runs
               WHERE namespace_id = %s AND snapshot_id = %s AND generation = %s""",
            (*scope.key, row.generation - 1),
        ).fetchone()
        lights = _light_cards(connection, scope, row.data_as_of)
        # P1-2: an empty list explains itself when the latest cutoffs had no data before them.
        no_data = (
            pg.no_data_days_before(connection, scope, row.last_cutoff)
            if row.last_cutoff is not None
            else None
        )
        import_status = None
        if run[1] is not None and pg.table_exists(connection, "import_files"):
            found = connection.execute(
                "SELECT status FROM import_files WHERE import_id = %s", (run[1],)
            ).fetchone()
            import_status = None if found is None else found[0]
    moment = row.data_as_of
    open_cards = [light for light in lights if light.list_state(moment) == "open"]
    # «С прошлой выдачи»: от данных прошлого поколения публикации; у засеянного хранилища с
    # одним поколением — от прошлой ежедневной выдачи (last_cutoff − 1 сут), не за всю историю.
    since = previous[0] if previous is not None else None
    if since is None and row.last_cutoff is not None:
        since = row.last_cutoff - timedelta(days=1)
    released = [
        light
        for light in lights
        if light.released(moment) and (since is None or light.release_at > since)
    ]
    horizons = []
    if row.last_cutoff is not None:
        horizons.append(
            HorizonState(
                target_spec_id=FEEDER_TARGET,
                horizon=HORIZON,
                run_id=run[0],
                issued_at=row.last_cutoff,
                open_cards=len(open_cards),
                new_cards=sum(1 for light in open_cards if light.issued_at == row.last_cutoff),
                released_since_previous=len(released),
                max_open=il.MAX_OPEN,
            )
        )
    return ForecastState(
        mode=row.mode,
        checked_at=max(checked_at, run[2]),
        generation=row.generation,
        data_as_of=row.data_as_of,
        published_at=run[2],
        horizons=horizons,
        last_import_id=run[1] if import_status is not None else None,
        last_import_status=import_status,
        source_messages_as_of=source_messages_as_of,
        no_data_from=no_data[0] if no_data else None,
        no_data_to=no_data[1] if no_data else None,
    )


def recurring_places(settings: Settings, *, as_of: datetime | None) -> RecurringPlaceList:
    scope = _scope(settings)
    with _connect(settings) as connection:
        row = _published(connection, scope)
        moment = _effective_as_of(row, as_of)
        events = pg.read_events(connection, scope, until=moment)
        names = pg.read_object_names(connection)
        lights = _light_cards(connection, scope, moment)
    index = il.EventIndex([event for event in events if event.start_at < moment])
    items = []
    for object_id in sorted(index.by_object, key=il.object_sort_key):
        year = index.count(object_id, moment - timedelta(days=365), moment)
        if year < il.RECURRING_MIN_EVENTS:
            continue
        starts = index.starts[object_id]
        items.append(
            RecurringPlace(
                object_id=object_id,
                object_name=names.get(object_id),
                sensor_type=PHASE_SENSOR_TYPE,
                incident_type="feeder_power_loss",
                events_90d=index.count(object_id, moment - timedelta(days=90), moment),
                events_365d=year,
                last_event_at=starts[-1] if starts else None,
                open_card_ids=[
                    light.card_id
                    for light in lights
                    if light.object_id == object_id and light.list_state(moment) == "open"
                ],
                rule_version=il.RECURRING_RULE_VERSION,
            )
        )
    items.sort(key=lambda item: (-item.events_365d, il.object_sort_key(item.object_id)))
    return RecurringPlaceList(mode=row.mode, as_of=moment, items=items, total=len(items))


# -- picket-line scheme ------------------------------------------------------------------------


def _picket(form: str, low: float | None, high: float | None, basis: str | None) -> SchemePicket:
    if form == "unknown":
        return SchemePicket(form="unknown")
    return SchemePicket(
        form=form, picket_from=low, picket_to=high if form == "range" else None, basis=basis
    )


def _object_layout(
    connection: psycopg.Connection, scope: Scope, object_id: str | None = None
) -> dict[str, list[pg.LayoutRow]]:
    layout = pg.read_layout(connection, object_id=object_id)
    known = {
        channel
        for (channel,) in connection.execute(
            """SELECT channel_id FROM forecast_detector_state
               WHERE namespace_id = %s AND snapshot_id = %s""",
            scope.key,
        ).fetchall()
    }
    per_object: dict[str, list[pg.LayoutRow]] = {}
    for row in layout.values():
        if row.object_id is None or (row.channel_id not in known and row.role != "landmark"):
            continue
        per_object.setdefault(row.object_id, []).append(row)
    return per_object


def _scheme(
    connection: psycopg.Connection,
    scope: Scope,
    mode: str,
    object_id: str,
    rows: list[pg.LayoutRow],
    moment: datetime,
    names: dict[str, str | None],
    lights: list[CardLight],
) -> ObjectScheme:
    feeders_rows = [row for row in rows if row.role == "feeder"]
    ids = [row.channel_id for row in feeders_rows]
    stats = dict(
        (channel, (count, last))
        for channel, count, last in connection.execute(
            """SELECT channel_id,
                      count(*) FILTER (WHERE start_at >= %s),
                      max(start_at)
               FROM forecast_phase_episodes
               WHERE namespace_id = %s AND snapshot_id = %s AND channel_id = ANY(%s)
                 AND start_at <= %s
               GROUP BY channel_id""",
            (moment - timedelta(days=365), *scope.key, ids, moment),
        ).fetchall()
    )
    alarms: dict[str, list[SchemeAlarm]] = {}
    day_ago = moment - timedelta(hours=24)
    # B-3 (F6): each alarm record carries the first later «Норма» of the channel without the
    # alarm flag up to the cut — then the line is no longer in alarm.
    for row_uid, channel_id, value_raw, event_at, cleared_at, cleared_value in connection.execute(
        """SELECT latest.row_uid, latest.channel_id, latest.value_raw, latest.event_at,
                  cleared.event_at, cleared.value_raw
           FROM unnest(%s::text[]) AS feeder(channel_id)
           CROSS JOIN LATERAL (
             SELECT row_uid, channel_id, value_raw, event_at FROM dispatch_observations
             WHERE namespace_id = %s AND snapshot_id = %s
               AND channel_id = feeder.channel_id
               AND event_at >= %s AND event_at <= %s AND alarm
             ORDER BY event_at DESC LIMIT %s) AS latest
           LEFT JOIN LATERAL (
             SELECT event_at, value_raw FROM dispatch_observations AS later
             WHERE later.namespace_id = %s AND later.snapshot_id = %s
               AND later.channel_id = latest.channel_id
               AND later.event_at > latest.event_at AND later.event_at <= %s
               AND later.alarm IS NOT TRUE
               AND lower(btrim(later.value_raw)) = ANY(%s)
             ORDER BY later.event_at LIMIT 1) AS cleared ON TRUE
           ORDER BY latest.event_at, latest.row_uid""",
        (
            ids,
            *scope.key,
            day_ago,
            moment,
            ALARMS_PER_FEEDER,
            *scope.key,
            moment,
            list(NORMAL_VALUES),
        ),
    ).fetchall():
        alarms.setdefault(channel_id, []).append(
            SchemeAlarm(
                row_uid=row_uid,
                channel_id=channel_id,
                value_raw=value_raw,
                event_at=event_at,
                cleared_at=cleared_at,
                cleared_value_raw=None if cleared_at is None else cleared_value,
            )
        )
    alarm_counts = dict(
        connection.execute(
            """SELECT channel_id, count(*) FROM dispatch_observations
               WHERE namespace_id = %s AND snapshot_id = %s AND channel_id = ANY(%s)
                 AND event_at >= %s AND event_at <= %s AND alarm
               GROUP BY channel_id""",
            (*scope.key, ids, day_ago, moment),
        ).fetchall()
    )
    feeders = []
    for row in sorted(
        feeders_rows, key=lambda r: (r.picket_from is None, r.picket_from or 0, r.name or "")
    ):
        count, last = stats.get(row.channel_id, (0, None))
        feeders.append(
            SchemeFeeder(
                channel_id=row.channel_id,
                name=row.name or row.channel_id,
                feeder_kind=row.feeder_kind or "other",
                picket=_picket(row.picket_form, row.picket_from, row.picket_to, row.picket_basis),
                episodes_365d=count or 0,
                last_episode_at=last,
                current_alarms=alarms.get(row.channel_id, []),
                alarm_records_24h=alarm_counts.get(row.channel_id, 0),
                named_link=_named_link(row.name),
            )
        )
    landmarks = [
        SchemeLandmark(
            channel_id=row.channel_id,
            name=row.name or row.channel_id,
            kind=row.landmark_kind or "other",
            picket=_picket(row.picket_form, row.picket_from, row.picket_to, row.picket_basis),
        )
        for row in sorted(rows, key=lambda r: (r.picket_from is None, r.picket_from or 0))
        if row.role == "landmark"
    ]
    mine = [light for light in lights if light.object_id == object_id]
    return ObjectScheme(
        mode=mode,
        as_of=moment,
        object_id=object_id,
        object_name=names.get(object_id),
        landmarks=landmarks,
        feeders=feeders,
        open_card_ids=[light.card_id for light in mine if light.list_state(moment) == "open"],
        released_7d=[
            SchemeReleasedCard(forecast_id=light.card_id, released_at=light.release_at)
            for light in mine
            if light.released(moment) and light.release_at >= moment - timedelta(days=7)
        ],
    )


def _named_link(name: str | None) -> SchemeNamedLink | None:
    """Label named in the channel name («ФВ2 (В23)» → «В23»), parsed at read time (C0.3)."""
    label = parse_named_link(name)
    return None if label is None else SchemeNamedLink(label=label)


def object_scheme(
    settings: Settings, object_id: str, *, as_of: datetime | None
) -> ObjectScheme | None:
    scope = _scope(settings)
    with _connect(settings) as connection:
        row = _published(connection, scope)
        moment = _effective_as_of(row, as_of)

        def read() -> ObjectScheme | None:
            layout = _object_layout(connection, scope, object_id)
            if object_id not in layout:
                return None
            return _scheme(
                connection,
                scope,
                row.mode,
                object_id,
                layout[object_id],
                moment,
                pg.read_object_names(connection),
                _light_cards(connection, scope, moment, object_id),
            )

        key = _cache_key(connection, scope, "object_scheme", moment, object_id)
        return READ_CACHE.get_or_compute(key, read)


def scheme_list(settings: Settings, *, as_of: datetime | None) -> ObjectSchemeList:
    scope = _scope(settings)
    with _connect(settings) as connection:
        row = _published(connection, scope)
        moment = _effective_as_of(row, as_of)

        def read() -> ObjectSchemeList:
            layout = _object_layout(connection, scope)
            names = pg.read_object_names(connection)
            lights = _light_cards(connection, scope, moment)
            schemes = [
                _scheme(connection, scope, row.mode, object_id, rows, moment, names, lights)
                for object_id, rows in sorted(
                    layout.items(), key=lambda item: il.object_sort_key(item[0])
                )
            ]
            return _scheme_summaries(row.mode, moment, schemes)

        key = _cache_key(connection, scope, "scheme_list", moment)
        return READ_CACHE.get_or_compute(key, read)


def _scheme_summaries(mode: str, moment: datetime, schemes: list[ObjectScheme]) -> ObjectSchemeList:
    items = []
    for scheme in schemes:
        known = [feeder for feeder in scheme.feeders if feeder.picket.form != "unknown"]
        span = [
            value
            for picket in [feeder.picket for feeder in known]
            + [
                landmark.picket
                for landmark in scheme.landmarks
                if landmark.picket.form != "unknown"
            ]
            for value in (picket.picket_from, picket.picket_to)
            if value is not None
        ]
        items.append(
            ObjectSchemeSummary(
                object_id=scheme.object_id,
                object_name=scheme.object_name,
                picket_min=min(span) if span else None,
                picket_max=max(span) if span else None,
                feeders=len(scheme.feeders),
                feeders_without_picket=len(scheme.feeders) - len(known),
                open_cards=len(scheme.open_card_ids),
                current_alarms=sum(len(feeder.current_alarms) for feeder in scheme.feeders),
                released_7d=len(scheme.released_7d),
            )
        )
    return ObjectSchemeList(mode=mode, as_of=moment, items=items, total=len(items))
