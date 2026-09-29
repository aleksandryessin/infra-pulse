"""C0.5: folded bell rows (single, test series, calibration series, chatter), their
records for expansion, priority «газ первым» and the policy caption."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_backend.operations import notifications as policy
from infra_pulse_core.contracts.notifications import (
    MAX_ROW_MEMBERS,
    NotificationItem,
    NotificationSummary,
)

MSK = timezone(timedelta(hours=3))
T0 = datetime(2026, 9, 24, 9, 0, tzinfo=MSK)


def record(index: int, minutes: int, sensor: str, value: str, **extra) -> policy.AlarmRecord:
    return policy.AlarmRecord(
        row_uid=f"synthetic-row-{index:04d}",
        channel_id=extra.pop("channel", f"synthetic-channel-{index:04d}"),
        object_id=extra.pop("object_id", "synthetic-object-1"),
        sensor_type=sensor,
        value_raw=value,
        alarm=True,
        event_at=T0 + timedelta(minutes=minutes),
        **extra,
    )


def test_fixture_bell_rows_expand_and_put_gas_first():
    client = TestClient(create_app(Settings(mode="fixture", _env_file=None)))
    summary = NotificationSummary.model_validate(
        client.get("/api/v1/notifications", params={"since": "2026-09-24T00:00:00+03:00"}).json()
    )
    assert summary.policy_caption and "Синтетика" in summary.policy_caption
    first = summary.items[0]
    assert (first.row_kind, first.priority, first.rule_id) == ("single", 0, "gas_detected")
    series = next(item for item in summary.items if item.row_kind == "test_series")
    assert (series.collapsed_count, series.channels_count, len(series.members)) == (6, 6, 6)
    assert series.members[-1].row_uid == series.ref_id
    cards = [item for item in summary.items if item.kind == "new_card"]
    assert cards and all(item.row_kind is None and not item.members for item in cards)


def test_policy_fills_rows_from_member_records():
    fire = [record(i, i, "Датчик дыма", "Обнаружен дым") for i in range(1, 7)]  # weekday 09:0x
    gas = record(50, -120, "Газовый датчик", "Обнаружен газ", object_id="synthetic-object-2")
    flood = [
        record(60 + i, 30 + i, "Датчик затопления", "Не замкнут", channel="synthetic-flood")
        for i in range(3)
    ]
    rows = policy.alarm_rows([*fire, gas, *flood])
    items = {row.kind: policy.alarm_item(row) for row in rows}
    series = items["test_series"]
    assert (series.collapsed_count, series.channels_count) == (6, 6)
    assert [member.row_uid for member in series.members] == [r.row_uid for r in fire]
    assert series.first_at == fire[0].event_at and series.at == fire[-1].event_at
    chatter = items["chatter"]
    assert (chatter.collapsed_count, chatter.channels_count, len(chatter.members)) == (3, 1, 3)
    single = items["single"]
    assert (single.priority, single.collapsed_count, single.members[0].value_raw) == (
        0,
        1,
        "Обнаружен газ",
    )


def test_long_rows_carry_the_newest_fifty_records():
    flood = [
        record(i, i % 60, "Датчик затопления", "Не замкнут", channel="synthetic-flood")
        for i in range(1, 60)
    ]
    item = policy.alarm_item(policy.alarm_rows(flood)[0])
    assert item.collapsed_count == 59 and len(item.members) == MAX_ROW_MEMBERS
    assert item.members[-1].row_uid == item.ref_id


def test_item_and_summary_rules():
    base = policy.alarm_item(policy.alarm_rows([record(1, 0, "Датчик дыма", "Обнаружен дым")])[0])
    payload = base.model_dump()
    cases = [
        (payload | {"row_kind": None}, "kind, count and first record"),
        (payload | {"collapsed_count": 2}, "single row holds one record"),
        (payload | {"members": []}, "newest records of the row"),
        (payload | {"ref_id": "other"}, "newest record"),
        (payload | {"first_at": payload["at"] + timedelta(minutes=1)}, "first record is later"),
    ]
    for data, message in cases:
        with pytest.raises(ValidationError, match=message):
            NotificationItem.model_validate(data)
    with pytest.raises(ValidationError, match="by priority, then newest first"):
        NotificationSummary(
            mode="fixture",
            since=base.at - timedelta(hours=1),
            as_of=base.at + timedelta(hours=1),
            checked_at=base.at + timedelta(hours=1),
            new_cards=0,
            critical_alarms=2,
            items=[base, base.model_copy(update={"priority": 0})],
            truncated=False,
            policy_version="v",
            maintenance_check="not_applied",
        )
