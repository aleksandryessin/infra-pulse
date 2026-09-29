from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Factor(BaseModel):
    feature: str
    description: str
    contribution: float | None = None


class Risk(BaseModel):
    """Score semantics are explicit; an anomaly score is never a failure probability."""

    model_config = ConfigDict(extra="forbid")

    id: str
    channel_id: str
    object_id: str | None = None
    engineering_tag: str | None = None
    as_of: AwareDatetime
    data_cutoff: AwareDatetime
    target_start: AwareDatetime | None = None
    target_end: AwareDatetime | None = None
    assessment_kind: Literal["condition_assessment", "forecast"]
    label_type: Literal[
        "unlabeled_anomaly",
        "technical_fault_state_proxy",
        "alarm_activation_proxy",
        "confirmed_failure",
    ]
    status: Literal["scored", "abstained"]
    score_kind: Literal["anomaly_score", "probability"] | None = None
    score: float | None = Field(default=None, ge=0, le=1)
    risk_level: Literal["low", "medium", "high", "unknown"]
    abstention_reason: str | None = None
    model_version: str
    feature_version: str
    label_version: str
    policy_version: str
    calibration_version: str | None = None
    source: Literal["synthetic_fixture", "historical_replay", "live"]
    factors: list[Factor] = Field(default_factory=list)
    recommendation: str | None = None

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        if self.data_cutoff > self.as_of:
            raise ValueError("data_cutoff cannot be later than as_of")
        if self.assessment_kind == "forecast":
            if self.target_start is None or self.target_end is None:
                raise ValueError("forecast requires a target window")
            if not self.as_of <= self.target_start < self.target_end:
                raise ValueError("invalid target window")
            if self.label_type == "unlabeled_anomaly":
                raise ValueError("anomaly detection alone is not a forecast")
        else:
            if self.target_start is not None or self.target_end is not None:
                raise ValueError("condition assessment has no future target window")
            if self.label_type != "unlabeled_anomaly":
                raise ValueError("condition assessment uses unlabeled_anomaly")
        if self.status == "abstained":
            if (
                self.score is not None
                or self.score_kind is not None
                or self.risk_level != "unknown"
            ):
                raise ValueError("abstention requires null score/kind and unknown risk")
            if not self.abstention_reason:
                raise ValueError("abstention needs a reason")
        elif self.score is None or self.score_kind is None or self.risk_level == "unknown":
            raise ValueError("scored assessment requires a score, kind and risk level")
        elif self.abstention_reason is not None:
            raise ValueError("scored assessment cannot carry an abstention reason")
        if self.score_kind == "probability" and self.calibration_version is None:
            raise ValueError("probability requires recorded calibration")
        if self.score_kind == "probability" and self.label_type == "unlabeled_anomaly":
            raise ValueError("unlabeled anomaly score cannot be called probability")
        return self


class RiskList(BaseModel):
    mode: Literal["fixture"] = "fixture"
    items: list[Risk]
    total: int
