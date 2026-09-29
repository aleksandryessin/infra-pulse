"""Picket-line scheme of an object («линейки пикетов», team decision 27.09.2026).

A one-dimensional picket scale per object built from channel names (93% of channels
carry a picket). It is a convention, not a geometry: no x/y coordinates and no links
between elements. Panels (inputs, ATS with a picket) are landmarks; feeders sit at a
picket or a picket range (e.g. ГРО, ФАО); channels without a picket go to the «ПК ?»
column. Open cards, current source alarms and cards released within 7 days are shown on
the same scale.
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from infra_pulse_core.contracts.forecast import FeederKind, ForecastMode, PicketBasis

SCHEME_CONVENTION = "условная шкала пикетов из названий каналов, 10 м на пикет; не план"
LandmarkKind = Literal["input", "ats", "panel", "other"]


class SchemePicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form: Literal["point", "range", "unknown"]
    picket_from: FiniteFloat | None = Field(default=None, ge=0)
    picket_to: FiniteFloat | None = Field(default=None, ge=0)
    basis: PicketBasis | None = None

    @model_validator(mode="after")
    def check_picket(self) -> Self:
        if self.form == "unknown":
            if (self.picket_from, self.picket_to, self.basis) != (None, None, None):
                raise ValueError("unknown picket carries no picket values or basis")
            return self
        if self.basis is None or self.picket_from is None:
            raise ValueError("known picket requires a value and its basis")
        if self.form == "point" and self.picket_to is not None:
            raise ValueError("point picket has no range end")
        if self.form == "range" and (self.picket_to is None or self.picket_to < self.picket_from):
            raise ValueError("range picket requires an ordered end")
        return self


class SchemeLandmark(BaseModel):
    """Panel element used as an orientation mark (input, ATS, switchboard)."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str | None = None
    name: str = Field(min_length=1, max_length=256)
    kind: LandmarkKind
    picket: SchemePicket


class SchemeAlarm(BaseModel):
    """A source record with the original ``alarm=true`` within 24 h before ``as_of``; not a
    forecast. F6 (B-3): ``cleared_at`` is the first later «Норма» of the same channel without
    the alarm flag, not later than ``as_of`` — after it the line is no longer in alarm;
    ``None`` — no such record yet: the alarm record may still be in force."""

    model_config = ConfigDict(extra="forbid")

    row_uid: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    value_raw: str
    event_at: AwareDatetime
    cleared_at: AwareDatetime | None = None
    cleared_value_raw: str | None = None

    @model_validator(mode="after")
    def check_cleared(self) -> Self:
        if (self.cleared_at is None) != (self.cleared_value_raw is None):
            raise ValueError("cleared time and value go together")
        if self.cleared_at is not None and self.cleared_at <= self.event_at:
            raise ValueError("cleared before the alarm record")
        return self


class SchemeNamedLink(BaseModel):
    """A link named in the channel name, e.g. «ФВ2 (В23)» → «В23», «ФРО1 (ГРО1-6)» →
    «ГРО1-6» (C0.3). A label only: no topology and no connection is inferred from it."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=32)
    basis: Literal["channel_name"] = "channel_name"


class SchemeFeeder(BaseModel):
    """A power line of the object («линия электропитания»; the model name keeps «feeder»)
    with its picket and past episodes; facts about the past only."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=256)
    feeder_kind: FeederKind
    picket: SchemePicket
    named_link: SchemeNamedLink | None = None
    episodes_365d: int = Field(ge=0)
    last_episode_at: AwareDatetime | None = None
    # Alarm records within 24 h before as_of, newest last; at most 20 per line are listed.
    current_alarms: list[SchemeAlarm] = Field(default_factory=list)
    # F6 (B-3): all alarm records of the line within the same 24 h, without the 20 cap.
    alarm_records_24h: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_alarm_count(self) -> Self:
        if self.alarm_records_24h is not None and self.alarm_records_24h < len(self.current_alarms):
            raise ValueError("alarm record count below listed alarms")
        return self


class SchemeReleasedCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forecast_id: str = Field(min_length=1)
    released_at: AwareDatetime


class ObjectScheme(BaseModel):
    """Picket line of one object at ``as_of``."""

    model_config = ConfigDict(extra="forbid")

    mode: ForecastMode
    as_of: AwareDatetime
    object_id: str = Field(min_length=1, max_length=256)
    object_name: str | None = None
    convention: Literal["условная шкала пикетов из названий каналов, 10 м на пикет; не план"] = (
        SCHEME_CONVENTION
    )
    landmarks: list[SchemeLandmark] = Field(default_factory=list)
    # Feeders with a known picket and, separately in the UI, the «ПК ?» column (unknown).
    feeders: list[SchemeFeeder] = Field(default_factory=list)
    open_card_ids: list[str] = Field(default_factory=list)
    released_7d: list[SchemeReleasedCard] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_scheme(self) -> Self:
        ids = [feeder.channel_id for feeder in self.feeders]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate feeder")
        if any(item.released_at > self.as_of for item in self.released_7d):
            raise ValueError("release later than as_of")
        if any(
            alarm.event_at > self.as_of
            for feeder in self.feeders
            for alarm in feeder.current_alarms
        ):
            raise ValueError("alarm later than as_of")
        if any(
            alarm.cleared_at is not None and alarm.cleared_at > self.as_of
            for feeder in self.feeders
            for alarm in feeder.current_alarms
        ):
            raise ValueError("alarm cleared later than as_of")
        return self


class ObjectSchemeSummary(BaseModel):
    """One row of the scheme overview: the object's picket span and what is on it."""

    model_config = ConfigDict(extra="forbid")

    object_id: str = Field(min_length=1, max_length=256)
    object_name: str | None = None
    picket_min: FiniteFloat | None = Field(default=None, ge=0)
    picket_max: FiniteFloat | None = Field(default=None, ge=0)
    feeders: int = Field(ge=0)
    feeders_without_picket: int = Field(ge=0)
    open_cards: int = Field(ge=0)
    current_alarms: int = Field(ge=0)
    released_7d: int = Field(ge=0)

    @model_validator(mode="after")
    def check_summary(self) -> Self:
        if (self.picket_min is None) != (self.picket_max is None):
            raise ValueError("picket span bounds go together")
        if self.picket_min is not None and self.picket_max < self.picket_min:
            raise ValueError("picket span is reversed")
        if self.feeders_without_picket > self.feeders:
            raise ValueError("feeders without picket exceed feeders")
        return self


class ObjectSchemeList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ForecastMode
    as_of: AwareDatetime
    convention: Literal["условная шкала пикетов из названий каналов, 10 м на пикет; не план"] = (
        SCHEME_CONVENTION
    )
    items: list[ObjectSchemeSummary]
    total: int = Field(ge=0)
