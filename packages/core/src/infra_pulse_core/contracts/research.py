"""Aggregated research results for the «Исследование» page; not a production forecast.

Every number comes from a checked-in aggregated report (no IDs) with its evaluation
period and how many times that period was already used. Scopes with too few events
say «нужно больше данных» instead of showing a metric.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ResearchStatus = Literal["in_product", "research_only", "needs_more_data"]
LadderStep = Literal["ceiling", "pair_oracle", "model", "static_list", "persistence", "random_list"]
RESEARCH_STATUS_LABELS: dict[str, str] = {
    "in_product": "прогноз в продукте",
    "research_only": "только исследование",
    "needs_more_data": "нужно больше данных",
}


class ResearchMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value: float | None = Field(default=None, ge=0, le=1)
    ci_low: float | None = Field(default=None, ge=0, le=1)
    ci_high: float | None = Field(default=None, ge=0, le=1)
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_interval(self) -> Self:
        if (self.ci_low is None) != (self.ci_high is None):
            raise ValueError("interval bounds go together")
        if self.ci_low is not None and self.value is not None:
            if not self.ci_low <= self.value <= self.ci_high:
                raise ValueError("value lies outside its interval")
        return self


class ResearchLadderRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: LadderStep
    label: str = Field(min_length=1, max_length=120)
    metrics: list[ResearchMetric] = Field(default_factory=list)


class ResearchScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    sensor_types: list[str] = Field(default_factory=list)
    status: ResearchStatus
    horizon_days: int | None = Field(default=None, ge=1)
    budget_k: int | None = Field(default=None, ge=1)
    policy: str | None = Field(default=None, max_length=120)
    evaluation_period: str = Field(min_length=1, max_length=120)
    period_use_count: int | None = Field(default=None, ge=1)
    events_per_year: int | None = Field(default=None, ge=0)
    ladder: list[ResearchLadderRow] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def check_scope(self) -> Self:
        if self.status == "needs_more_data" and self.ladder:
            raise ValueError("a scope that needs more data shows no metrics")
        return self


Cell = str | int | float | None


class ResearchColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=120)
    unit: str | None = Field(default=None, max_length=32)


class ResearchBlock(BaseModel):
    """A read-only titled table (e.g. a registry proposed by DS); nothing is hard-coded.

    Blocks are optional and appear only when the served summary contains them.
    """

    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=160)
    caption: str | None = Field(default=None, max_length=500)
    status: ResearchStatus
    columns: list[ResearchColumn] = Field(default_factory=list)
    rows: list[dict[str, Cell]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_table(self) -> Self:
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate column")
        if any(set(row) - set(keys) for row in self.rows):
            raise ValueError("row has a cell outside the columns")
        return self


class ResearchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    synthetic: bool
    source_reports: list[str] = Field(default_factory=list)
    scopes: list[ResearchScope] = Field(min_length=1)
    blocks: list[ResearchBlock] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_sources(self) -> Self:
        if not self.synthetic and not self.source_reports:
            raise ValueError("real research numbers name their source reports")
        ids = [scope.scope_id for scope in self.scopes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate research scope")
        blocks = [block.block_id for block in self.blocks]
        if len(blocks) != len(set(blocks)):
            raise ValueError("duplicate research block")
        return self
