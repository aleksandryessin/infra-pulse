"""Management report (ТЗ §8, optional, C0.2): monthly summary as JSON; XLSX renders the
same numbers (package R1). PDF is printed from the browser page. Counters are registered
events, not a quality metric; quality ratios stay on the research side.
"""

from datetime import date
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from infra_pulse_core.contracts.forecast import DecisionCode, ForecastMode, TargetSpecId

MAX_TOP_OBJECTS = 10


class ReportCardCounts(BaseModel):
    """выдано = снята по событию + без события + неизвестно + открыто."""

    model_config = ConfigDict(extra="forbid")

    target_spec_id: TargetSpecId
    issued: int = Field(ge=0)
    released: int = Field(ge=0)
    no_event: int = Field(ge=0)
    unknown: int = Field(ge=0)
    open: int = Field(ge=0)

    @model_validator(mode="after")
    def check_counts(self) -> Self:
        if self.released + self.no_event + self.unknown + self.open != self.issued:
            raise ValueError("card counters do not add up to issued cards")
        return self


class ReportDecisionCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_code: DecisionCode
    count: int = Field(ge=0)


class ReportObjectRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_id: str = Field(min_length=1, max_length=256)
    object_name: str | None = None
    cards: int = Field(ge=0)
    released: int = Field(ge=0)
    source_alarms: int = Field(ge=0)

    @model_validator(mode="after")
    def check_row(self) -> Self:
        if self.released > self.cards:
            raise ValueError("released cards exceed cards")
        return self


class ReportAlarmTypeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sensor_type: str = Field(min_length=1)
    source_alarms: int = Field(ge=0)


class MonthlyReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ForecastMode
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    period_start: date
    period_end: date
    generated_at: AwareDatetime
    data_as_of: AwareDatetime | None = None
    basis: Literal["automatic_registered_event"] = "automatic_registered_event"
    cards: list[ReportCardCounts] = Field(default_factory=list)
    decisions: list[ReportDecisionCount] = Field(default_factory=list)
    top_objects: list[ReportObjectRow] = Field(default_factory=list, max_length=MAX_TOP_OBJECTS)
    alarms_by_type: list[ReportAlarmTypeRow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_report(self) -> Self:
        if self.period_start >= self.period_end:
            raise ValueError("report period is empty or reversed")
        if self.period_start.strftime("%Y-%m") != self.month or self.period_start.day != 1:
            raise ValueError("report period starts on the first day of its month")
        targets = [row.target_spec_id for row in self.cards]
        if len(targets) != len(set(targets)):
            raise ValueError("one card row per target")
        codes = [row.decision_code for row in self.decisions]
        if len(codes) != len(set(codes)):
            raise ValueError("one row per decision code")
        return self
