"""Observed source messages and provisional dispatcher review order.

The review order is a workflow hint. It is never a model risk or a diagnosis.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    model_validator,
)


class ReceivedBatchMessage(BaseModel):
    """Candidate local JSON input; not an agreed customer exchange format."""

    model_config = ConfigDict(extra="forbid")

    source_record_id: str = Field(min_length=1, max_length=256)
    source_event_id: str | None = None
    channel_id: str = Field(min_length=1, max_length=256)
    object_id: str | None = None
    sensor_type: str | None = None
    system_type: str | None = None
    value_raw: str
    value_numeric: FiniteFloat | None = None
    alarm: StrictBool
    event_at: AwareDatetime
    reference_version: str | None = None


class ReceivedBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=128)
    records: list[ReceivedBatchMessage] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def check_record_ids(self) -> Self:
        ids = [record.source_record_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate source_record_id within batch")
        return self


class ObservedMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_uid: str = Field(min_length=1)
    source_event_id: str | None = None
    channel_id: str = Field(min_length=1)
    object_id: str | None = None
    # Dispatcher name of the object from the reference (C0.4), if known.
    object_name: str | None = None
    sensor_type: str | None = None
    system_type: str | None = None
    value_raw: str
    value_numeric: float | None = None
    # Source alarm flag as sent. None: the source format has no such column (ТЗ
    # Appendix 1 journal, G1) — «не передан», never «false»; alarm is not a failure.
    alarm: bool | None
    event_at: AwareDatetime
    available_at: AwareDatetime
    availability_basis: Literal["observed", "simulated"]
    source_namespace: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    source_file: str | None = None
    source_sha256: str | None = None
    record_ordinal: int | None = Field(default=None, ge=0)
    reference_version: str | None = None
    quality_flags: list[str] = Field(default_factory=list)
    # Picket of the channel for the picket-line scheme (C0.1): parsed from the channel name
    # or from reference data; unknown topology is never guessed (UI-06, 10 m per picket).
    picket_form: Literal["point", "range", "unknown"] = "unknown"
    picket_from: FiniteFloat | None = Field(default=None, ge=0)
    picket_to: FiniteFloat | None = Field(default=None, ge=0)
    picket_basis: Literal["reference", "channel_name", "synthetic"] | None = None
    # F6: name and line group of the channel from the channel layout, for «Сейчас» rows
    # («Освещение · ГРО2 ПК39» instead of the channel number); None — not in the layout.
    channel_name: str | None = None
    feeder_kind: Literal["lighting", "ventilation", "pumps", "ozk", "other"] | None = None

    @model_validator(mode="after")
    def check_picket(self) -> Self:
        if self.picket_form == "unknown":
            if (self.picket_from, self.picket_to, self.picket_basis) != (None, None, None):
                raise ValueError("unknown picket carries no picket values or basis")
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


class AttentionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: ObservedMessage
    review_order: int = Field(ge=1)
    attention_band: Literal["source_alarm", "watch_text", "chronological"]
    reason_codes: list[Literal["source_alarm_true", "exact_text_candidate", "received_order"]]
    policy_version: str
    policy_status: Literal["provisional"] = "provisional"
    local_review_revision: int = Field(default=0, ge=0)
    local_last_review_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_reasons(self) -> Self:
        if self.attention_band == "source_alarm":
            if not self.message.alarm or "source_alarm_true" not in self.reason_codes:
                raise ValueError("source alarm band requires the original alarm=true")
        elif self.attention_band == "watch_text":
            if self.message.alarm or "exact_text_candidate" not in self.reason_codes:
                raise ValueError("watch text band requires alarm=false and a candidate reason")
        elif self.message.alarm:
            raise ValueError("alarm=true cannot be placed in the chronological band")
        if (self.local_review_revision == 0) != (self.local_last_review_at is None):
            raise ValueError("local review revision and timestamp must agree")
        return self


class AttentionList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["fixture", "replay", "received", "live"]
    view: Literal["attention", "all"] = "attention"
    as_of: AwareDatetime
    received_watermark: int | None = Field(default=None, ge=0)
    received_after_watermark: int | None = Field(default=None, ge=0)
    policy_version: str
    policy_status: Literal["provisional"] = "provisional"
    items: list[AttentionEntry]
    offset: int = Field(default=0, ge=0)
    total: int = Field(ge=0)
    all_records_total: int = Field(ge=0)
    source_alarm_count: int = Field(ge=0)
    watch_text_count: int = Field(ge=0)
    chronological_count: int = Field(ge=0)

    @model_validator(mode="after")
    def check_items(self) -> Self:
        if (self.mode == "received") != (self.received_watermark is not None):
            raise ValueError("received pages require a committed import watermark")
        if self.received_after_watermark is not None:
            if self.received_watermark is None:
                raise ValueError("received lower bound requires received mode")
            if self.received_after_watermark > self.received_watermark:
                raise ValueError("received lower bound cannot exceed committed import watermark")
        if self.items and self.total < self.offset + len(self.items):
            raise ValueError("total cannot be smaller than the returned page")
        if self.total > self.all_records_total:
            raise ValueError("attention view cannot exceed all source records")
        if self.source_alarm_count + self.watch_text_count + self.chronological_count != self.total:
            raise ValueError("attention band counts must sum to the filtered total")
        if self.view == "attention" and self.chronological_count:
            raise ValueError("attention candidates cannot contain chronological records")
        if any(item.message.available_at > self.as_of for item in self.items):
            raise ValueError("queue contains a message unavailable at as_of")
        if [item.review_order for item in self.items] != list(
            range(self.offset + 1, self.offset + len(self.items) + 1)
        ):
            raise ValueError("review order must be contiguous")
        if any(item.policy_version != self.policy_version for item in self.items):
            raise ValueError("mixed attention policy versions")
        return self


class Capabilities(BaseModel):
    """What the running stand serves, read from its configuration and database.

    ``stage``: ``scaffold`` (no data source), ``fixture`` (synthetic data), ``not_ready``
    (replay/received scope not loaded or database unavailable), ``observations_only``
    (source records loaded, no forecast published yet), ``forecast_published`` (the
    worker published a forecast for the scope). ``inference_ready`` is true exactly for
    ``forecast_published``: forecasts are computed by the worker and read by the API;
    the API never trains or scores a model inside a request.
    """

    model_config = ConfigDict(extra="forbid")

    stage: Literal["scaffold", "fixture", "not_ready", "observations_only", "forecast_published"]
    mode: Literal["scaffold", "fixture", "replay", "received"]
    inference_ready: bool
    persistence_ready: bool
    authentication_ready: bool
    fixture_available: bool
    observations_ready: bool
    local_reviews_enabled: bool
    replay_window_start: AwareDatetime | None = None
    replay_window_end: AwareDatetime | None = None
    replay_rows: int | None = Field(default=None, ge=0)
    received_last_at: AwareDatetime | None = None
    received_rows: int | None = Field(default=None, ge=0)
    received_after_watermark: int | None = Field(default=None, ge=0)
    received_rows_after_watermark: int | None = Field(default=None, ge=0)
    received_source_alarms_after_watermark: int | None = Field(default=None, ge=0)
    received_candidates_after_watermark: int | None = Field(default=None, ge=0)
    received_candidate_groups_after_watermark: int | None = Field(default=None, ge=0)
    received_status_checked_at: AwareDatetime | None = None
    received_inbox_last_scan_at: AwareDatetime | None = None
    received_inbox_failure_count: int | None = Field(default=None, ge=0)
    received_inbox_last_failure_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_received_updates(self) -> Self:
        synthetic = ("scaffold", "fixture")
        if (self.stage in synthetic or self.mode in synthetic) and self.stage != self.mode:
            raise ValueError("scaffold and fixture stages are their modes")
        if self.inference_ready != (self.stage == "forecast_published"):
            raise ValueError("inference is ready exactly when a forecast is published")
        values = (
            self.received_after_watermark,
            self.received_rows_after_watermark,
            self.received_source_alarms_after_watermark,
            self.received_candidates_after_watermark,
            self.received_candidate_groups_after_watermark,
        )
        if any(value is None for value in values) != all(value is None for value in values):
            raise ValueError("received update counts require their watermark")
        if self.received_rows_after_watermark is not None:
            if self.mode != "received":
                raise ValueError("received update counts require received mode")
            if self.received_source_alarms_after_watermark > self.received_rows_after_watermark:
                raise ValueError("source alarm count cannot exceed received row count")
            if not (
                self.received_source_alarms_after_watermark
                <= self.received_candidates_after_watermark
                <= self.received_rows_after_watermark
            ):
                raise ValueError("candidate count must include alarms and fit received rows")
            if not (
                self.received_candidate_groups_after_watermark
                <= self.received_candidates_after_watermark
            ):
                raise ValueError("candidate groups cannot exceed candidate records")
            if bool(self.received_candidate_groups_after_watermark) != bool(
                self.received_candidates_after_watermark
            ):
                raise ValueError("candidate groups and records must both be empty or nonempty")
        return self


class ObjectAttentionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_id: str | None = None
    channel_count: int = Field(ge=0)
    record_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    source_alarm_count: int = Field(ge=0)
    last_available_at: AwareDatetime
    last_source_alarm_at: AwareDatetime | None
    last_watch_text_at: AwareDatetime | None
    attention_band: Literal["source_alarm", "watch_text", "chronological"]

    @model_validator(mode="after")
    def check_attention_band(self) -> Self:
        if not self.source_alarm_count <= self.candidate_count <= self.record_count:
            raise ValueError("object candidate counts must contain alarms and fit records")
        expected = (
            "source_alarm"
            if self.source_alarm_count
            else "watch_text"
            if self.candidate_count
            else "chronological"
        )
        if self.attention_band != expected:
            raise ValueError("object attention band must describe candidate presence")
        if (self.last_source_alarm_at is not None) != (self.source_alarm_count > 0):
            raise ValueError("object source alarm time must match source alarm count")
        if (self.last_watch_text_at is not None) != (
            self.candidate_count > self.source_alarm_count
        ):
            raise ValueError("object text candidate time must match candidate count")
        if any(
            value > self.last_available_at
            for value in (self.last_source_alarm_at, self.last_watch_text_at)
            if value is not None
        ):
            raise ValueError("object candidate time exceeds last available record")
        return self


class ObjectAttentionList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["replay", "received"]
    as_of: AwareDatetime
    received_watermark: int | None = Field(default=None, ge=0)
    items: list[ObjectAttentionSummary]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    candidate_kind: Literal["all", "alarm", "text", "candidate"]

    @model_validator(mode="after")
    def check_received_watermark(self) -> Self:
        if (self.mode == "received") != (self.received_watermark is not None):
            raise ValueError("received object groups require a committed import watermark")
        return self


class ChannelAttentionSummary(BaseModel):
    """A channel's latest received record, not an inferred current state."""

    model_config = ConfigDict(extra="forbid")

    object_id: str | None = None
    system_type: str | None = None
    channel_id: str = Field(min_length=1)
    record_count: int = Field(ge=1)
    candidate_count: int = Field(ge=0)
    source_alarm_count: int = Field(ge=0)
    last_source_alarm_at: AwareDatetime | None
    last_watch_text_at: AwareDatetime | None
    last_received_message: ObservedMessage
    last_received_group_count: int = Field(ge=1)
    latest_source_event_at: AwareDatetime
    source_time_regressed: bool
    multiple_object_ids_seen: bool

    @model_validator(mode="after")
    def check_latest_message(self) -> Self:
        if self.last_received_message.channel_id != self.channel_id:
            raise ValueError("channel summary and source message differ")
        if self.last_received_message.object_id != self.object_id:
            raise ValueError("object summary and source message differ")
        if self.last_received_message.system_type != self.system_type:
            raise ValueError("system summary and source message differ")
        if not self.source_alarm_count <= self.candidate_count <= self.record_count:
            raise ValueError("channel candidate counts must contain alarms and fit records")
        if (self.last_source_alarm_at is not None) != (self.source_alarm_count > 0):
            raise ValueError("channel source alarm time must match source alarm count")
        if (self.last_watch_text_at is not None) != (
            self.candidate_count > self.source_alarm_count
        ):
            raise ValueError("channel text candidate time must match candidate count")
        if any(
            value > self.last_received_message.available_at
            for value in (self.last_source_alarm_at, self.last_watch_text_at)
            if value is not None
        ):
            raise ValueError("channel candidate time exceeds last available record")
        return self


class ChannelAttentionList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["replay", "received"]
    as_of: AwareDatetime
    received_watermark: int | None = Field(default=None, ge=0)
    object_id: str | None = None
    items: list[ChannelAttentionSummary]
    offset: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def check_page(self) -> Self:
        if (self.mode == "received") != (self.received_watermark is not None):
            raise ValueError("received channel groups require a committed import watermark")
        if self.items and self.total < self.offset + len(self.items):
            raise ValueError("channel total cannot be smaller than returned page")
        if any(item.last_received_message.available_at > self.as_of for item in self.items):
            raise ValueError("channel view contains an unavailable message")
        return self


class SourceAlarmObject(BaseModel):
    """One object of the «Сейчас» window (F-04): every source alarm record of the window.

    ``scheme_record_count`` — records on the power lines drawn on the object's scheme
    (feeders of the channel layout); the scheme counts only these, the window every sensor.
    ``last_message`` — the latest record by event time, not by receipt.
    """

    model_config = ConfigDict(extra="forbid")

    object_id: str = Field(min_length=1)
    object_name: str | None = None
    record_count: int = Field(ge=1)
    channel_count: int = Field(ge=1)
    scheme_record_count: int = Field(ge=0)
    first_event_at: AwareDatetime
    last_event_at: AwareDatetime
    last_message: ObservedMessage

    @model_validator(mode="after")
    def check_counts(self) -> Self:
        if self.channel_count > self.record_count:
            raise ValueError("object channels exceed its records")
        if self.scheme_record_count > self.record_count:
            raise ValueError("records on scheme lines exceed the object records")
        if self.first_event_at > self.last_event_at:
            raise ValueError("object window starts after its last record")
        if self.last_message.object_id != self.object_id:
            raise ValueError("object summary and its last message differ")
        if self.last_message.event_at != self.last_event_at:
            raise ValueError("last message is not the latest record of the object")
        return self


class SourceAlarmWindow(BaseModel):
    """«Сейчас» (F-04): records with the source flag «тревожное» (``alarm=true``) whose event
    time is at or after ``event_from``, available at ``as_of``.

    Counts cover the whole window. ``objects`` and ``latest`` are ordered by event time,
    newest first; ``objects`` holds up to the requested number of objects, ``object_count``
    all of them. A source alarm is the source flag, not a confirmed failure.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["fixture", "replay", "received"]
    as_of: AwareDatetime
    received_watermark: int | None = Field(default=None, ge=0)
    event_from: AwareDatetime
    record_count: int = Field(ge=0)
    object_count: int = Field(ge=0)
    without_object_count: int = Field(ge=0)
    objects: list[SourceAlarmObject]
    latest: list[ObservedMessage]

    @model_validator(mode="after")
    def check_window(self) -> Self:
        if (self.mode == "received") != (self.received_watermark is not None):
            raise ValueError("received windows require a committed import watermark")
        if len(self.objects) > self.object_count:
            raise ValueError("more objects than counted in the window")
        if len({item.object_id for item in self.objects}) != len(self.objects):
            raise ValueError("an object is listed twice")
        listed = sum(item.record_count for item in self.objects) + self.without_object_count
        if listed > self.record_count or (
            len(self.objects) == self.object_count and listed != self.record_count
        ):
            raise ValueError("object records must fit the window")
        if len(self.latest) > self.record_count:
            raise ValueError("more latest records than the window holds")
        messages = [*self.latest, *(item.last_message for item in self.objects)]
        if any(
            message.alarm is not True
            or message.event_at < self.event_from
            or message.available_at > self.as_of
            for message in messages
        ):
            raise ValueError("window contains a record outside its bounds")
        for items in (
            [message.event_at for message in self.latest],
            [item.last_event_at for item in self.objects],
        ):
            if items != sorted(items, reverse=True):
                raise ValueError("window records must be ordered by event time, newest first")
        return self


class ReplayCoverageObject(BaseModel):
    """Reference channels heard within this bounded replay window."""

    model_config = ConfigDict(extra="forbid")

    object_id: str | None = None
    reference_channel_count: int = Field(ge=1)
    heard_channel_count: int = Field(ge=0)
    record_count: int = Field(ge=0)
    last_available_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_counts(self) -> Self:
        if self.heard_channel_count > self.reference_channel_count:
            raise ValueError("heard channels exceed reference channels")
        if (self.record_count == 0) != (self.last_available_at is None):
            raise ValueError("record count and last availability must agree")
        return self


class ReplayCoverageObjectList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: AwareDatetime
    window_start: AwareDatetime
    reference_sha256: str
    mapping_basis: Literal["current_reference_not_historical"] = "current_reference_not_historical"
    items: list[ReplayCoverageObject]
    total: int = Field(ge=0)


class ReplayCoverageChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    object_id: str | None = None
    system_type: str | None = None
    sensor_type: str | None = None
    record_count: int = Field(ge=0)
    last_available_at: AwareDatetime | None = None
    coverage_status: Literal["heard_in_replay_window", "no_record_in_replay_window"]

    @model_validator(mode="after")
    def check_coverage(self) -> Self:
        if (self.record_count > 0) != (self.coverage_status == "heard_in_replay_window"):
            raise ValueError("coverage status and records differ")
        if (self.record_count == 0) != (self.last_available_at is None):
            raise ValueError("record count and last availability differ")
        return self


class ReplayCoverageChannelList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: AwareDatetime
    window_start: AwareDatetime
    reference_sha256: str
    mapping_basis: Literal["current_reference_not_historical"] = "current_reference_not_historical"
    object_id: str | None = None
    items: list[ReplayCoverageChannel]
    offset: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def check_page(self) -> Self:
        if self.items and self.total < self.offset + len(self.items):
            raise ValueError("coverage total cannot be smaller than the returned page")
        return self


class ReviewNoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
    expected_revision: int = Field(ge=0)
    view_as_of: AwareDatetime
    displayed_received_watermark: int | None = Field(default=None, ge=0)
    displayed_snapshot_id: str = Field(min_length=1)
    displayed_policy_version: str = Field(min_length=1)
    action_text: str = Field(min_length=1, max_length=500)
    result_text: str = Field(min_length=1, max_length=1000)
    reason_text: str = Field(min_length=1, max_length=1000)


class ReviewNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note_id: UUID
    row_uid: str
    revision: int = Field(ge=1)
    actor_id: str
    action_text: str
    result_text: str
    reason_text: str
    created_at: AwareDatetime
    view_as_of: AwareDatetime | None = None
    displayed_received_watermark: int | None = Field(default=None, ge=0)
    policy_version: str | None = None
    attention_band: Literal["source_alarm", "watch_text", "chronological"] | None = None
    reason_codes: (
        list[Literal["source_alarm_true", "exact_text_candidate", "received_order"]] | None
    ) = None


class ReviewNoteList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["replay", "received"]
    row_uid: str
    revision: int = Field(ge=0)
    items: list[ReviewNote]


class ReviewJournalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: ReviewNote
    message: ObservedMessage

    @model_validator(mode="after")
    def check_identity(self) -> Self:
        if self.note.row_uid != self.message.row_uid:
            raise ValueError("review note and source message differ")
        return self


class ReviewJournalList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["replay", "received"]
    items: list[ReviewJournalEntry]
    offset: int = Field(ge=0)
    total: int = Field(ge=0)
    review_watermark: int = Field(ge=0)

    @model_validator(mode="after")
    def check_page(self) -> Self:
        if self.items and self.total < self.offset + len(self.items):
            raise ValueError("journal total cannot be smaller than the returned page")
        if self.total > self.review_watermark:
            raise ValueError("journal total exceeds its committed watermark")
        return self
