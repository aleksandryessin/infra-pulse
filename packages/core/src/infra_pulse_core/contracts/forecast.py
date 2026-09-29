"""Forecast of a registered technical event, read-only slice and journal.

Two incident families share one card shape (object x sensor type x horizon x cutoff):

- ``sensor-failure/*`` — a registered technical event of a sensor channel (connection
  loss, technical value). Not a confirmed breakdown; the cause is known only after a
  field visit (OUT-04).
- ``phase-feeders/phase_loss_all`` — «обесточивание электрооборудования объекта»
  (C0.3; the ID keeps «feeders» for compatibility): every
  registered connection-loss episode («Неисправен») of «Состояние фазы» channels. 99.7% of
  these events consist of feeders only (lighting, ventilation, pumps); inputs, ATS and
  panels never report «Неисправен» (FINAL_V9, 27.09.2026). The cause is unknown. The card
  is per object (object x «Состояние фазы»); the production list is a static list over
  the pair's events in the past 365 days, issued as a 14-day rolling list that releases
  a card at its event.

Gate A (26.09.2026) accepted only a *risk estimate*. The static list shows the share
«k из n» of past cards in its frequency bucket (``frequency_share``); ``probability``
stays in the schema for a future ML-05 approval but is rejected unless a calibration
version is recorded **and** ``PROBABILITY_DISPLAY_APPROVED`` is switched on in review.

Recall definitions are not fixed here: the journal stores each card's outcome and the
events of its window, and summaries carry named ratios with their own definition
version.
"""

import math
import re
from datetime import date, timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    model_validator,
)

from infra_pulse_core.contracts.imports import ImportStatus

# ML-05 is partial: a calibrated "probability" label needs a separate approval and
# a reviewed contract change. Until then every scored card is a risk estimate.
PROBABILITY_DISPLAY_APPROVED: bool = False

EVENT_LABEL = "зарегистрированное техническое событие канала"
FEEDER_EVENT_LABEL = (
    "Линии питания освещения, вентиляции и насосов: сначала потеря связи („Неисправен“), "
    "через ~10 мин — „Обесточен“. Причина неизвестна"
)
# Pilot (C0.2), published after package B4: smoke and heat detectors of an object.
FIRE_PILOT_EVENT_LABEL = (
    "потеря связи дымового или теплового извещателя либо серия неподтверждённых самосбросов "
    "вне сессии ТО; причина неизвестна"
)

ForecastMode = Literal["fixture", "replay", "received", "live"]
Horizon = Literal["24h", "48h", "168h", "336h"]
HORIZON_HOURS: dict[str, int] = {"24h": 24, "48h": 48, "168h": 168, "336h": 336}
TargetSpecId = Literal[
    "sensor-failure/connection_loss_1h",
    "sensor-failure/technical_value",
    "phase-feeders/phase_loss_all",
    "fire-detectors/failure_pilot",
]
FEEDER_TARGET = "phase-feeders/phase_loss_all"
FIRE_PILOT_TARGET = "fire-detectors/failure_pilot"
TARGET_LABELS: dict[str, str] = {
    "sensor-failure/connection_loss_1h": "потеря связи ≥ 1 ч",
    "sensor-failure/technical_value": "техническое значение",
    FEEDER_TARGET: "обесточивание электрооборудования объекта",
    FIRE_PILOT_TARGET: "отказ пожарных извещателей (пилот)",
}
IncidentType = Literal[
    "sensor_connection_loss",
    "sensor_technical_value",
    "feeder_power_loss",
    "fire_detector_failure",
]
TARGET_INCIDENTS: dict[str, str] = {
    "sensor-failure/connection_loss_1h": "sensor_connection_loss",
    "sensor-failure/technical_value": "sensor_technical_value",
    FEEDER_TARGET: "feeder_power_loss",
    FIRE_PILOT_TARGET: "fire_detector_failure",
}
EVENT_LABELS: dict[str, str] = {
    "sensor-failure/connection_loss_1h": EVENT_LABEL,
    "sensor-failure/technical_value": EVENT_LABEL,
    FEEDER_TARGET: FEEDER_EVENT_LABEL,
    FIRE_PILOT_TARGET: FIRE_PILOT_EVENT_LABEL,
}
RiskLevel = Literal["low", "medium", "high", "unknown"]
BucketLevel = Literal["low", "medium", "high"]
AbstentionReason = Literal[
    "insufficient_history",
    "stale_or_missing_input",
    "lookback_in_excluded_period",
    "sensor_type_out_of_scope",
    "incompatible_bundle",
    "channel_disabled",
    "no_object_binding",
]
FactKind = Literal[
    "last_connection_loss_at",
    "last_technical_value_at",
    "episodes_7d",
    "episodes_30d",
    "events_365d",
    "technical_values_7d",
    "object_mass_events_30d",
    "calendar",
    "temperature_trend_7d",
    "history_coverage",
    "power_off_followup",
]
_FACT_TIME_KINDS = {"last_connection_loss_at", "last_technical_value_at"}
_FACT_COUNT_KINDS = {
    "episodes_7d",
    "episodes_30d",
    "events_365d",
    "technical_values_7d",
    "object_mass_events_30d",
}
# power_off_followup: share of past events followed by «Обесточен» within the rule window.
_FACT_SHARE_KINDS = {"calendar", "history_coverage", "power_off_followup"}
Recurrence = Literal["chronic", "fresh"]
# Kind of a phase feeder from its channel name; «other» when the name does not tell.
FeederKind = Literal["lighting", "ventilation", "pumps", "ozk", "other"]
FEEDER_KIND_LABELS: dict[str, str] = {
    "lighting": "освещение",
    "ventilation": "вентиляция",
    "pumps": "насосы",
    "ozk": "ОЗК",
    "other": "прочее",
}
# Where a picket comes from: reference data, parsed from the channel name, or synthetic.
PicketBasis = Literal["reference", "channel_name", "synthetic"]
ListState = Literal["open", "released", "expired"]
VerificationMethod = Literal[
    "source_records",
    "remote_poll",
    "call_collector",
    "video",
    "field_visit",
    "not_checked",
]
# Named ratio metrics (e.g. a recall variant) are free identifiers with their own
# definition version: the contract does not fix which recall is primary.
METRIC_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
# Scored cards: pending/realized/not_realized/unknown. Abstained cards never
# "realize": they use the *_without_forecast wording and are excluded from quality.
OutcomeStatus = Literal[
    "pending",
    "realized",
    "not_realized",
    "event_without_forecast",
    "no_event_without_forecast",
    "unknown",
]
_SCORED_OUTCOMES = {"pending", "realized", "not_realized", "unknown"}
_ABSTAINED_OUTCOMES = {"pending", "event_without_forecast", "no_event_without_forecast", "unknown"}
_EVENT_OUTCOMES = {"realized", "event_without_forecast"}
UnknownOutcomeReason = Literal["source_coverage", "policy_day", "data_end", "label_spec_changed"]
DecisionCode = Literal["R1", "R2", "R3", "R4", "R5", "R6", "R7"]
# Dispatcher labels of the codes (C0.4, technologist and designer 27.09). Codes are not
# renamed; only labels and rules change.
DECISION_LABELS: dict[str, str] = {
    "R1": "Под наблюдением",
    "R2": "Проверить удалённо",
    "R3": "Сообщено энергетику",
    "R4": "Включить объект в плановый осмотр до срока",
    "R5": "Уже известно / в работе",
    "R6": "Передать смене",
    "R7": "Нет оснований",
}
NOTIFIED_CODE = "R3"  # «Сообщено энергетику»: recipient, time and a result deadline
WATCH_CODE = "R1"  # «Под наблюдением»: until when
# Result of the check after a decision (C0.4). «found» is not confirmed by the customer.
CheckResultStatus = Literal["awaiting", "fixed", "no_violation", "not_done"]
CHECK_RESULT_LABELS: dict[str, str] = {
    "awaiting": "ждём результат",
    "fixed": "устранено",
    "no_violation": "нарушений не найдено",
    "not_done": "проверка не проведена",
}
FoundItem = Literal["breaker", "cable", "contactor", "comm_module", "cabinet_power", "other"]
FOUND_LABELS: dict[str, str] = {
    "breaker": "автомат",
    "cable": "кабель",
    "contactor": "контактор",
    "comm_module": "модуль связи",
    "cabinet_power": "питание шкафа",
    "other": "другое",
}
# Cause of a registered event (input for later retraining, ТЗ §12); only for a realized card.
EventCause = Literal[
    "planned_outage", "protection_trip", "external_grid", "smvu_channel", "unknown"
]
EVENT_CAUSE_LABELS: dict[str, str] = {
    "planned_outage": "плановое отключение",
    "protection_trip": "срабатывание защиты",
    "external_grid": "внешняя сеть",
    "smvu_channel": "канал связи СМВУ",
    "unknown": "неизвестно",
}
CheckResultCode = Literal["O1", "O2", "O3", "O4", "O5", "O6"]

# Technologist card, section 1: a card never states a diagnosis, a cause, an
# unapproved probability, an emergency or a healthy state. Applied to fact texts.
FORBIDDEN_CARD_WORDING = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"поломк",
        r"неисправност\w*\s+прибор",
        r"выйд\w*\s+из\s+строя",
        r"(выход\w*|вышл?\w*)\s+из\s+строя",
        r"требуется\s+замена",
        r"причин[аы]\s*[—–:-]",
        r"вероятност",
        r"критичн",
        # «аварийное освещение» is the normative term of СП 265.1325800.2016 (recommendation v5);
        # «авария», «аварийная ситуация», «аварийный выезд» stay forbidden.
        r"авари(?!йн\w*\s+освещени)",
        r"\bпожар\b",
        r"проникновени",
        r"\bнорма\b",
        r"\bисправ(ен|на|но|ны)\b",
    )
)


def check_card_wording(text: str) -> str:
    for pattern in FORBIDDEN_CARD_WORDING:
        if pattern.search(text):
            raise ValueError(f"forbidden card wording: {pattern.pattern}")
    return text


class ForecastFact(BaseModel):
    """An observed fact with its period before the cutoff; never a causal diagnosis."""

    model_config = ConfigDict(extra="forbid")

    kind: FactKind
    text: str = Field(min_length=1, max_length=300)
    period_start: AwareDatetime
    period_end: AwareDatetime
    value_number: FiniteFloat | None = None
    value_at: AwareDatetime | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def check_fact(self) -> Self:
        check_card_wording(self.text)
        if self.period_start > self.period_end:
            raise ValueError("fact period is reversed")
        if self.kind in _FACT_TIME_KINDS:
            if self.value_at is None or self.value_number is not None:
                raise ValueError("time fact requires value_at only")
            if not self.period_start <= self.value_at <= self.period_end:
                raise ValueError("time fact lies outside its period")
            return self
        if self.value_at is not None or self.value_number is None:
            raise ValueError("numeric fact requires value_number only")
        if self.kind in _FACT_COUNT_KINDS and (
            self.value_number < 0 or not self.value_number.is_integer()
        ):
            raise ValueError("count fact must be a non-negative integer")
        if self.kind in _FACT_SHARE_KINDS and not 0 <= self.value_number <= 1:
            raise ValueError("share fact must be within [0, 1]")
        return self


class ForecastChannel(BaseModel):
    """Candidate channel of the card, ordered as the scorer ranked it.

    An abstained card lists the channels it would have covered at the cutoff;
    their order is then a listing order, not a score rank.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1, max_length=256)
    channel_name: str | None = None
    engineering_tag: str | None = None
    rank_in_card: int = Field(ge=1)
    picket_form: Literal["point", "range", "unknown"]
    picket_from: FiniteFloat | None = Field(default=None, ge=0)
    picket_to: FiniteFloat | None = Field(default=None, ge=0)
    # UI-06: 10 m per picket is shown with its origin; unknown topology is not guessed.
    picket_basis: PicketBasis | None = None
    feeder_kind: FeederKind | None = None
    reason_facts: list[ForecastFact] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_picket(self) -> Self:
        if self.picket_form == "unknown":
            if self.picket_from is not None or self.picket_to is not None:
                raise ValueError("unknown picket cannot carry picket values")
            if self.picket_basis is not None:
                raise ValueError("unknown picket has no basis")
            return self
        if self.picket_basis is None or self.picket_from is None:
            raise ValueError("known picket requires a value and its basis")
        if self.picket_form == "point" and self.picket_to is not None:
            raise ValueError("point picket has no range end")
        if self.picket_form == "range" and (
            self.picket_to is None or self.picket_to < self.picket_from
        ):
            raise ValueError("range picket requires an ordered end")
        return self


class BucketRate(BaseModel):
    """Share of cards with an event in the rank bucket over one past period."""

    model_config = ConfigDict(extra="forbid")

    period_start: date
    period_end: date
    cards: int = Field(ge=1)
    positive_cards: int = Field(ge=0)
    event_rate: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def check_rate(self) -> Self:
        if self.period_start >= self.period_end:
            raise ValueError("bucket period is empty or reversed")
        if self.positive_cards > self.cards:
            raise ValueError("positive cards exceed cards")
        if not math.isclose(self.event_rate, self.positive_cards / self.cards, abs_tol=1e-4):
            raise ValueError("event rate differs from its numerator and denominator")
        return self


class RankBucket(BaseModel):
    """Card's bucket under a versioned per target x horizon policy, e.g. daily
    ``[1, 6, 11, 51]`` (1–5, 6–10, 11–50, 51+) or weekly ``[1, 11, 21, 51]``.
    The policy also assigns the risk level of every bucket."""

    model_config = ConfigDict(extra="forbid")

    policy_id: str = Field(min_length=1)
    # Starts of all buckets of the policy; the last bucket is open-ended.
    bucket_starts: list[int] = Field(min_length=1)
    bucket_levels: list[BucketLevel] = Field(min_length=1)
    rank_from: int = Field(ge=1)
    rank_to: int | None = Field(default=None, ge=1)
    calibration: BucketRate
    holdout: BucketRate | None = None
    source_report: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_bounds(self) -> Self:
        starts = self.bucket_starts
        if starts[0] != 1 or any(
            low >= high for low, high in zip(starts, starts[1:], strict=False)
        ):
            raise ValueError("bucket starts must begin at 1 and increase")
        if len(self.bucket_levels) != len(starts):
            raise ValueError("every policy bucket needs a risk level")
        if self.rank_from not in starts:
            raise ValueError("rank bucket bounds differ from its policy")
        expected_to = starts[self.index + 1] - 1 if self.index + 1 < len(starts) else None
        if self.rank_to != expected_to:
            raise ValueError("rank bucket bounds differ from its policy")
        return self

    @property
    def index(self) -> int:
        return self.bucket_starts.index(self.rank_from)

    @property
    def level(self) -> BucketLevel:
        return self.bucket_levels[self.index]


class FrequencyBucket(BaseModel):
    """«k из n»: share of past cards with an event among cards whose pair had a
    number of events in the past 365 days within ``[events_from, events_to]``.

    Built on a stated past period (``table_version``); it is a frequency of the
    history, not a calibrated probability (ML-05). The Wilson interval is shown with it.
    """

    model_config = ConfigDict(extra="forbid")

    basis: Literal["pair_events_365d"] = "pair_events_365d"
    table_version: str = Field(min_length=1)
    events_from: int = Field(ge=0)
    events_to: int | None = Field(default=None, ge=0)
    cards: int = Field(ge=1)
    positive_cards: int = Field(ge=0)
    share: float = Field(ge=0, le=1)
    wilson_low: float = Field(ge=0, le=1)
    wilson_high: float = Field(ge=0, le=1)
    period_start: date
    period_end: date
    level: BucketLevel
    source_report: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_bucket(self) -> Self:
        if self.events_to is not None and self.events_to < self.events_from:
            raise ValueError("frequency bucket bounds are reversed")
        if self.positive_cards > self.cards:
            raise ValueError("positive cards exceed cards")
        if not math.isclose(self.share, self.positive_cards / self.cards, abs_tol=1e-4):
            raise ValueError("share differs from its numerator and denominator")
        if not self.wilson_low <= self.share <= self.wilson_high:
            raise ValueError("share lies outside its Wilson interval")
        if self.period_start >= self.period_end:
            raise ValueError("frequency table period is empty or reversed")
        return self


_SCORE_LABELS = {
    "risk_estimate": "оценка риска",
    "probability": "вероятность",
    "frequency_share": "доля k из n",
}


class ForecastScore(BaseModel):
    """Card score at issue.

    - ``risk_estimate`` (gate A): calibrated value labelled «оценка риска» beside the
      empirical rates of its ``rank_bucket``; the value need not equal those rates.
    - ``frequency_share`` (static list): ``value`` equals the share of its
      ``frequency`` bucket, shown as «k из n» with the Wilson interval.
    - ``probability``: only after ML-05 approval (see the card validator).

    ``rank`` is the order of checking at the issue cutoff among ``ranked_cards``
    candidates, not severity. For a rolling list ``card_budget`` is the maximum number
    of open cards of the run.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["risk_estimate", "probability", "frequency_share"] = "risk_estimate"
    label: Literal["оценка риска", "вероятность", "доля k из n"] = "оценка риска"
    value: float = Field(ge=0, le=1)
    rank: int = Field(ge=1)
    ranked_cards: int = Field(ge=1)
    card_budget: int = Field(ge=1)
    rank_bucket: RankBucket | None = None
    frequency: FrequencyBucket | None = None

    @model_validator(mode="after")
    def check_score(self) -> Self:
        if self.label != _SCORE_LABELS[self.kind]:
            raise ValueError("score label must match score kind")
        if self.rank > self.ranked_cards:
            raise ValueError("rank exceeds ranked cards")
        if self.kind == "frequency_share":
            if self.frequency is None:
                raise ValueError("frequency share requires its frequency bucket")
            if not math.isclose(self.value, self.frequency.share, abs_tol=1e-4):
                raise ValueError("frequency share value differs from its bucket share")
        elif self.rank_bucket is None:
            raise ValueError("risk estimate and probability require a rank bucket")
        if self.rank_bucket is not None:
            low, high = self.rank_bucket.rank_from, self.rank_bucket.rank_to
            if self.rank < low or (high is not None and self.rank > high):
                raise ValueError("rank lies outside its bucket")
        if self.rank <= self.card_budget and self.level == "low":
            raise ValueError("a card within the budget cannot be low risk")
        return self

    @property
    def level(self) -> BucketLevel:
        if self.kind == "frequency_share" and self.frequency is not None:
            return self.frequency.level
        assert self.rank_bucket is not None
        return self.rank_bucket.level


class ForecastFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_as_of: AwareDatetime
    last_object_record_at: AwareDatetime | None = None
    lookback_days: int = Field(ge=1)
    history_days_available: int | None = Field(default=None, ge=0)
    coverage: Literal["complete", "partial", "insufficient", "unknown"]

    @model_validator(mode="after")
    def check_freshness(self) -> Self:
        if self.last_object_record_at is not None and self.last_object_record_at > self.data_as_of:
            raise ValueError("object record is later than the data watermark")
        return self


class ObservedOverlayItem(BaseModel):
    """Source record seen after issue; raw text and the original alarm are kept apart."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1, max_length=256)
    row_uid: str | None = None
    observation_kind: Literal["source_alarm", "target_candidate"]
    value_raw: str
    alarm: bool
    event_at: AwareDatetime
    available_at: AwareDatetime

    @model_validator(mode="after")
    def check_alarm(self) -> Self:
        if self.observation_kind == "source_alarm" and not self.alarm:
            raise ValueError("source alarm overlay requires the original alarm=true")
        return self


class ObservedOverlay(BaseModel):
    """Current observation after issue, shown separately. The forecast is not recomputed."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["already_observed"] = "already_observed"
    forecast_recomputed: Literal[False] = False
    checked_at: AwareDatetime
    items: list[ObservedOverlayItem] = Field(min_length=1)

    @model_validator(mode="after")
    def check_items(self) -> Self:
        if any(item.available_at > self.checked_at for item in self.items):
            raise ValueError("overlay contains a record unavailable at checked_at")
        return self


class ForecastVersions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scorer: Literal["persistence", "logreg", "catboost", "static_list"]
    model_version: str = Field(min_length=1)
    feature_version: str = Field(min_length=1)
    label_version: str = Field(min_length=1)
    # Versioned policy for risk level, buckets, card budget and list policy.
    policy_version: str = Field(min_length=1)
    calibration_version: str | None = None
    # Rule that marks a pair as chronic or fresh; required when a card carries it.
    recurrence_rule_version: str | None = None
    release_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)


class ForecastCard(BaseModel):
    """Immutable object x sensor type card as issued at ``issued_at`` (the cutoff).

    Later observations are never written into the card; see ``ForecastCardView``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=256)
    mode: ForecastMode
    target_spec_id: TargetSpecId
    # Derived from the target when omitted; always checked against it.
    incident_type: IncidentType | None = None
    event_label: Literal[
        "зарегистрированное техническое событие канала",
        "Линии питания освещения, вентиляции и насосов: сначала потеря связи („Неисправен“), "
        "через ~10 мин — „Обесточен“. Причина неизвестна",
        "потеря связи дымового или теплового извещателя либо серия неподтверждённых "
        "самосбросов вне сессии ТО; причина неизвестна",
    ] = EVENT_LABEL
    target_label: Literal[
        "потеря связи ≥ 1 ч",
        "техническое значение",
        "обесточивание электрооборудования объекта",
        "отказ пожарных извещателей (пилот)",
    ]
    object_id: str | None = None
    sensor_type: str = Field(min_length=1)
    system_type: str | None = None
    horizon: Horizon
    issued_at: AwareDatetime
    window_start: AwareDatetime
    window_end: AwareDatetime
    published_at: AwareDatetime
    freshness: ForecastFreshness
    status: Literal["scored", "abstained"]
    abstention_reason: AbstentionReason | None = None
    abstention_detail: str | None = Field(default=None, max_length=300)
    score: ForecastScore | None = None
    risk_level: RiskLevel
    channels: list[ForecastChannel] = Field(default_factory=list)
    # Candidate channels of the pair; the card may list only the top ``channels`` (e.g. the
    # 3–5 feeders with most past episodes). None means every candidate is listed.
    channels_total: int | None = Field(default=None, ge=0)
    # Channels already in an episode at the cutoff: current observation, not candidates.
    active_episode_channel_ids: list[str] = Field(default_factory=list)
    facts: list[ForecastFact] = Field(default_factory=list)
    continuation_of: str | None = None
    # Pair with repeated past events (chronic) or not (fresh), by a versioned rule.
    recurrence: Recurrence | None = None
    versions: ForecastVersions

    @model_validator(mode="before")
    @classmethod
    def fill_target_labels(cls, data):
        if isinstance(data, dict) and data.get("target_spec_id") in TARGET_INCIDENTS:
            target = data["target_spec_id"]
            data = dict(data)
            data.setdefault("incident_type", TARGET_INCIDENTS[target])
            data.setdefault("event_label", EVENT_LABELS[target])
        return data

    @model_validator(mode="after")
    def check_card(self) -> Self:
        if self.target_label != TARGET_LABELS[self.target_spec_id]:
            raise ValueError("target label differs from the target spec")
        if self.incident_type != TARGET_INCIDENTS[self.target_spec_id]:
            raise ValueError("incident type differs from the target spec")
        if self.event_label != EVENT_LABELS[self.target_spec_id]:
            raise ValueError("event label differs from the target spec")
        if self.recurrence is not None and self.versions.recurrence_rule_version is None:
            raise ValueError("recurrence requires its rule version")
        if self.channels_total is not None and self.channels_total < len(self.channels):
            raise ValueError("channels total is smaller than the listed channels")
        if self.window_start != self.issued_at:
            raise ValueError("forecast window starts at the cutoff")
        if self.window_end - self.window_start != timedelta(hours=HORIZON_HOURS[self.horizon]):
            raise ValueError("forecast window differs from the horizon")
        if not self.issued_at <= self.published_at < self.window_end:
            raise ValueError("card must be published after the cutoff and before the window ends")
        if self.freshness.data_as_of > self.issued_at:
            raise ValueError("data watermark is later than the cutoff")
        if (self.object_id is None) != (self.abstention_reason == "no_object_binding"):
            raise ValueError("a card without object binding is abstained as no_object_binding")
        if self.abstention_detail is not None:
            check_card_wording(self.abstention_detail)
        if self.status == "abstained":
            if self.score is not None or self.risk_level != "unknown":
                raise ValueError("abstention requires no score and unknown risk")
            if self.abstention_reason is None:
                raise ValueError("abstention needs a reason code")
            if self.abstention_reason == "insufficient_history" and (
                self.freshness.history_days_available is None
                or self.freshness.history_days_available >= self.freshness.lookback_days
            ):
                raise ValueError("insufficient history must be shorter than the lookback")
        else:
            if self.score is None or self.risk_level == "unknown":
                raise ValueError("scored card requires a score and a risk level")
            if self.abstention_reason is not None or self.abstention_detail is not None:
                raise ValueError("scored card cannot carry an abstention")
            if not self.channels:
                raise ValueError("scored card lists its candidate channels")
            if any(not channel.reason_facts for channel in self.channels):
                raise ValueError("every candidate channel states why it was included")
            if self.risk_level != self.score.level:
                raise ValueError("risk level must follow the rank bucket policy")
        if self.score is not None and self.score.kind == "probability":
            if self.versions.calibration_version is None:
                raise ValueError("probability requires recorded calibration")
            if not PROBABILITY_DISPLAY_APPROVED:
                raise ValueError("probability display is not approved (ML-05); use risk_estimate")
        channel_ids = [channel.channel_id for channel in self.channels]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("duplicate candidate channel")
        if [channel.rank_in_card for channel in self.channels] != list(
            range(1, len(self.channels) + 1)
        ):
            raise ValueError("channel ranks in card must be contiguous from 1")
        if set(channel_ids) & set(self.active_episode_channel_ids):
            raise ValueError("a channel in an active episode is not a candidate")
        facts = [*self.facts, *(fact for channel in self.channels for fact in channel.reason_facts)]
        if any(fact.period_end > self.issued_at for fact in facts):
            raise ValueError("fact period ends after the cutoff")
        if self.continuation_of == self.id:
            raise ValueError("card cannot continue itself")
        return self


# Rule of a recommendation policy («group_pumps», «repeat_realized»).
RULE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class ForecastRecommendation(BaseModel):
    """Recommended action from a versioned policy (technologist dictionary R1–R7).

    The text is computed on the server from facts and the policy; the UI never derives
    it. It is highlighted, not pre-selected, in the decision form.
    """

    model_config = ConfigDict(extra="forbid")

    # None while the policy is not mapped to the technologist dictionary R1–R7.
    decision_code: DecisionCode | None = None
    # Main text, the same for every scored card of a policy version.
    text: str = Field(min_length=1, max_length=300)
    # Details under the main text (v5: a realized repeat, groups of the card lines), in order.
    details: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list, max_length=3
    )
    # Policy rules that produced the details (audit: why the text is such).
    rule_ids: list[Annotated[str, Field(pattern=RULE_ID_PATTERN)]] = Field(
        default_factory=list, max_length=6
    )
    policy_version: str = Field(min_length=1)
    # False until the customer confirms the regulation; the UI says so next to the text.
    regulation_confirmed: bool = False

    @model_validator(mode="after")
    def check_text(self) -> Self:
        check_card_wording(self.text)
        for detail in self.details:
            check_card_wording(detail)
        if len(set(self.rule_ids)) != len(self.rule_ids):
            raise ValueError("duplicate recommendation rule")
        return self


def check_list_state(card: ForecastCard, list_state: ListState | None, released_at) -> None:
    """A released card left the list at its event, inside its window and after issue."""
    if (list_state == "released") != (released_at is not None):
        raise ValueError("released state and release time go together")
    if released_at is not None and not card.issued_at <= released_at < card.window_end:
        raise ValueError("release lies outside the card window")
    if list_state is not None and card.status == "abstained" and list_state == "released":
        raise ValueError("an abstained card is never released by an event")


class ForecastCardView(BaseModel):
    """Issued card plus a view-time «уже наблюдается» overlay.

    The overlay is a current observation after issue, shown separately; the card
    and its score are not recomputed. The journal stores only the bare card.
    """

    model_config = ConfigDict(extra="forbid")

    card: ForecastCard
    already_observed: bool = False
    observed_overlay: ObservedOverlay | None = None
    # Rolling list state at the view's as_of; the issued card itself never changes.
    list_state: ListState | None = None
    released_at: AwareDatetime | None = None
    day_index: int | None = Field(default=None, ge=1)
    days_total: int | None = Field(default=None, ge=1)
    recommendation: ForecastRecommendation | None = None

    @model_validator(mode="after")
    def check_overlay(self) -> Self:
        check_list_state(self.card, self.list_state, self.released_at)
        if (self.day_index is None) != (self.days_total is None):
            raise ValueError("day index and days total go together")
        if self.days_total is not None:
            if self.days_total * 24 != HORIZON_HOURS[self.card.horizon]:
                raise ValueError("days total differs from the horizon")
            if self.day_index > self.days_total:
                raise ValueError("day index exceeds days total")
        if self.already_observed != (self.observed_overlay is not None):
            raise ValueError("already_observed flag must match the overlay")
        if self.observed_overlay is not None and any(
            item.event_at < self.card.issued_at or item.available_at < self.card.issued_at
            for item in self.observed_overlay.items
        ):
            raise ValueError("overlay shows only records observed after issue")
        return self


class ForecastOutcome(BaseModel):
    """Registered event after the window by the same label definition, not a human verdict.

    Abstained cards get ``event_without_forecast`` / ``no_event_without_forecast``
    for coverage accounting and are always excluded from quality metrics.
    """

    model_config = ConfigDict(extra="forbid")

    status: OutcomeStatus
    basis: Literal["automatic_registered_event"] = "automatic_registered_event"
    label_version: str = Field(min_length=1)
    resolved_at: AwareDatetime | None = None
    first_event_at: AwareDatetime | None = None
    lead_hours: float | None = Field(default=None, ge=0)
    event_channel_ids: list[str] = Field(default_factory=list)
    event_cluster_size: int | None = Field(default=None, ge=1)
    unknown_reason: UnknownOutcomeReason | None = None
    # Events of the object on channels that were not candidates of the card; they
    # never make the card realized (capture only via candidate member channels).
    other_events_on_object_count: int | None = Field(default=None, ge=0)
    excluded_from_quality_metrics: bool = False
    # A dispatched check (R3) before the window ended: y=0 then is not a false forecast.
    intervention_before_window_end: bool = False
    # First «Обесточен» on the event channels after the event start (C0.4), if seen.
    power_off_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_outcome(self) -> Self:
        if self.power_off_at is not None and (
            self.status not in _EVENT_OUTCOMES
            or self.first_event_at is None
            or self.power_off_at < self.first_event_at
        ):
            raise ValueError("power-off time follows the event start of an event outcome")
        event_fields = (
            self.first_event_at is not None
            or self.lead_hours is not None
            or bool(self.event_channel_ids)
            or self.event_cluster_size is not None
        )
        if self.status == "pending":
            if (
                self.resolved_at is not None
                or event_fields
                or self.unknown_reason is not None
                or self.other_events_on_object_count is not None
            ):
                raise ValueError("pending outcome has no resolution")
            return self
        if self.resolved_at is None:
            raise ValueError("resolved outcome requires resolved_at")
        if self.status in _EVENT_OUTCOMES:
            if self.first_event_at is None or not self.event_channel_ids:
                raise ValueError("event outcome requires the event start and channels")
            if (self.status == "realized") != (self.lead_hours is not None):
                raise ValueError("lead is measured only for a realized forecast")
        elif event_fields:
            raise ValueError("only an event outcome carries event details")
        if (self.status == "unknown") != (self.unknown_reason is not None):
            raise ValueError("unknown outcome and its reason go together")
        if self.status in ("event_without_forecast", "no_event_without_forecast") and not (
            self.excluded_from_quality_metrics
        ):
            raise ValueError("outcomes without forecast are excluded from quality metrics")
        return self


class ForecastDecisionSummary(BaseModel):
    """Dispatcher decision (technologist dictionary R1–R7) and later check result (O1–O6)."""

    model_config = ConfigDict(extra="forbid")

    decision_code: DecisionCode
    reason_code: str = Field(min_length=1, max_length=64)
    reason_text: str = Field(min_length=1, max_length=300)
    dictionary_version: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    actor_role: str = Field(min_length=1)
    decided_at: AwareDatetime
    revision: int = Field(ge=1)
    simulated: bool
    verification_methods: list[VerificationMethod] = Field(default_factory=list)
    # «Кому и когда сообщено» (e.g. the duty power engineer of the operator).
    notified_to: str | None = Field(default=None, min_length=1, max_length=200)
    notified_at: AwareDatetime | None = None
    # «Сообщено энергетику» (R3): until when a check result is awaited (C0.4).
    awaiting_result_until: AwareDatetime | None = None
    # «Под наблюдением» (R1): until when the card is watched (C0.4).
    watch_until: AwareDatetime | None = None
    draft_id: str | None = None
    draft_status: Literal["not_sent"] | None = None
    check_result_code: CheckResultCode = "O6"
    check_result_simulated: bool = False

    @model_validator(mode="after")
    def check_decision(self) -> Self:
        if (self.decision_code in ("R3", "R4")) != (self.draft_id is not None):
            raise ValueError("R3/R4 create a work-order draft; other decisions do not")
        if (self.draft_id is None) != (self.draft_status is None):
            raise ValueError("draft and its status go together")
        if self.check_result_simulated != (self.check_result_code != "O6"):
            raise ValueError("check results O1–O5 exist only as marked simulation")
        if len(set(self.verification_methods)) != len(self.verification_methods):
            raise ValueError("duplicate verification method")
        if (self.notified_to is None) != (self.notified_at is None):
            raise ValueError("notified recipient and time go together")
        if self.notified_at is not None and self.notified_at > self.decided_at:
            raise ValueError("notification is later than the decision")
        check_decision_deadlines(
            self.decision_code, self.awaiting_result_until, self.watch_until, self.decided_at
        )
        return self


def check_decision_deadlines(code: str, awaiting_until, watch_until, after) -> None:
    """R3 alone has a result deadline and R1 alone a watch deadline, both after ``after``."""
    if awaiting_until is not None and code != NOTIFIED_CODE:
        raise ValueError("only «Сообщено энергетику» (R3) awaits a result")
    if watch_until is not None and code != WATCH_CODE:
        raise ValueError("only «Под наблюдением» (R1) has a watch deadline")
    for deadline in (awaiting_until, watch_until):
        if deadline is not None and after is not None and deadline <= after:
            raise ValueError("a decision deadline must be later than the decision")


class ForecastDecisionList(BaseModel):
    """Every decision revision of one card, newest first, with its author."""

    model_config = ConfigDict(extra="forbid")

    forecast_id: str = Field(min_length=1, max_length=256)
    items: list[ForecastDecisionSummary]

    @model_validator(mode="after")
    def check_revisions(self) -> Self:
        revisions = [item.revision for item in self.items]
        if revisions != sorted(revisions, reverse=True) or len(set(revisions)) != len(revisions):
            raise ValueError("decision revisions are unique and newest first")
        return self


class ForecastDecisionCreate(BaseModel):
    """Dispatcher decision request; the author comes from the session, never the body.

    ``idempotency_key`` repeats return the stored decision; ``expected_revision`` is the
    card's decision revision the dispatcher saw (0 when none), a mismatch is 409.
    """

    model_config = ConfigDict(extra="forbid")

    decision_code: DecisionCode
    reason_code: str = Field(min_length=1, max_length=64)
    reason_text: str = Field(min_length=1, max_length=300)
    verification_methods: list[VerificationMethod] = Field(min_length=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    expected_revision: int = Field(ge=0)
    draft_note: str | None = Field(default=None, max_length=500)
    # «Кому и когда сообщено»; both or none. The time is when the recipient was told.
    notified_to: str | None = Field(default=None, min_length=1, max_length=200)
    notified_at: AwareDatetime | None = None
    # R3 «Сообщено энергетику»: required, later than ``notified_at`` (C0.4).
    awaiting_result_until: AwareDatetime | None = None
    # R1 «Под наблюдением»: required (C0.4).
    watch_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_request(self) -> Self:
        if len(set(self.verification_methods)) != len(self.verification_methods):
            raise ValueError("duplicate verification method")
        if (self.notified_to is None) != (self.notified_at is None):
            raise ValueError("notified recipient and time go together")
        if self.decision_code == NOTIFIED_CODE and (
            self.notified_to is None or self.awaiting_result_until is None
        ):
            raise ValueError(
                "«Сообщено энергетику» (R3) needs recipient, time and a result deadline"
            )
        if self.decision_code == WATCH_CODE and self.watch_until is None:
            raise ValueError("«Под наблюдением» (R1) needs a watch deadline")
        check_decision_deadlines(
            self.decision_code, self.awaiting_result_until, self.watch_until, self.notified_at
        )
        if self.draft_note is not None and self.decision_code not in ("R3", "R4"):
            raise ValueError("only R3/R4 carry a work-order draft note")
        return self


class ForecastWindowEvent(BaseModel):
    """A registered event of the card's pair inside the card window.

    ``while_open`` tells whether the card was open at the event start. Recall variants
    are computed from these rows by a versioned definition, not fixed in the contract.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=256)
    started_at: AwareDatetime
    channel_ids: list[str] = Field(min_length=1)
    cluster_size: int = Field(ge=1)
    while_open: bool
    # First «Обесточен» on the event channels after the start (C0.4), if seen.
    power_off_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_power_off(self) -> Self:
        if self.power_off_at is not None and self.power_off_at < self.started_at:
            raise ValueError("power-off time precedes the event start")
        return self


class ForecastCheckResultCreate(BaseModel):
    """Result of the check after a decision (C0.4); the author comes from the session.

    ``found`` («что устранено») only with ``fixed`` and is not confirmed by the customer;
    ``event_cause`` only for a card whose event was registered (the server checks it).
    """

    model_config = ConfigDict(extra="forbid")

    check_result: CheckResultStatus
    found: list[FoundItem] = Field(default_factory=list)
    found_other_text: str | None = Field(default=None, min_length=1, max_length=200)
    result_at: AwareDatetime
    comment: str | None = Field(default=None, max_length=500)
    event_cause: EventCause | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def check_request(self) -> Self:
        check_found(self.check_result, self.found, self.found_other_text)
        return self


def check_found(result: str, found: list[str], other_text: str | None) -> None:
    if len(set(found)) != len(found):
        raise ValueError("duplicate found item")
    if found and result != "fixed":
        raise ValueError("found items only for a fixed result")
    if ("other" in found) != (other_text is not None):
        raise ValueError("«другое» and its text go together")


class ForecastCheckResult(BaseModel):
    """Stored check result revision of a card (append-only, audited)."""

    model_config = ConfigDict(extra="forbid")

    forecast_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    check_result: CheckResultStatus
    found: list[FoundItem] = Field(default_factory=list)
    found_other_text: str | None = Field(default=None, min_length=1, max_length=200)
    found_confirmed_by_customer: Literal[False] = False
    result_at: AwareDatetime
    comment: str | None = Field(default=None, max_length=500)
    event_cause: EventCause | None = None
    actor_id: str = Field(min_length=1)
    actor_role: str = Field(min_length=1)
    recorded_at: AwareDatetime
    simulated: bool

    @model_validator(mode="after")
    def check_result_row(self) -> Self:
        check_found(self.check_result, self.found, self.found_other_text)
        if self.result_at > self.recorded_at:
            raise ValueError("result time is later than its record")
        return self


class ForecastCheckResultList(BaseModel):
    """Every check result revision of one card, newest first."""

    model_config = ConfigDict(extra="forbid")

    forecast_id: str = Field(min_length=1, max_length=256)
    items: list[ForecastCheckResult]

    @model_validator(mode="after")
    def check_revisions(self) -> Self:
        revisions = [item.revision for item in self.items]
        if revisions != sorted(revisions, reverse=True) or len(set(revisions)) != len(revisions):
            raise ValueError("check result revisions are unique and newest first")
        if any(item.forecast_id != self.forecast_id for item in self.items):
            raise ValueError("check result of another card")
        return self


class ForecastJournalEntry(BaseModel):
    """Append-only journal row: the issued card snapshot, its outcome and a decision.

    For a rolling list with release the card leaves the list at its first event
    (``released_at``) and is realized then; otherwise the outcome resolves only after
    the window ends.
    """

    model_config = ConfigDict(extra="forbid")

    journal_position: int = Field(ge=1)
    snapshot_immutable: Literal[True] = True
    card: ForecastCard
    outcome: ForecastOutcome
    decision: ForecastDecisionSummary | None = None
    list_state: ListState | None = None
    released_at: AwareDatetime | None = None
    events: list[ForecastWindowEvent] = Field(default_factory=list)
    # Latest check result revision saved by the journal's ``records_as_of`` (C0.4).
    check_result: ForecastCheckResult | None = None

    @model_validator(mode="after")
    def check_entry(self) -> Self:
        card, outcome = self.card, self.outcome
        if outcome.label_version != card.versions.label_version:
            raise ValueError("outcome must use the card's label definition")
        check_list_state(card, self.list_state, self.released_at)
        if self.released_at is not None:
            if outcome.status != "realized":
                raise ValueError("a released card is realized")
            if outcome.first_event_at > self.released_at:
                raise ValueError("release precedes the first event")
            if outcome.resolved_at < self.released_at:
                raise ValueError("outcome resolves before the release")
        elif outcome.resolved_at is not None and outcome.resolved_at < card.window_end:
            raise ValueError("outcome resolves only after the window ends")
        starts = [event.started_at for event in self.events]
        if starts != sorted(starts):
            raise ValueError("window events are ordered by start")
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("duplicate window event")
        for event in self.events:
            if not card.window_start <= event.started_at < card.window_end:
                raise ValueError("window event starts outside the window")
            if (
                event.while_open
                and self.released_at is not None
                and event.started_at > self.released_at
            ):
                raise ValueError("an event after the release is not while open")
        if outcome.excluded_from_quality_metrics != (card.status == "abstained"):
            raise ValueError("only abstained cards are excluded from quality metrics")
        allowed = _ABSTAINED_OUTCOMES if card.status == "abstained" else _SCORED_OUTCOMES
        if outcome.status not in allowed:
            raise ValueError(f"outcome {outcome.status} does not apply to {card.status} cards")
        if outcome.status in _EVENT_OUTCOMES:
            if not card.window_start <= outcome.first_event_at < card.window_end:
                raise ValueError("event starts outside the window")
            if outcome.lead_hours is not None:
                lead = (outcome.first_event_at - card.issued_at).total_seconds() / 3600
                if not math.isclose(outcome.lead_hours, lead, abs_tol=0.01):
                    raise ValueError("lead differs from event start minus cutoff")
            # A card that lists only its top candidates (``channels_total`` above the listed
            # count) can realize on an unlisted candidate channel (B2, C0.1 request).
            lists_all = card.channels_total is None or card.channels_total <= len(card.channels)
            if lists_all and not set(outcome.event_channel_ids) <= {
                channel.channel_id for channel in card.channels
            }:
                raise ValueError("event must be on candidate channels of the card")
        if self.decision is not None and self.decision.decided_at < card.published_at:
            raise ValueError("decision precedes the published card")
        if outcome.intervention_before_window_end and not (
            self.decision is not None
            and self.decision.decision_code in ("R2", "R3", "R4")
            and self.decision.decided_at < card.window_end
        ):
            raise ValueError("intervention flag requires an R2–R4 decision within the window")
        return self

    @model_validator(mode="after")
    def check_result_fits(self) -> Self:
        result = self.check_result
        if result is None:
            return self
        if result.forecast_id != self.card.id:
            raise ValueError("check result of another card")
        if result.recorded_at < self.card.published_at:
            raise ValueError("check result precedes the published card")
        if result.event_cause is not None and self.outcome.status != "realized":
            raise ValueError("event cause needs a registered event of the card")
        return self


class ListPolicy(BaseModel):
    """How cards of a run are issued.

    - ``daily_top_k``: the run's own top ``max_open`` cards; cards of older runs are gone.
    - ``rolling_release``: a card stays open ``window_days`` from its cutoff, each cutoff
      adds cards by score for pairs without an open card until ``max_open`` are open,
      nothing is displaced; with ``release_on = first_event`` the card leaves at its
      first registered event and frees its place from the next cutoff.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["daily_top_k", "rolling_release"]
    max_open: int = Field(ge=1, le=100)
    window_days: int = Field(ge=1, le=60)
    release_on: Literal["window_end", "first_event"]
    policy_version: str = Field(min_length=1)


class PublishedRun(BaseModel):
    """Currently published run of one target x horizon with its own generation counter."""

    model_config = ConfigDict(extra="forbid")

    target_spec_id: TargetSpecId
    horizon: Horizon
    run_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    issued_at: AwareDatetime
    published_at: AwareDatetime
    list_policy: ListPolicy | None = None

    @model_validator(mode="after")
    def check_policy(self) -> Self:
        policy = self.list_policy
        if policy is not None and policy.kind == "rolling_release":
            if policy.window_days * 24 != HORIZON_HOURS[self.horizon]:
                raise ValueError("rolling window differs from the horizon")
        if self.published_at < self.issued_at:
            raise ValueError("run is published before its cutoff")
        return self


class ForecastList(BaseModel):
    """Current cards of every published target x horizon run.

    ``publication_token`` is opaque and covers all ``runs``; a cursor issued under
    another token is stale (409), so pages never mix publications.
    """

    model_config = ConfigDict(extra="forbid")

    mode: ForecastMode
    as_of: AwareDatetime
    publication_token: str = Field(min_length=1, max_length=64)
    runs: list[PublishedRun]
    items: list[ForecastCardView]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None = None

    @model_validator(mode="after")
    def check_page(self) -> Self:
        if len(self.items) > min(self.total, self.limit):
            raise ValueError("page exceeds its limit or total")
        runs = {(run.target_spec_id, run.horizon): run for run in self.runs}
        if len(runs) != len(self.runs):
            raise ValueError("one published run per target and horizon")
        if any(run.published_at > self.as_of for run in self.runs):
            raise ValueError("run unpublished at as_of")
        open_per_run: dict[tuple[str, str], int] = {}
        for view in self.items:
            card = view.card
            key = (card.target_spec_id, card.horizon)
            run = runs.get(key)
            if card.mode != self.mode:
                raise ValueError("mixed forecast modes")
            if run is None:
                raise ValueError("card does not belong to a currently published run")
            rolling = run.list_policy is not None and run.list_policy.kind == "rolling_release"
            if rolling:
                # Cards of a rolling list were issued by this or an earlier run; open cards
                # are the default page, released or expired ones only on request.
                if card.issued_at > run.issued_at:
                    raise ValueError("card does not belong to a currently published run")
                if card.status == "scored" and view.list_state is None:
                    raise ValueError("a rolling list card states whether it is open")
                if view.list_state == "open" and self.as_of >= card.window_end:
                    raise ValueError("an open card is inside its window")
                if view.released_at is not None and view.released_at > self.as_of:
                    raise ValueError("release later than as_of")
                if card.status == "scored" and view.list_state == "open":
                    open_per_run[key] = open_per_run.get(key, 0) + 1
                    if open_per_run[key] > run.list_policy.max_open:
                        raise ValueError("more open cards than the list policy allows")
            elif (run.run_id, run.issued_at) != (card.versions.run_id, card.issued_at):
                raise ValueError("card does not belong to a currently published run")
        return self


class ForecastJournalList(BaseModel):
    """Append-only journal page pinned by ``journal_watermark`` (committed entries).

    Two time axes. ``as_of`` is the data moment: cards, outcomes and window events are
    shown as known from the data up to it. Decisions and check results are records on
    the wall clock (when a dispatcher saved them): an entry carries the newest one saved
    by ``records_as_of``, even when it is later than ``as_of`` — on a historical scope
    (data ended in the past) every decision is taken after the data. ``None`` (fixture)
    bounds them by ``as_of``. Outcomes and quality metrics never depend on them.
    """

    model_config = ConfigDict(extra="forbid")

    mode: ForecastMode
    as_of: AwareDatetime
    records_as_of: AwareDatetime | None = None
    journal_watermark: int = Field(ge=0)
    items: list[ForecastJournalEntry]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None = None

    @model_validator(mode="after")
    def check_page(self) -> Self:
        if len(self.items) > min(self.total, self.limit):
            raise ValueError("page exceeds its limit or total")
        if self.total > self.journal_watermark:
            raise ValueError("journal total exceeds its committed watermark")
        if any(item.journal_position > self.journal_watermark for item in self.items):
            raise ValueError("journal entry is beyond the pinned watermark")
        if any(item.card.mode != self.mode for item in self.items):
            raise ValueError("mixed forecast modes")
        if any(
            (item.outcome.resolved_at or item.card.published_at) > self.as_of for item in self.items
        ):
            raise ValueError("journal contains a record later than as_of")
        records = self.as_of if self.records_as_of is None else self.records_as_of
        if any(
            (item.decision is not None and item.decision.decided_at > records)
            or (item.check_result is not None and item.check_result.recorded_at > records)
            for item in self.items
        ):
            raise ValueError("journal contains a decision or check result later than records_as_of")
        if self.mode == "fixture" and any(
            item.decision is not None and not item.decision.simulated for item in self.items
        ):
            raise ValueError("fixture decisions are marked simulated")
        return self


class RatioMetric(BaseModel):
    """Named ratio with its numerator, denominator and definition version.

    The name is free (e.g. a recall variant chosen later); the contract fixes only
    that every shown ratio carries its counts.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=METRIC_NAME_PATTERN)
    definition_version: str = Field(min_length=1)
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def check_ratio(self) -> Self:
        if self.numerator > self.denominator:
            raise ValueError("numerator exceeds denominator")
        if self.denominator == 0:
            if self.value is not None:
                raise ValueError("an empty ratio has no value")
        elif self.value is None or not math.isclose(
            self.value, self.numerator / self.denominator, abs_tol=1e-4
        ):
            raise ValueError("ratio value differs from its numerator and denominator")
        return self


class ForecastQualityRow(BaseModel):
    """«Прогноз против факта» for one period x target x horizon.

    Card counts are the «k из n» basis: k = ``cards_realized``, n = realized + not
    realized; pending and unknown cards are counted but excluded from the share.
    Abstained cards are not issued and are not counted here.
    """

    model_config = ConfigDict(extra="forbid")

    period_start: date
    period_end: date
    target_spec_id: TargetSpecId
    horizon: Horizon
    cards_issued: int = Field(ge=0)
    cards_pending: int = Field(ge=0)
    cards_realized: int = Field(ge=0)
    cards_not_realized: int = Field(ge=0)
    cards_unknown: int = Field(ge=0)
    card_share: RatioMetric
    event_metrics: list[RatioMetric] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_row(self) -> Self:
        if self.period_start >= self.period_end:
            raise ValueError("summary period is empty or reversed")
        parts = (
            self.cards_pending + self.cards_realized + self.cards_not_realized + self.cards_unknown
        )
        if parts != self.cards_issued:
            raise ValueError("card outcomes do not add up to issued cards")
        share = self.card_share
        if (share.numerator, share.denominator) != (
            self.cards_realized,
            self.cards_realized + self.cards_not_realized,
        ):
            raise ValueError("card share must be realized out of resolved cards")
        names = [metric.name for metric in self.event_metrics]
        if len(names) != len(set(names)):
            raise ValueError("duplicate event metric")
        return self


class ForecastJournalCountsRow(BaseModel):
    """Dispatcher journal counters for one period x target x horizon; no P or R here.

    выдано = снята по событию + без события + неизвестно + открыто.
    """

    model_config = ConfigDict(extra="forbid")

    period_start: date
    period_end: date
    target_spec_id: TargetSpecId
    horizon: Horizon
    cards_issued: int = Field(ge=0)
    cards_released: int = Field(ge=0)
    cards_no_event: int = Field(ge=0)
    cards_unknown: int = Field(ge=0)
    cards_open: int = Field(ge=0)

    @model_validator(mode="after")
    def check_row(self) -> Self:
        if self.period_start >= self.period_end:
            raise ValueError("counts period is empty or reversed")
        parts = self.cards_released + self.cards_no_event + self.cards_unknown + self.cards_open
        if parts != self.cards_issued:
            raise ValueError("card counters do not add up to issued cards")
        return self


class ForecastJournalCounts(BaseModel):
    """Journal counters for dispatchers; quality ratios live on the research side."""

    model_config = ConfigDict(extra="forbid")

    mode: ForecastMode
    as_of: AwareDatetime
    rows: list[ForecastJournalCountsRow]


class ForecastQualitySummary(BaseModel):
    """Weekly summary of the journal; ``evidence_scope`` says what it may be used for."""

    model_config = ConfigDict(extra="forbid")

    mode: ForecastMode
    as_of: AwareDatetime
    basis: Literal["automatic_registered_event"] = "automatic_registered_event"
    # fixture: synthetic; demo_period: illustration on replayed history, not a metric;
    # live_uploads: cards issued after uploaded data (live check), still small samples.
    evidence_scope: Literal["fixture", "demo_period", "live_uploads"]
    rows: list[ForecastQualityRow]

    @model_validator(mode="after")
    def check_scope(self) -> Self:
        if (self.mode == "fixture") != (self.evidence_scope == "fixture"):
            raise ValueError("fixture summaries and only they are marked fixture")
        return self


class HorizonState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_spec_id: TargetSpecId
    horizon: Horizon
    run_id: str = Field(min_length=1)
    issued_at: AwareDatetime
    open_cards: int = Field(ge=0)
    # Cards opened by the latest cutoff: the in-app «новых: N» notification.
    new_cards: int = Field(ge=0)
    released_since_previous: int = Field(ge=0)
    max_open: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def check_counts(self) -> Self:
        if self.new_cards > self.open_cards:
            raise ValueError("new cards exceed open cards")
        if self.max_open is not None and self.open_cards > self.max_open:
            raise ValueError("open cards exceed the list policy")
        return self


class ForecastState(BaseModel):
    """Cheap polling document (UI-03): refetch lists only when ``generation`` changes.

    ``data_as_of`` is the data watermark of the latest published run; the UI shows the
    forecast as of it, not as of the wall clock (historical and uploaded data).
    """

    model_config = ConfigDict(extra="forbid")

    mode: ForecastMode
    checked_at: AwareDatetime
    generation: int = Field(ge=0)
    data_as_of: AwareDatetime | None = None
    published_at: AwareDatetime | None = None
    horizons: list[HorizonState] = Field(default_factory=list)
    last_import_id: str | None = None
    last_import_status: ImportStatus | None = None
    # Two data slices shown to the user (C0.4): the forecast is computed up to
    # ``forecast_data_as_of`` (= ``data_as_of``); source messages are received up to
    # ``source_messages_as_of`` (may be later: messages arrive between recomputations).
    forecast_data_as_of: AwareDatetime | None = None
    source_messages_as_of: AwareDatetime | None = None
    # Days (MSK) without usable data right before the latest cutoff: every cutoff issued on
    # them has no cards (rehearsal 29.09.2026, P1-2). None when the day before the latest
    # cutoff has data, and when the scope has no data day before it at all.
    no_data_from: date | None = None
    no_data_to: date | None = None

    @model_validator(mode="before")
    @classmethod
    def fill_forecast_slice(cls, data):
        if isinstance(data, dict) and "forecast_data_as_of" not in data:
            data = dict(data)
            data["forecast_data_as_of"] = data.get("data_as_of")
        return data

    @model_validator(mode="after")
    def check_state(self) -> Self:
        if self.forecast_data_as_of != self.data_as_of:
            raise ValueError("forecast data slice equals data_as_of")
        if (self.generation == 0) != (self.published_at is None):
            raise ValueError("a published state has a generation and a publication time")
        if (self.last_import_id is None) != (self.last_import_status is None):
            raise ValueError("last import id and status go together")
        if self.published_at is not None and self.published_at > self.checked_at:
            raise ValueError("publication is later than the check")
        if (self.no_data_from is None) != (self.no_data_to is None):
            raise ValueError("days without data are a closed period")
        if self.no_data_from is not None and self.no_data_from > self.no_data_to:
            raise ValueError("days without data are reversed")
        return self


class RecurringPlace(BaseModel):
    """Registry row «хронически проблемное место»: frequent past events, not a forecast."""

    model_config = ConfigDict(extra="forbid")

    object_id: str = Field(min_length=1, max_length=256)
    object_name: str | None = None
    sensor_type: str = Field(min_length=1)
    incident_type: IncidentType
    events_90d: int = Field(ge=0)
    events_365d: int = Field(ge=0)
    last_event_at: AwareDatetime | None = None
    open_card_ids: list[str] = Field(default_factory=list)
    rule_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_row(self) -> Self:
        if self.events_90d > self.events_365d:
            raise ValueError("90-day events exceed 365-day events")
        return self


class RecurringPlaceList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ForecastMode
    as_of: AwareDatetime
    items: list[RecurringPlace]
    total: int = Field(ge=0)
