"""In-app notifications (ТЗ §10, C0.2): the bell counts new forecast cards and new
critical source alarms since a moment.

Critical alarms follow a versioned policy of the technologist (fire, smoke outside a
detector test, gas outside a calibration, flooding). Until the policy is confirmed
``policy_confirmed`` is false; until the maintenance mask runs in runtime
``maintenance_check`` is ``not_applied`` and the UI says so. There is no sound, no external
channel and no «critical» styling in the data: only the kind and the rule text.
"""

from datetime import timedelta
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from infra_pulse_core.contracts.forecast import ForecastMode, TargetSpecId, check_card_wording

MAX_NOTIFICATION_ITEMS = 50
MAX_NOTIFICATION_WINDOW = timedelta(days=7)
MAX_ROW_MEMBERS = 50
NotificationKind = Literal["new_card", "critical_alarm"]
# C0.5 (policy critical-alarms-v1): a bell row is one record or a folded series.
# single — one record; test_series — detectors of one object, likely a test (weekday
# 07–19); calibration_series — gas sensors of one object, likely a calibration; chatter —
# repeats of one channel with one text within a clock hour, outside a series (flood sensors
# since v1, every rule since 29.09; the title reads «… ×N», the span is first_at–at). Folded rows
# hide nothing: they expand.
RowKind = Literal["single", "test_series", "calibration_series", "chatter"]
# Order of the list: a lower priority value is shown first («Обнаружен газ» outside a
# calibration series = 0, one record or repeats of one channel), then newest first.
PRIORITY_FIRST = 0
PRIORITY_DEFAULT = 1


class NotificationMember(BaseModel):
    """One source record of a bell row; open it with ``/api/v1/attention?row_uid=``.

    ``channel_name`` — the channel name from the channel layout (the active reference), as
    ``/attention`` shows it; none when the channel is not in the reference (QA 29.09, D3).
    """

    model_config = ConfigDict(extra="forbid")

    row_uid: str = Field(min_length=1, max_length=256)
    channel_id: str = Field(min_length=1, max_length=256)
    channel_name: str | None = None
    event_at: AwareDatetime
    value_raw: str


class NotificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: NotificationKind
    # Forecast card ID for new_card; source record row_uid for critical_alarm.
    ref_id: str = Field(min_length=1, max_length=256)
    object_id: str | None = None
    object_name: str | None = None
    title: str = Field(min_length=1, max_length=200)
    at: AwareDatetime
    target_spec_id: TargetSpecId | None = None
    rule_id: str | None = Field(default=None, min_length=1, max_length=64)
    rule_text: str | None = Field(default=None, max_length=200)
    priority: int = Field(default=PRIORITY_DEFAULT, ge=0, le=9)
    # Critical alarm rows only (C0.5): kind of row, how many records and channels it
    # folds, its first record time and its records (the newest 50, oldest first).
    row_kind: RowKind | None = None
    collapsed_count: int | None = Field(default=None, ge=1)
    channels_count: int | None = Field(default=None, ge=1)
    first_at: AwareDatetime | None = None
    members: list[NotificationMember] = Field(default_factory=list, max_length=MAX_ROW_MEMBERS)

    @model_validator(mode="after")
    def check_item(self) -> Self:
        check_card_wording(self.title)
        if self.kind == "new_card":
            if self.target_spec_id is None or self.rule_id is not None:
                raise ValueError("a new card names its target and no alarm rule")
            if self.row_kind is not None or self.members or self.collapsed_count is not None:
                raise ValueError("a new card is not a folded alarm row")
            return self
        if self.rule_id is None or self.rule_text is None or self.target_spec_id is not None:
            raise ValueError("a critical alarm names its rule and no forecast target")
        if self.row_kind is None or self.collapsed_count is None or self.first_at is None:
            raise ValueError("a critical alarm row states its kind, count and first record")
        if self.row_kind == "single" and self.collapsed_count != 1:
            raise ValueError("a single row holds one record")
        if len(self.members) != min(self.collapsed_count, MAX_ROW_MEMBERS):
            raise ValueError("members are the newest records of the row, up to 50")
        if self.first_at > self.at:
            raise ValueError("first record is later than the row time")
        if self.members:
            times = [member.event_at for member in self.members]
            if times != sorted(times):
                raise ValueError("members are oldest first")
            if self.members[-1].row_uid != self.ref_id or times[-1] != self.at:
                raise ValueError("the row refers to its newest record")
            if times[0] < self.first_at:
                raise ValueError("a member precedes the first record of the row")
        return self


class NotificationSummary(BaseModel):
    """Counts and the newest items after ``since`` up to ``as_of`` (the data watermark)."""

    model_config = ConfigDict(extra="forbid")

    mode: ForecastMode
    since: AwareDatetime
    as_of: AwareDatetime
    checked_at: AwareDatetime
    new_cards: int = Field(ge=0)
    critical_alarms: int = Field(ge=0)
    items: list[NotificationItem] = Field(default_factory=list, max_length=MAX_NOTIFICATION_ITEMS)
    truncated: bool
    policy_version: str = Field(min_length=1)
    policy_confirmed: bool = False
    maintenance_check: Literal["not_applied", "applied"]
    # Caption of the policy for the customer (C0.5), from the versioned policy JSON.
    policy_caption: str | None = Field(default=None, max_length=600)

    @model_validator(mode="after")
    def check_summary(self) -> Self:
        if self.since > self.as_of:
            raise ValueError("since is later than as_of")
        if self.as_of - self.since > MAX_NOTIFICATION_WINDOW:
            raise ValueError("notification window exceeds 7 days")
        if any(not self.since < item.at <= self.as_of for item in self.items):
            raise ValueError("item lies outside the notification window")
        order = [(item.priority, -item.at.timestamp()) for item in self.items]
        if order != sorted(order):
            raise ValueError("items are by priority, then newest first")
        cards = sum(item.kind == "new_card" for item in self.items)
        alarms = len(self.items) - cards
        if cards > self.new_cards or alarms > self.critical_alarms:
            raise ValueError("items exceed their counters")
        if self.truncated != (len(self.items) < self.new_cards + self.critical_alarms):
            raise ValueError("truncated flag differs from counters and items")
        return self
