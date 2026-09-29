"""Synthetic forecast cards, journal and state for ``INFRA_MODE=fixture``.

All identifiers, pickets, counts, shares and timings are invented. Sensor and system
type names follow the source vocabulary. The served scenario is the production shape
of 27.09.2026 (C0.3): «обесточивание электрооборудования объекта» (ID
``phase-feeders/phase_loss_all``),
one card per object, a 14-day rolling list of 10 cards with release at the first event,
static-list score shown as «k из n», the top feeders of the object in the card and a
picket-line scheme. ``legacy_*`` builders keep the gate A sensor-failure examples for
contract rule tests; they are not served. Nothing here is a metric, model output or
customer data.
"""

import hashlib
import math
import re
from datetime import date, datetime, timedelta, timezone
from functools import cache
from typing import Literal

from infra_pulse_core.contracts.forecast import (
    FEEDER_TARGET,
    HORIZON_HOURS,
    TARGET_LABELS,
    BucketRate,
    ForecastCard,
    ForecastCardView,
    ForecastChannel,
    ForecastCheckResult,
    ForecastCheckResultList,
    ForecastDecisionList,
    ForecastDecisionSummary,
    ForecastFact,
    ForecastFreshness,
    ForecastJournalCounts,
    ForecastJournalCountsRow,
    ForecastJournalEntry,
    ForecastJournalList,
    ForecastList,
    ForecastOutcome,
    ForecastQualityRow,
    ForecastQualitySummary,
    ForecastRecommendation,
    ForecastScore,
    ForecastState,
    ForecastVersions,
    ForecastWindowEvent,
    FrequencyBucket,
    Horizon,
    HorizonState,
    ListPolicy,
    ListState,
    ObservedOverlay,
    ObservedOverlayItem,
    OutcomeStatus,
    PublishedRun,
    RankBucket,
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

MSK = timezone(timedelta(hours=3))  # cutoff 00:00 Europe/Moscow is an assumption of the spec
FIXTURE_AS_OF = datetime(2026, 9, 24, 10, 0, tzinfo=MSK)
SOURCE_REPORT = "synthetic-fixture-not-a-metric"
LABEL_VERSION = "synthetic-sensor-failure-labels-v0"
CONNECTION_LOSS: TargetSpecId = "sensor-failure/connection_loss_1h"
TECHNICAL_VALUE: TargetSpecId = "sensor-failure/technical_value"
FEEDERS: TargetSpecId = FEEDER_TARGET
FEEDER_LABEL_VERSION = "synthetic-phase-feeder-labels-all-episodes-v0"
PHASE = "Состояние фазы"
DISPATCH_SYSTEM = "Диспетчерский контроль"
HORIZON: Horizon = "336h"
# Per target x horizon publication counters of the synthetic runs shown now.
RUN_GENERATIONS: dict[tuple[str, str], int] = {(FEEDERS, HORIZON): 9}
LIST_POLICIES: dict[str, ListPolicy] = {
    HORIZON: ListPolicy(
        kind="rolling_release",
        max_open=10,
        window_days=14,
        release_on="first_event",
        policy_version="synthetic-feeder-list-14d-k10-v0",
    ),
}
_HORIZON_ORDER = {"24h": 0, "48h": 1, "168h": 2, "336h": 3}
# Data analyst 26.09: bucket starts per target x horizon policy; the last bucket is open-ended.
_BUCKET_STARTS = {"24h": [1, 6, 11, 51], "48h": [1, 6, 11, 51], "168h": [1, 11, 21, 51]}
# Risk level per bucket: no card inside the budget (10 per day, 50 per week) is "low".
_DAILY_LEVELS = ["high", "medium", "low", "low"]
_BUCKET_LEVELS = {
    "24h": _DAILY_LEVELS,
    "48h": _DAILY_LEVELS,
    "168h": ["high", "medium", "medium", "low"],
}


class ForecastCursorError(ValueError):
    """Cursor is malformed or outside the committed range."""


class StaleForecastCursor(ForecastCursorError):
    """Cursor belongs to a publication generation that is no longer current."""


def _at(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=MSK)


def _rate(cards: int, positive: int, start: date, end: date) -> BucketRate:
    return BucketRate(
        period_start=start,
        period_end=end,
        cards=cards,
        positive_cards=positive,
        event_rate=round(positive / cards, 4),
    )


def _score(
    target: TargetSpecId,
    horizon: Horizon,
    rank: int,
    ranked: int,
    budget: int,
    value: float,
    rates: tuple[int, int, int, int],
) -> ForecastScore:
    starts = _BUCKET_STARTS[horizon]
    index = max(i for i, start in enumerate(starts) if start <= rank)
    return ForecastScore(
        value=value,
        rank=rank,
        ranked_cards=ranked,
        card_budget=budget,
        rank_bucket=RankBucket(
            policy_id=f"synthetic-rank-buckets-{target.split('/')[1]}-{horizon}-v0",
            bucket_starts=starts,
            bucket_levels=_BUCKET_LEVELS[horizon],
            rank_from=starts[index],
            rank_to=starts[index + 1] - 1 if index + 1 < len(starts) else None,
            calibration=_rate(rates[0], rates[1], date(2025, 1, 1), date(2025, 7, 1)),
            holdout=_rate(rates[2], rates[3], date(2025, 7, 1), date(2026, 7, 1)),
            source_report=SOURCE_REPORT,
        ),
    )


def _run_id(target: TargetSpecId, horizon: Horizon, cutoff: datetime) -> str:
    return f"synthetic-run-{target.split('/')[1]}-{horizon}-{cutoff:%Y-%m-%d}"


def _versions(run_id: str) -> ForecastVersions:
    return ForecastVersions(
        scorer="persistence",
        model_version="synthetic-persistence-rule-v0",
        feature_version="synthetic-features-v0",
        label_version=LABEL_VERSION,
        policy_version="synthetic-forecast-policy-v0",
        calibration_version="synthetic-isotonic-2025h1-v0",
        release_id="synthetic-release-001",
        run_id=run_id,
    )


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


def _freshness(cutoff: datetime, *, history_days: int = 30) -> ForecastFreshness:
    return ForecastFreshness(
        data_as_of=cutoff - timedelta(seconds=30),
        last_object_record_at=cutoff - timedelta(minutes=2),
        lookback_days=30,
        history_days_available=history_days,
        coverage="complete" if history_days >= 30 else "insufficient",
    )


def _card(
    card_id: str,
    *,
    target: TargetSpecId,
    object_id: str,
    sensor_type: str,
    system_type: str,
    horizon: Horizon,
    cutoff: datetime,
    risk_level: RiskLevel,
    score: ForecastScore | None,
    channels: list[ForecastChannel],
    facts: list[ForecastFact],
    **extra,
) -> ForecastCard:
    freshness = extra.pop("freshness", None) or _freshness(cutoff)
    return ForecastCard(
        id=card_id,
        mode="fixture",
        target_spec_id=target,
        target_label=TARGET_LABELS[target],
        object_id=object_id,
        sensor_type=sensor_type,
        system_type=system_type,
        horizon=horizon,
        issued_at=cutoff,
        window_start=cutoff,
        window_end=cutoff + timedelta(hours=HORIZON_HOURS[horizon]),
        published_at=cutoff + timedelta(minutes=4),
        freshness=freshness,
        status="scored" if score is not None else "abstained",
        score=score,
        risk_level=risk_level,
        channels=channels,
        facts=facts,
        versions=_versions(_run_id(target, horizon, cutoff)),
        **extra,
    )


def _history_facts(cutoff: datetime, last_loss: datetime, episodes: int) -> list[ForecastFact]:
    lookback = cutoff - timedelta(days=30)
    return [
        _fact(
            "last_connection_loss_at",
            "Последнее начало потери связи ≥ 1 ч",
            lookback,
            cutoff,
            at=last_loss,
        ),
        _fact(
            "episodes_30d",
            f"Эпизодов потери связи ≥ 1 ч за 30 сут: {episodes}",
            lookback,
            cutoff,
            number=episodes,
        ),
    ]


def legacy_current_cards() -> list[ForecastCard]:
    """Gate A sensor-failure examples (24 h / 168 h); used by contract rule tests only."""
    day = _at(24)
    week = _at(21)  # Monday cutoff of the weekly list
    lookback = day - timedelta(days=30)
    return [
        _card(
            "synthetic-forecast-24h-001",
            target=CONNECTION_LOSS,
            object_id="synthetic-object-01",
            sensor_type="Датчик дыма",
            system_type="Пожарная охрана",
            horizon="24h",
            cutoff=day,
            risk_level="high",
            score=_score(CONNECTION_LOSS, "24h", 1, 240, 10, 0.052, (1000, 40, 2000, 60)),
            channels=[
                ForecastChannel(
                    channel_id="synthetic-smoke-011",
                    channel_name="Синтетический датчик дыма 11",
                    engineering_tag="SYN-SD-011",
                    rank_in_card=1,
                    picket_form="point",
                    picket_from=12,
                    picket_basis="synthetic",
                    reason_facts=_history_facts(day, _at(22, 3, 10), 3),
                ),
                ForecastChannel(
                    channel_id="synthetic-smoke-012",
                    channel_name="Синтетический датчик дыма 12",
                    engineering_tag="SYN-SD-012",
                    rank_in_card=2,
                    picket_form="range",
                    picket_from=12,
                    picket_to=14,
                    picket_basis="synthetic",
                    reason_facts=_history_facts(day, _at(15, 17, 45), 1),
                ),
            ],
            active_episode_channel_ids=["synthetic-smoke-013"],
            facts=[
                _fact(
                    "object_mass_events_30d",
                    "Массовых событий объекта за 30 сут: 2 (до 4 каналов)",
                    lookback,
                    day,
                    number=2,
                ),
                _fact(
                    "calendar",
                    "В истории этого типа по четвергам доля начал 18%",
                    datetime(2019, 1, 31, tzinfo=MSK),
                    datetime(2025, 1, 1, tzinfo=MSK),
                    number=0.18,
                    unit="share",
                ),
                _fact(
                    "history_coverage",
                    "Покрытие истории объекта за 30 сут: 100%",
                    lookback,
                    day,
                    number=1.0,
                    unit="share",
                ),
            ],
        ),
        _card(
            "synthetic-forecast-24h-002",
            target=TECHNICAL_VALUE,
            object_id="synthetic-object-02",
            sensor_type="Датчик температуры",
            system_type="Температурная подсистема",
            horizon="24h",
            cutoff=day,
            risk_level="high",
            score=_score(TECHNICAL_VALUE, "24h", 1, 60, 10, 0.021, (1000, 15, 2000, 20)),
            channels=[
                ForecastChannel(
                    channel_id="synthetic-temp-021",
                    channel_name="Синтетический датчик температуры 21",
                    engineering_tag="SYN-TT-021",
                    rank_in_card=1,
                    picket_form="point",
                    picket_from=3,
                    picket_basis="synthetic",
                    reason_facts=[
                        _fact(
                            "last_technical_value_at",
                            "Последнее техническое значение (код -127)",
                            lookback,
                            day,
                            at=_at(23, 14, 5),
                        ),
                        _fact(
                            "technical_values_7d",
                            "Технических значений за 7 сут: 4",
                            day - timedelta(days=7),
                            day,
                            number=4,
                        ),
                    ],
                )
            ],
            facts=[
                _fact(
                    "temperature_trend_7d",
                    "Медиана температуры за 7 сут изменилась на +6,5 °C",
                    day - timedelta(days=7),
                    day,
                    number=6.5,
                    unit="°C",
                )
            ],
        ),
        _card(
            "synthetic-forecast-24h-003",
            target=CONNECTION_LOSS,
            object_id="synthetic-object-03",
            sensor_type="Газовый датчик",
            system_type="Газовая охрана",
            horizon="24h",
            cutoff=day,
            risk_level="medium",
            score=_score(CONNECTION_LOSS, "24h", 7, 240, 10, 0.018, (1000, 20, 2000, 30)),
            channels=[
                ForecastChannel(
                    channel_id="synthetic-gas-031",
                    channel_name="Синтетический газовый датчик 31",
                    rank_in_card=1,
                    picket_form="unknown",
                    reason_facts=[
                        _fact(
                            "episodes_7d",
                            "Эпизодов потери связи ≥ 1 ч за 7 сут: 2",
                            day - timedelta(days=7),
                            day,
                            number=2,
                        ),
                        *_history_facts(day, _at(22, 20, 30), 2)[:1],
                    ],
                )
            ],
            facts=[],
            continuation_of="synthetic-forecast-24h-p03",
        ),
        _card(
            "synthetic-forecast-24h-004",
            target=CONNECTION_LOSS,
            object_id="synthetic-object-04",
            sensor_type="Датчик дыма",
            system_type="Пожарная охрана",
            horizon="24h",
            cutoff=day,
            risk_level="unknown",
            score=None,
            channels=[
                ForecastChannel(
                    channel_id="synthetic-smoke-041",
                    channel_name="Синтетический датчик дыма 41",
                    rank_in_card=1,
                    picket_form="point",
                    picket_from=7,
                    picket_basis="synthetic",
                ),
                ForecastChannel(
                    channel_id="synthetic-smoke-042",
                    channel_name="Синтетический датчик дыма 42",
                    rank_in_card=2,
                    picket_form="unknown",
                ),
            ],
            facts=[
                _fact(
                    "history_coverage",
                    "История объекта: 12 сут из 30 требуемых",
                    lookback,
                    day,
                    number=0.4,
                    unit="share",
                )
            ],
            abstention_reason="insufficient_history",
            abstention_detail="Прогноз не выдан: история меньше 30 сут.",
            freshness=_freshness(day, history_days=12),
        ),
        _card(
            "synthetic-forecast-168h-001",
            target=CONNECTION_LOSS,
            object_id="synthetic-object-05",
            sensor_type="Состояние фазы",
            system_type="Диспетчерский контроль",
            horizon="168h",
            cutoff=week,
            risk_level="medium",
            score=_score(CONNECTION_LOSS, "168h", 12, 400, 50, 0.061, (1000, 50, 2000, 80)),
            channels=[
                ForecastChannel(
                    channel_id="synthetic-phase-051",
                    channel_name="Синтетическое состояние фазы 51",
                    rank_in_card=1,
                    picket_form="range",
                    picket_from=40,
                    picket_to=41,
                    picket_basis="synthetic",
                    reason_facts=_history_facts(week, _at(2, 11, 0), 5),
                )
            ],
            facts=[],
        ),
    ]


def _legacy_past_cards() -> list[ForecastCard]:
    first, second = _at(22), _at(23)
    return [
        _card(
            "synthetic-forecast-24h-p01",
            target=CONNECTION_LOSS,
            object_id="synthetic-object-01",
            sensor_type="Датчик дыма",
            system_type="Пожарная охрана",
            horizon="24h",
            cutoff=first,
            risk_level="high",
            score=_score(CONNECTION_LOSS, "24h", 2, 236, 10, 0.047, (1000, 40, 2000, 60)),
            channels=[
                ForecastChannel(
                    channel_id="synthetic-smoke-011",
                    rank_in_card=1,
                    picket_form="point",
                    picket_from=12,
                    picket_basis="synthetic",
                    reason_facts=_history_facts(first, _at(20, 5, 0), 2),
                )
            ],
            facts=[],
        ),
        _card(
            "synthetic-forecast-24h-p02",
            target=TECHNICAL_VALUE,
            object_id="synthetic-object-06",
            sensor_type="Датчик температуры",
            system_type="Температурная подсистема",
            horizon="24h",
            cutoff=first,
            risk_level="high",
            score=_score(TECHNICAL_VALUE, "24h", 3, 58, 10, 0.019, (1000, 15, 2000, 20)),
            channels=[
                ForecastChannel(
                    channel_id="synthetic-temp-061",
                    rank_in_card=1,
                    picket_form="unknown",
                    reason_facts=[
                        _fact(
                            "technical_values_7d",
                            "Технических значений за 7 сут: 2",
                            first - timedelta(days=7),
                            first,
                            number=2,
                        )
                    ],
                )
            ],
            facts=[],
        ),
        _card(
            "synthetic-forecast-24h-p03",
            target=CONNECTION_LOSS,
            object_id="synthetic-object-03",
            sensor_type="Газовый датчик",
            system_type="Газовая охрана",
            horizon="24h",
            cutoff=second,
            risk_level="medium",
            score=_score(CONNECTION_LOSS, "24h", 9, 238, 10, 0.016, (1000, 20, 2000, 30)),
            channels=[
                ForecastChannel(
                    channel_id="synthetic-gas-031",
                    rank_in_card=1,
                    picket_form="unknown",
                    reason_facts=_history_facts(second, _at(22, 20, 30), 1),
                )
            ],
            facts=[],
        ),
        _card(
            "synthetic-forecast-24h-p04",
            target=CONNECTION_LOSS,
            object_id="synthetic-object-07",
            sensor_type="Датчик дыма",
            system_type="Пожарная охрана",
            horizon="24h",
            cutoff=second,
            risk_level="unknown",
            score=None,
            channels=[
                ForecastChannel(
                    channel_id="synthetic-smoke-071",
                    rank_in_card=1,
                    picket_form="point",
                    picket_from=2,
                    picket_basis="synthetic",
                )
            ],
            facts=[
                _fact(
                    "history_coverage",
                    "История объекта: 20 сут из 30 требуемых",
                    second - timedelta(days=30),
                    second,
                    number=0.67,
                    unit="share",
                )
            ],
            abstention_reason="insufficient_history",
            abstention_detail="Прогноз не выдан: история меньше 30 сут.",
            freshness=_freshness(second, history_days=20),
        ),
        _card(
            "synthetic-forecast-24h-p05",
            target=TECHNICAL_VALUE,
            object_id="synthetic-object-08",
            sensor_type="Датчик температуры",
            system_type="Температурная подсистема",
            horizon="24h",
            cutoff=second,
            risk_level="unknown",
            score=None,
            channels=[
                ForecastChannel(
                    channel_id="synthetic-temp-081",
                    rank_in_card=1,
                    picket_form="unknown",
                )
            ],
            facts=[],
            abstention_reason="stale_or_missing_input",
            abstention_detail="Прогноз не выдан: данные источника старше 48 ч.",
            freshness=ForecastFreshness(
                data_as_of=second - timedelta(days=2),
                last_object_record_at=second - timedelta(days=2, minutes=5),
                lookback_days=30,
                history_days_available=30,
                coverage="partial",
            ),
        ),
    ]


def _observed_overlay() -> ObservedOverlay:
    """Records seen after issue for card 002; shown beside the card, not inside it."""
    return ObservedOverlay(
        checked_at=_at(24, 9, 55),
        items=[
            ObservedOverlayItem(
                channel_id="synthetic-temp-021",
                row_uid="synthetic-row-2101",
                observation_kind="target_candidate",
                value_raw="-127",
                alarm=False,
                event_at=_at(24, 6, 40),
                available_at=_at(24, 6, 41),
            ),
            ObservedOverlayItem(
                channel_id="synthetic-temp-021",
                row_uid="synthetic-row-2102",
                observation_kind="source_alarm",
                value_raw="Не определено",
                alarm=True,
                event_at=_at(24, 6, 40),
                available_at=_at(24, 6, 41),
            ),
        ],
    )


def legacy_current_views() -> list[ForecastCardView]:
    overlays = {"synthetic-forecast-24h-002": _observed_overlay()}
    return [
        ForecastCardView(
            card=card,
            already_observed=card.id in overlays,
            observed_overlay=overlays.get(card.id),
        )
        for card in legacy_current_cards()
    ]


def _decision(code, reason_code, reason_text, decided_at, **extra) -> ForecastDecisionSummary:
    return ForecastDecisionSummary(
        decision_code=code,
        reason_code=reason_code,
        reason_text=reason_text,
        dictionary_version="synthetic-technologist-dictionary-v0",
        actor_id="synthetic-dispatcher-01",
        actor_role="dispatcher_ods",
        decided_at=decided_at,
        revision=1,
        simulated=True,
        **extra,
    )


def legacy_journal_entries() -> list[ForecastJournalEntry]:
    """Gate A examples of all outcomes; used by contract rule tests only."""
    current = {card.id: card for card in legacy_current_cards()}
    past = {card.id: card for card in _legacy_past_cards()}
    pending = ForecastOutcome(status="pending", label_version=LABEL_VERSION)
    pending_abstained = pending.model_copy(update={"excluded_from_quality_metrics": True})

    rows: list[tuple[ForecastCard, ForecastOutcome, ForecastDecisionSummary | None]] = [
        (current["synthetic-forecast-168h-001"], pending, None),
        (
            past["synthetic-forecast-24h-p01"],
            ForecastOutcome(
                status="realized",
                label_version=LABEL_VERSION,
                resolved_at=_at(23, 0, 30),
                first_event_at=_at(22, 11, 24),
                lead_hours=11.4,
                event_channel_ids=["synthetic-smoke-011"],
                event_cluster_size=3,
                other_events_on_object_count=0,
            ),
            _decision("R2", "repeat_channel_episodes", "повторные эпизоды канала", _at(22, 8, 15)),
        ),
        (
            past["synthetic-forecast-24h-p02"],
            ForecastOutcome(
                status="not_realized",
                label_version=LABEL_VERSION,
                resolved_at=_at(23, 0, 30),
                other_events_on_object_count=1,
                intervention_before_window_end=True,
            ),
            _decision(
                "R3",
                "technical_values",
                "технические значения",
                _at(22, 9, 0),
                draft_id="synthetic-draft-001",
                draft_status="not_sent",
                check_result_code="O3",
                check_result_simulated=True,
            ),
        ),
        (
            past["synthetic-forecast-24h-p03"],
            ForecastOutcome(
                status="unknown",
                label_version=LABEL_VERSION,
                resolved_at=_at(24, 0, 30),
                unknown_reason="source_coverage",
            ),
            None,
        ),
        (
            past["synthetic-forecast-24h-p04"],
            ForecastOutcome(
                status="event_without_forecast",
                label_version=LABEL_VERSION,
                resolved_at=_at(24, 0, 30),
                first_event_at=_at(23, 15, 10),
                event_channel_ids=["synthetic-smoke-071"],
                event_cluster_size=1,
                other_events_on_object_count=0,
                excluded_from_quality_metrics=True,
            ),
            None,
        ),
        (
            past["synthetic-forecast-24h-p05"],
            ForecastOutcome(
                status="no_event_without_forecast",
                label_version=LABEL_VERSION,
                resolved_at=_at(24, 0, 30),
                other_events_on_object_count=0,
                excluded_from_quality_metrics=True,
            ),
            None,
        ),
        (
            current["synthetic-forecast-24h-001"],
            pending,
            _decision(
                "R3",
                "repeat_connection_loss",
                "устойчивая или повторная потеря связи",
                _at(24, 8, 30),
                draft_id="synthetic-draft-002",
                draft_status="not_sent",
            ),
        ),
        (current["synthetic-forecast-24h-002"], pending, None),
        (current["synthetic-forecast-24h-003"], pending, None),
        (current["synthetic-forecast-24h-004"], pending_abstained, None),
    ]
    return [
        ForecastJournalEntry(
            journal_position=position,
            card=card,
            outcome=outcome,
            decision=decision,
        )
        for position, (card, outcome, decision) in enumerate(rows, start=1)
    ]


# --- Served scenario: «обесточивание электрооборудования объекта», 14-day list, K = 10 --

# Synthetic frequency table «k из n»: (events_from, events_to, n, k, level) by events of
# the object's phase feeders in the past 365 days.
_FREQUENCY_ROWS: list[tuple[int, int | None, int, int, str]] = [
    (40, None, 300, 255, "high"),
    (15, 39, 350, 245, "medium"),
    (0, 14, 280, 120, "low"),
]
# The fixture calls the product rule (``incident_list.recommend``, v5 of 29.09) under a
# synthetic policy name; synthetic schemes have no section switch.
RECOMMENDATION_POLICY = "synthetic-feeder-policy-v5"
_FEEDERS = [
    # (name, kind, picket form, from, to)
    ("Синт. ФРО1 (ГРО1-6) ПК12-ПК18", "lighting", "range", 12.0, 18.0),
    ("Синт. ФВ2 (В23) ПК21", "ventilation", "point", 21.0, None),
    ("Синт. ФАНС1 ПК24", "pumps", "point", 24.0, None),
    ("Синт. ОЗК1 ПК30", "ozk", "point", 30.0, None),
    ("Синт. резервная линия 3", "other", "unknown", None, None),
]


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    share = k / n
    denominator = 1 + z * z / n
    centre = (share + z * z / (2 * n)) / denominator
    half = z * math.sqrt(share * (1 - share) / n + z * z / (4 * n * n)) / denominator
    return math.floor((centre - half) * 1e4) / 1e4, math.ceil((centre + half) * 1e4) / 1e4


def _frequency(events: int) -> FrequencyBucket:
    for low, high, n, k, level in _FREQUENCY_ROWS:
        if low <= events and (high is None or events <= high):
            wilson_low, wilson_high = _wilson(k, n)
            return FrequencyBucket(
                table_version="synthetic-frequency-table-14d-v0",
                events_from=low,
                events_to=high,
                cards=n,
                positive_cards=k,
                share=round(k / n, 4),
                wilson_low=wilson_low,
                wilson_high=wilson_high,
                period_start=date(2023, 1, 1),
                period_end=date(2025, 1, 1),
                level=level,
                source_report=SOURCE_REPORT,
            )
    raise ValueError(f"no frequency bucket for {events} events")


def _feeder_versions(cutoff: datetime) -> ForecastVersions:
    return ForecastVersions(
        scorer="static_list",
        model_version="synthetic-static-list-365d-v0",
        feature_version="synthetic-object-feeder-events-365d-v0",
        label_version=FEEDER_LABEL_VERSION,
        policy_version=LIST_POLICIES[HORIZON].policy_version,
        recurrence_rule_version="synthetic-recurrence-rule-v0",
        release_id="synthetic-release-003",
        run_id=_run_id(FEEDERS, HORIZON, cutoff),
    )


def _feeder_channels(object_no: int, cutoff: datetime, events: int) -> list[ForecastChannel]:
    """Top feeders of the object by past episodes; facts about the past only."""
    year_ago = cutoff - timedelta(days=365)
    channels = []
    for index, (name, kind, form, low, high) in enumerate(_FEEDERS, start=1):
        episodes = max(events // (index + 1), 1)
        last = cutoff - timedelta(days=3 * index, hours=object_no % 7)
        channels.append(
            ForecastChannel(
                channel_id=f"synthetic-feeder-{object_no}{index:02d}",
                channel_name=name,
                engineering_tag=f"SYN-F-{object_no}-{index}",
                rank_in_card=index,
                picket_form=form,
                picket_from=low,
                picket_to=high,
                picket_basis=None if form == "unknown" else "synthetic",
                feeder_kind=kind,
                reason_facts=[
                    _fact(
                        "events_365d",
                        f"Потерь связи линии электропитания за 365 сут: {episodes}",
                        year_ago,
                        cutoff,
                        number=episodes,
                    ),
                    _fact(
                        "last_connection_loss_at",
                        "Последнее начало потери связи линии электропитания",
                        year_ago,
                        cutoff,
                        at=last,
                    ),
                ],
            )
        )
    return channels


def _feeder_card(
    object_no: int,
    cutoff: datetime,
    *,
    rank: int | None = None,
    ranked: int = 19,
    events: int = 0,
    recurrence: str | None = None,
    followup: tuple[int, int] = (0, 0),
    history_days: int = 900,
) -> ForecastCard:
    year_ago = cutoff - timedelta(days=365)
    scored = rank is not None
    extra: dict = {}
    if scored:
        bucket = _frequency(events)
        score = ForecastScore(
            kind="frequency_share",
            label="доля k из n",
            value=bucket.share,
            rank=rank,
            ranked_cards=ranked,
            card_budget=LIST_POLICIES[HORIZON].max_open,
            frequency=bucket,
        )
        facts = [
            _fact(
                "events_365d",
                f"Событий потери связи линий электропитания объекта за 365 сут: {events}",
                year_ago,
                cutoff,
                number=events,
            ),
            _fact(
                "power_off_followup",
                "После потери связи линия электропитания «Обесточен» в течение 10 мин: "
                f"{followup[0]} из {followup[1]}",
                year_ago,
                cutoff,
                number=round(followup[0] / followup[1], 4),
                unit="share",
            ),
        ]
        extra.update(
            recurrence=recurrence,
            channels=_feeder_channels(object_no, cutoff, events),
            channels_total=len(_FEEDERS),
        )
        risk_level: RiskLevel = score.level
    else:
        score = None
        risk_level = "unknown"
        facts = [
            _fact(
                "history_coverage",
                f"История объекта: {history_days} сут из 365 требуемых",
                year_ago,
                cutoff,
                number=round(history_days / 365, 4),
                unit="share",
            )
        ]
        extra.update(
            channels=[],
            abstention_reason="insufficient_history",
            abstention_detail="Прогноз не выдан: история объекта меньше 365 сут.",
        )
    return ForecastCard(
        id=f"synthetic-feeders-14d-{object_no:03d}",
        mode="fixture",
        target_spec_id=FEEDERS,
        target_label=TARGET_LABELS[FEEDERS],
        object_id=f"synthetic-object-{object_no}",
        sensor_type=PHASE,
        system_type=DISPATCH_SYSTEM,
        horizon=HORIZON,
        issued_at=cutoff,
        window_start=cutoff,
        window_end=cutoff + timedelta(hours=HORIZON_HOURS[HORIZON]),
        published_at=cutoff + timedelta(minutes=4),
        freshness=ForecastFreshness(
            data_as_of=cutoff - timedelta(seconds=30),
            last_object_record_at=cutoff - timedelta(minutes=2),
            lookback_days=365,
            history_days_available=history_days,
            coverage="complete" if history_days >= 365 else "insufficient",
        ),
        status="scored" if scored else "abstained",
        score=score,
        risk_level=risk_level,
        facts=facts,
        versions=_feeder_versions(cutoff),
        **extra,
    )


# (object, issue day in September, rank, events_365d, recurrence, «Обесточен» k/n)
_OPEN = [
    (11, 11, 1, 64, "chronic", (58, 64)),
    (12, 13, 2, 52, "chronic", (47, 52)),
    (13, 15, 1, 47, "chronic", (41, 47)),
    (14, 16, 3, 41, "chronic", (35, 41)),
    (15, 18, 2, 38, "chronic", (31, 38)),
    (16, 19, 4, 33, "chronic", (29, 33)),
    (17, 21, 1, 29, "chronic", (22, 29)),
    (18, 23, 2, 22, "fresh", (17, 22)),
    (19, 24, 1, 19, "fresh", (15, 19)),
    (20, 24, 2, 17, "fresh", (12, 17)),
]
RELEASED_ID = "synthetic-feeders-14d-021"
_RELEASED_AT = _at(23, 11, 40)
EXPIRED_ID = "synthetic-feeders-14d-022"
UNKNOWN_ID = "synthetic-feeders-14d-023"
ABSTAINED_ID = "synthetic-feeders-14d-024"


def _feeder_cards() -> dict[str, ForecastCard]:
    cards = [
        _feeder_card(23, _at(9), rank=5, events=26, recurrence="chronic", followup=(20, 26)),
        _feeder_card(22, _at(10), rank=4, events=31, recurrence="chronic", followup=(27, 31)),
        _feeder_card(21, _at(12), rank=1, events=71, recurrence="chronic", followup=(66, 71)),
        *(
            _feeder_card(
                number, _at(day), rank=rank, events=events, recurrence=kind, followup=followup
            )
            for number, day, rank, events, kind, followup in _OPEN
        ),
        _feeder_card(24, _at(24), history_days=200),
    ]
    return {card.id: card for card in cards}


def _list_state(card: ForecastCard, as_of: datetime) -> tuple[str | None, datetime | None]:
    if card.status == "abstained":
        return None, None
    if card.id == RELEASED_ID and _RELEASED_AT <= as_of:
        return "released", _RELEASED_AT
    return ("expired" if card.window_end <= as_of else "open"), None


@cache
def _pair_cards() -> dict[tuple[str | None, str], tuple[il.PriorCard, ...]]:
    """Synthetic cards of each pair as the recommendation sees them (issue, status, release)."""
    pairs: dict[tuple[str | None, str], list[il.PriorCard]] = {}
    for other in _feeder_cards().values():
        pairs.setdefault((other.object_id, other.target_spec_id), []).append(
            il.PriorCard(
                issued_at=other.issued_at,
                scored=other.status == "scored",
                release_at=_RELEASED_AT if other.id == RELEASED_ID else None,
            )
        )
    return {key: tuple(value) for key, value in pairs.items()}


def _recommendation(card: ForecastCard) -> ForecastRecommendation:
    """The product rule over the card lines and the synthetic cards of its pair (repeat)."""
    rule = il.recommend(
        card.status == "scored",
        [(channel.channel_name, channel.feeder_kind) for channel in card.channels],
        repeat=il.repeat_at(
            card.issued_at, _pair_cards().get((card.object_id, card.target_spec_id), ())
        ),
    )
    return ForecastRecommendation(
        text=rule.text,
        details=list(rule.details),
        rule_ids=list(rule.rule_ids),
        policy_version=RECOMMENDATION_POLICY,
    )


def _view(card: ForecastCard, as_of: datetime = FIXTURE_AS_OF) -> ForecastCardView:
    state, released_at = _list_state(card, as_of)
    days_total = HORIZON_HOURS[card.horizon] // 24
    day_index = None
    if state == "open":
        day_index = min((as_of - card.issued_at) // timedelta(days=1) + 1, days_total)
    return ForecastCardView(
        card=card,
        list_state=state,
        released_at=released_at,
        day_index=day_index,
        days_total=days_total if day_index is not None else None,
        recommendation=_recommendation(card),
    )


def current_cards() -> list[ForecastCard]:
    """Scored cards open at ``FIXTURE_AS_OF`` in the rolling list."""
    return [view.card for view in current_views()]


def current_views(list_state: ListState = "open") -> list[ForecastCardView]:
    views = [_view(card) for card in _feeder_cards().values()]
    return [view for view in views if view.list_state == list_state]


_NOTIFIED_TO = "дежурный энергетик эксплуатирующей организации (синтетика)"


def _feeder_decision(code, reason_code, reason_text, decided_at, methods, **extra):
    notified = (
        {"notified_to": _NOTIFIED_TO, "notified_at": decided_at - timedelta(minutes=5)}
        if code == "R3"
        else {}
    )
    return ForecastDecisionSummary(
        decision_code=code,
        reason_code=reason_code,
        reason_text=reason_text,
        dictionary_version="synthetic-technologist-dictionary-v0",
        actor_id="synthetic-dispatcher-01",
        actor_role="dispatcher",
        decided_at=decided_at,
        revision=1,
        simulated=True,
        verification_methods=methods,
        **notified,
        **extra,
    )


def journal_entries() -> list[ForecastJournalEntry]:
    """Append-only synthetic journal of the served scenario, in issue order."""
    cards = _feeder_cards()
    pending = ForecastOutcome(status="pending", label_version=FEEDER_LABEL_VERSION)
    released = cards[RELEASED_ID]
    first_event = _RELEASED_AT
    lead = round((first_event - released.issued_at).total_seconds() / 3600, 2)
    event_channels = [channel.channel_id for channel in released.channels[:2]]
    outcomes: dict[str, ForecastOutcome] = {
        EXPIRED_ID: ForecastOutcome(
            status="not_realized",
            label_version=FEEDER_LABEL_VERSION,
            resolved_at=_at(24, 0, 30),
            other_events_on_object_count=0,
        ),
        UNKNOWN_ID: ForecastOutcome(
            status="unknown",
            label_version=FEEDER_LABEL_VERSION,
            resolved_at=_at(23, 0, 30),
            unknown_reason="source_coverage",
        ),
        RELEASED_ID: ForecastOutcome(
            status="realized",
            label_version=FEEDER_LABEL_VERSION,
            resolved_at=first_event + timedelta(minutes=1),
            first_event_at=first_event,
            lead_hours=lead,
            event_channel_ids=event_channels,
            event_cluster_size=2,
            other_events_on_object_count=0,
            power_off_at=first_event + timedelta(minutes=10),
        ),
        ABSTAINED_ID: pending.model_copy(update={"excluded_from_quality_metrics": True}),
    }
    decisions = {
        RELEASED_ID: _feeder_decision(
            "R3",
            "R3.1",
            "устойчивая или повторная потеря связи линий электропитания",
            _at(22, 9, 10),
            ["source_records", "remote_poll"],
            draft_id="synthetic-draft-021",
            draft_status="not_sent",
            awaiting_result_until=_at(24, 17, 0),
        ),
        "synthetic-feeders-14d-011": _feeder_decision(
            "R3",
            "R3.1",
            "устойчивая или повторная потеря связи линий электропитания",
            _at(11, 8, 30),
            ["source_records"],
            draft_id="synthetic-draft-011",
            draft_status="not_sent",
            awaiting_result_until=_at(13, 17, 0),
        ),
        "synthetic-feeders-14d-012": _feeder_decision(
            "R1",
            "R1.1",
            "действий сейчас не требуется",
            _at(13, 9, 15),
            ["source_records", "remote_poll"],
            watch_until=_at(20, 9, 0),
        ),
    }
    events = {
        RELEASED_ID: [
            ForecastWindowEvent(
                event_id="synthetic-event-2101",
                started_at=first_event,
                channel_ids=event_channels,
                cluster_size=2,
                while_open=True,
                power_off_at=first_event + timedelta(minutes=10),
            ),
            ForecastWindowEvent(
                event_id="synthetic-event-2102",
                started_at=_at(24, 6, 10),
                channel_ids=[released.channels[0].channel_id],
                cluster_size=1,
                while_open=False,
            ),
        ]
    }
    rows = []
    ordered = sorted(cards.values(), key=lambda card: (card.issued_at, card.id))
    results = _check_results()
    for position, card in enumerate(ordered, start=1):
        state, released_at = _list_state(card, FIXTURE_AS_OF)
        rows.append(
            ForecastJournalEntry(
                journal_position=position,
                card=card,
                outcome=outcomes.get(card.id, pending),
                decision=decisions.get(card.id),
                list_state=state,
                released_at=released_at,
                events=events.get(card.id, []),
                check_result=results.get(card.id),
            )
        )
    return rows


def _check_results() -> dict[str, ForecastCheckResult]:
    """Synthetic check results; «found» is never confirmed by the customer."""
    return {
        RELEASED_ID: ForecastCheckResult(
            forecast_id=RELEASED_ID,
            revision=1,
            check_result="fixed",
            found=["breaker"],
            result_at=_at(23, 16, 0),
            comment="Синтетика: энергетик сообщил о сработавшем автомате линии освещения",
            event_cause="protection_trip",
            actor_id="synthetic-dispatcher-01",
            actor_role="dispatcher",
            recorded_at=_at(23, 16, 5),
            simulated=True,
        ),
        "synthetic-feeders-14d-011": ForecastCheckResult(
            forecast_id="synthetic-feeders-14d-011",
            revision=1,
            check_result="awaiting",
            result_at=_at(11, 9, 0),
            actor_id="synthetic-dispatcher-01",
            actor_role="dispatcher",
            recorded_at=_at(11, 9, 0),
            simulated=True,
        ),
    }


def check_result_list(forecast_id: str) -> ForecastCheckResultList | None:
    if forecast_id not in _feeder_cards():
        return None
    found = _check_results().get(forecast_id)
    return ForecastCheckResultList(forecast_id=forecast_id, items=[found] if found else [])


def _parse_cursor(cursor: str | None, pattern: str) -> tuple[str, int] | None:
    if cursor is None:
        return None
    match = re.fullmatch(rf"({pattern})\.(\d{{1,9}})", cursor)
    if match is None:
        raise ForecastCursorError("invalid_forecast_cursor")
    return match.group(1), int(match.group(2))


def published_runs() -> list[PublishedRun]:
    latest = _at(24)
    return [
        PublishedRun(
            target_spec_id=FEEDERS,
            horizon=HORIZON,
            run_id=_run_id(FEEDERS, HORIZON, latest),
            generation=RUN_GENERATIONS[(FEEDERS, HORIZON)],
            issued_at=latest,
            published_at=latest + timedelta(minutes=4),
            list_policy=LIST_POLICIES[HORIZON],
        )
    ]


def publication_token(runs: list[PublishedRun]) -> str:
    """Opaque token over every published target x horizon run and its generation."""
    material = "|".join(
        f"{run.target_spec_id}:{run.horizon}:{run.run_id}:{run.generation}" for run in runs
    )
    return "pt-" + hashlib.sha256(material.encode()).hexdigest()[:16]


def _object_matches(card_object_id: str | None, wanted: str | None) -> bool:
    if wanted is None:
        return True
    return card_object_id is None if wanted == "__unknown__" else card_object_id == wanted


def forecast_list(
    *,
    horizon: Horizon | None = None,
    target_spec_id: TargetSpecId | None = None,
    risk_level: RiskLevel | None = None,
    status: Literal["scored", "abstained"] | None = None,
    sensor_type: str | None = None,
    object_id: str | None = None,
    list_state: ListState = "open",
    released_since: datetime | None = None,
    cursor: str | None = None,
    limit: int = 25,
) -> ForecastList:
    runs = published_runs()
    token = publication_token(runs)
    offset = 0
    parsed = _parse_cursor(cursor, r"pt-[0-9a-f]{16}")
    if parsed is not None:
        cursor_token, offset = parsed
        if cursor_token != token:
            raise StaleForecastCursor("forecast_cursor_stale")
    views = [
        view
        for view in current_views(list_state)
        if (horizon is None or view.card.horizon == horizon)
        and (
            released_since is None
            or (view.released_at is not None and view.released_at >= released_since)
        )
        and (target_spec_id is None or view.card.target_spec_id == target_spec_id)
        and (risk_level is None or view.card.risk_level == risk_level)
        and (status is None or view.card.status == status)
        and (sensor_type is None or view.card.sensor_type == sensor_type)
        and _object_matches(view.card.object_id, object_id)
    ]
    views.sort(
        key=lambda view: (
            _HORIZON_ORDER[view.card.horizon],
            view.card.status != "scored",
            view.card.issued_at,
            view.card.score.rank if view.card.score else 0,
            view.card.id,
        )
    )
    page = views[offset : offset + limit]
    end = offset + len(page)
    return ForecastList(
        mode="fixture",
        as_of=FIXTURE_AS_OF,
        publication_token=token,
        runs=runs,
        items=page,
        total=len(views),
        limit=limit,
        next_cursor=f"{token}.{end}" if end < len(views) else None,
    )


def forecast_card(forecast_id: str) -> ForecastCardView | None:
    """Card with its list state at ``FIXTURE_AS_OF`` (open, released, expired or abstained)."""
    card = _feeder_cards().get(forecast_id)
    return None if card is None else _view(card)


def forecast_journal(
    *,
    outcome: OutcomeStatus | None = None,
    horizon: Horizon | None = None,
    target_spec_id: TargetSpecId | None = None,
    object_id: str | None = None,
    list_state: ListState | None = None,
    issued_from: date | None = None,
    issued_to: date | None = None,
    decision_state: Literal["none", "any"] | None = None,
    cursor: str | None = None,
    limit: int = 25,
) -> ForecastJournalList:
    entries = journal_entries()
    watermark, offset = len(entries), 0
    parsed = _parse_cursor(cursor, r"w\d{1,9}")
    if parsed is not None:
        watermark, offset = int(parsed[0][1:]), parsed[1]
        if watermark > len(entries):
            raise ForecastCursorError("journal_watermark_out_of_range")
    selected = [
        entry
        for entry in entries
        if entry.journal_position <= watermark
        and (outcome is None or entry.outcome.status == outcome)
        and (horizon is None or entry.card.horizon == horizon)
        and (target_spec_id is None or entry.card.target_spec_id == target_spec_id)
        and _object_matches(entry.card.object_id, object_id)
        and (list_state is None or entry.list_state == list_state)
        and (issued_from is None or entry.card.issued_at.astimezone(MSK).date() >= issued_from)
        and (issued_to is None or entry.card.issued_at.astimezone(MSK).date() <= issued_to)
        and (decision_state is None or (entry.decision is None) == (decision_state == "none"))
    ]
    selected.sort(key=lambda entry: entry.journal_position, reverse=True)
    page = selected[offset : offset + limit]
    end = offset + len(page)
    return ForecastJournalList(
        mode="fixture",
        as_of=FIXTURE_AS_OF,
        journal_watermark=watermark,
        items=page,
        total=len(selected),
        limit=limit,
        next_cursor=f"w{watermark}.{end}" if end < len(selected) else None,
    )


def decision_list(forecast_id: str) -> ForecastDecisionList | None:
    """Decision revisions of a card (one synthetic revision at most)."""
    if forecast_id not in _feeder_cards():
        return None
    items = [
        entry.decision
        for entry in journal_entries()
        if entry.card.id == forecast_id and entry.decision is not None
    ]
    return ForecastDecisionList(forecast_id=forecast_id, items=items)


def forecast_state() -> ForecastState:
    run = published_runs()[0]
    views = current_views()
    released = [view for view in current_views("released") if view.released_at is not None]
    return ForecastState(
        mode="fixture",
        checked_at=FIXTURE_AS_OF,
        generation=RUN_GENERATIONS[(FEEDERS, HORIZON)],
        data_as_of=_at(24) - timedelta(seconds=30),
        published_at=run.published_at,
        horizons=[
            HorizonState(
                target_spec_id=FEEDERS,
                horizon=HORIZON,
                run_id=run.run_id,
                issued_at=run.issued_at,
                open_cards=len(views),
                new_cards=sum(1 for view in views if view.card.issued_at == run.issued_at),
                released_since_previous=sum(
                    1 for view in released if view.released_at >= run.issued_at - timedelta(days=1)
                ),
                max_open=LIST_POLICIES[HORIZON].max_open,
            )
        ],
        last_import_id="synthetic-import-001",
        last_import_status="published",
        source_messages_as_of=FIXTURE_AS_OF - timedelta(minutes=5),
    )


def _week_groups() -> list[tuple[date, list[ForecastJournalEntry]]]:
    weeks: dict[date, list[ForecastJournalEntry]] = {}
    for entry in journal_entries():
        if entry.card.status != "scored":
            continue
        issued = entry.card.issued_at.astimezone(MSK).date()
        weeks.setdefault(issued - timedelta(days=issued.weekday()), []).append(entry)
    return sorted(weeks.items())


def journal_counts() -> ForecastJournalCounts:
    """Dispatcher counters by issue week: выдано / снята / без события / неизвестно / открыто."""
    rows = []
    for start, group in _week_groups():
        states = [entry.list_state for entry in group]
        statuses = [entry.outcome.status for entry in group]
        rows.append(
            ForecastJournalCountsRow(
                period_start=start,
                period_end=start + timedelta(days=7),
                target_spec_id=FEEDERS,
                horizon=HORIZON,
                cards_issued=len(group),
                cards_released=states.count("released"),
                cards_no_event=statuses.count("not_realized"),
                cards_unknown=statuses.count("unknown"),
                cards_open=states.count("open"),
            )
        )
    return ForecastJournalCounts(mode="fixture", as_of=FIXTURE_AS_OF, rows=rows)


def _ratio(name: str, numerator: int, denominator: int, version: str) -> RatioMetric:
    return RatioMetric(
        name=name,
        definition_version=version,
        numerator=numerator,
        denominator=denominator,
        value=round(numerator / denominator, 4) if denominator else None,
    )


def quality_summary() -> ForecastQualitySummary:
    """Research-side weekly «прогноз против факта» with ratios (analysts only)."""
    rows = []
    for start, group in _week_groups():
        counts = {status: 0 for status in ("pending", "realized", "not_realized", "unknown")}
        for entry in group:
            counts[entry.outcome.status] += 1
        events = [event for entry in group for event in entry.events]
        metrics = []
        if events:
            metrics.append(
                _ratio(
                    "events_with_open_card",
                    sum(event.while_open for event in events),
                    len(events),
                    "synthetic-event-metric-v0",
                )
            )
        rows.append(
            ForecastQualityRow(
                period_start=start,
                period_end=start + timedelta(days=7),
                target_spec_id=FEEDERS,
                horizon=HORIZON,
                cards_issued=len(group),
                cards_pending=counts["pending"],
                cards_realized=counts["realized"],
                cards_not_realized=counts["not_realized"],
                cards_unknown=counts["unknown"],
                card_share=_ratio(
                    "card_share",
                    counts["realized"],
                    counts["realized"] + counts["not_realized"],
                    "synthetic-card-share-v0",
                ),
                event_metrics=metrics,
            )
        )
    return ForecastQualitySummary(
        mode="fixture", as_of=FIXTURE_AS_OF, evidence_scope="fixture", rows=rows
    )


def recurring_places() -> RecurringPlaceList:
    open_ids: dict[str, list[str]] = {}
    for card in current_cards():
        open_ids.setdefault(card.object_id, []).append(card.id)
    rows = [(11, 17, 64, _at(20, 10, 20)), (12, 14, 52, _at(17, 9, 5)), (21, 19, 71, _RELEASED_AT)]
    items = [
        RecurringPlace(
            object_id=f"synthetic-object-{number}",
            object_name=f"Синтетический объект {number}",
            sensor_type=PHASE,
            incident_type="feeder_power_loss",
            events_90d=events_90d,
            events_365d=events_365d,
            last_event_at=last_event,
            open_card_ids=open_ids.get(f"synthetic-object-{number}", []),
            rule_version="synthetic-recurrence-rule-v0",
        )
        for number, events_90d, events_365d, last_event in rows
    ]
    return RecurringPlaceList(mode="fixture", as_of=FIXTURE_AS_OF, items=items, total=len(items))


def _scheme_picket(form: str, low: float | None, high: float | None) -> SchemePicket:
    if form == "unknown":
        return SchemePicket(form="unknown")
    return SchemePicket(form=form, picket_from=low, picket_to=high, basis="synthetic")


def _scheme_objects() -> list[int]:
    return sorted({int(card.object_id.rsplit("-", 1)[1]) for card in _feeder_cards().values()})


def _current_alarms(object_no: int) -> dict[str, list[SchemeAlarm]]:
    """Object 19: an alarm record still in force on ФВ2 and one already cleared by «Норма»
    on ФРО1 (B-3: the scheme shows it as a past record of the day, not a current alarm)."""
    if object_no != 19:
        return {}
    channel = f"synthetic-feeder-{object_no}02"
    past = f"synthetic-feeder-{object_no}01"
    return {
        channel: [
            SchemeAlarm(
                row_uid="synthetic-row-1902",
                channel_id=channel,
                value_raw="Обесточен",
                event_at=_at(24, 8, 10),
            )
        ],
        past: [
            SchemeAlarm(
                row_uid="synthetic-row-1901",
                channel_id=past,
                value_raw="Отключено устройство",
                event_at=_at(24, 7, 20),
                cleared_at=_at(24, 7, 35),
                cleared_value_raw="Норма",
            )
        ],
    }


def _named_link(name: str) -> SchemeNamedLink | None:
    label = parse_named_link(name)
    return None if label is None else SchemeNamedLink(label=label)


def _channel_history(channel: ForecastChannel) -> tuple[int, datetime | None]:
    facts = {fact.kind: fact for fact in channel.reason_facts}
    episodes = facts["events_365d"].value_number
    return int(episodes or 0), facts["last_connection_loss_at"].value_at


def object_scheme(object_id: str) -> ObjectScheme | None:
    """Picket line of a synthetic object; landmarks and feeders are invented."""
    cards = [card for card in _feeder_cards().values() if card.object_id == object_id]
    if not cards:
        return None
    object_no = int(object_id.rsplit("-", 1)[1])
    alarms = _current_alarms(object_no)
    latest = max(cards, key=lambda card: (card.issued_at, card.id))
    card_facts = {channel.channel_id: _channel_history(channel) for channel in latest.channels}
    feeders = []
    for index, (name, kind, form, low, high) in enumerate(_FEEDERS, start=1):
        channel_id = f"synthetic-feeder-{object_no}{index:02d}"
        # Карточка и схема одного объекта показывают одни и те же числа по линии.
        episodes, last_at = card_facts.get(
            channel_id, (max(40 // (index + 1), 1), _at(20 - index, 10, 0))
        )
        feeders.append(
            SchemeFeeder(
                channel_id=channel_id,
                name=name,
                feeder_kind=kind,
                picket=_scheme_picket(form, low, high),
                episodes_365d=episodes,
                last_episode_at=last_at,
                current_alarms=alarms.get(channel_id, []),
                alarm_records_24h=len(alarms.get(channel_id, [])),
                named_link=_named_link(name),
            )
        )
    views = [_view(card) for card in cards]
    return ObjectScheme(
        mode="fixture",
        as_of=FIXTURE_AS_OF,
        object_id=object_id,
        object_name=f"Синтетический объект {object_no}",
        landmarks=[
            SchemeLandmark(
                name="Синтетический ввод 1",
                kind="input",
                picket=_scheme_picket("point", 10.0, None),
            ),
            SchemeLandmark(
                name="Синтетический АВР", kind="ats", picket=_scheme_picket("point", 11.0, None)
            ),
        ],
        feeders=feeders,
        open_card_ids=[view.card.id for view in views if view.list_state == "open"],
        released_7d=[
            SchemeReleasedCard(forecast_id=view.card.id, released_at=view.released_at)
            for view in views
            if view.released_at is not None
            and view.released_at >= FIXTURE_AS_OF - timedelta(days=7)
        ],
    )


def scheme_list() -> ObjectSchemeList:
    items = []
    for object_no in _scheme_objects():
        scheme = object_scheme(f"synthetic-object-{object_no}")
        known = [feeder for feeder in scheme.feeders if feeder.picket.form != "unknown"]
        span = [
            value
            for feeder in known
            for value in (feeder.picket.picket_from, feeder.picket.picket_to)
            if value is not None
        ] + [landmark.picket.picket_from for landmark in scheme.landmarks]
        items.append(
            ObjectSchemeSummary(
                object_id=scheme.object_id,
                object_name=scheme.object_name,
                picket_min=min(span),
                picket_max=max(span),
                feeders=len(scheme.feeders),
                feeders_without_picket=len(scheme.feeders) - len(known),
                open_cards=len(scheme.open_card_ids),
                current_alarms=sum(len(feeder.current_alarms) for feeder in scheme.feeders),
                released_7d=len(scheme.released_7d),
            )
        )
    return ObjectSchemeList(mode="fixture", as_of=FIXTURE_AS_OF, items=items, total=len(items))


def forecast_fixture_document() -> dict:
    """Checked-in ``contracts/forecast.fixture.json``: every forecast read in fixture mode."""
    return {
        "forecasts": forecast_list(limit=100).model_dump(mode="json"),
        "journal": forecast_journal(limit=100).model_dump(mode="json"),
        "journal_counts": journal_counts().model_dump(mode="json"),
        "quality": quality_summary().model_dump(mode="json"),
        "state": forecast_state().model_dump(mode="json"),
        "recurring": recurring_places().model_dump(mode="json"),
        "schemes": scheme_list().model_dump(mode="json"),
        "scheme_example": object_scheme("synthetic-object-19").model_dump(mode="json"),
    }
