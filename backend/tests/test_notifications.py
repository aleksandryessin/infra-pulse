"""N1 notifications without a database: policy v1, rows, window and summary.

ТЗ §10, UI-03 (in-app notifications), OUT-03 (no sound or external channel). Policy
``critical-alarms-v1`` of the technologist and the internal acceptance review, one test per
rule on synthetic records:

- (а) pump «Затоплен» next to «Обесточен»/«Неисправен» of the object is not critical;
- (б) ≥ 5 detector channels of an object within 10 min on a weekday 07–19 fold into
  «Похоже на ППР или ТО: N извещателей — сверить с графиком»; night and weekend series
  stay single rows;
- (в) ≥ 5 gas channels fold into «Похоже на ППР или ТО: N газоанализаторов — сверить с
  графиком»; a single
  «Обнаружен газ» at any ``alarm`` is selected first and never cut from the list;
- (г) flood-sensor «Не замкнут» chatter is one row per channel and clock hour;
- repeats (29.09): the same folding for every rule — records of one channel with one text
  within a clock hour are one row «… ×N» (first_at–at); checked on a synthetic copy of the
  30.06.2026 window of the stand copy (33 rows → 17).

Nothing is hidden: a folded row keeps the IDs of all its records. All data is synthetic.
"""

import json
import re
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from infra_pulse_backend.api import forecast_db, forecast_fixture
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.api.auth_deps import current_actor
from infra_pulse_backend.config import Settings
from infra_pulse_backend.ingestion.journal_csv import MSK
from infra_pulse_backend.operations import notifications as policy
from infra_pulse_backend.operations.notifications import (
    AlarmRecord,
    NotificationWindowError,
    PowerEvent,
    StandClock,
    StandClockUnavailable,
    alarm_rows,
    build_summary,
    check_window,
    match_rule,
    new_cards,
    series_members,
    stand_clock,
    window_rows,
    with_power_context,
    working_hours,
)
from infra_pulse_core.contracts.auth import Me
from infra_pulse_core.contracts.forecast import ForecastState, check_card_wording

ROOT = Path(__file__).resolve().parents[2]
# Thursday 24.09.2026 12:00 MSK: working hours.
T0 = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
NIGHT = datetime(2026, 9, 24, 20, 30, tzinfo=UTC)  # Thursday 23:30 MSK
SATURDAY = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)  # Saturday 12:00 MSK
SMOKE = ("Датчик дыма", "Обнаружен дым")
HEAT = ("Тепловой датчик", "Не замкнут")
MANUAL = ("Ручной извещатель", "Рычаг сдернут")
GAS = ("Газовый датчик", "Обнаружен газ")
PUMP = ("Состояние насоса", "Затоплен")
FLOOD = ("Датчик затопления", "Не замкнут")


def record(
    number: int,
    minutes: float,
    pair: tuple[str, str] = SMOKE,
    *,
    object_id: str | None = "synthetic-object-01",
    channel: str | None = None,
    alarm: bool = False,
    start: datetime = T0,
) -> AlarmRecord:
    return AlarmRecord(
        row_uid=f"synthetic-row-{number:04d}",
        channel_id=channel or f"synthetic-channel-{number:04d}",
        object_id=object_id,
        sensor_type=pair[0],
        value_raw=pair[1],
        alarm=alarm,
        event_at=start + timedelta(minutes=minutes),
    )


def clock(data_as_of: datetime, *, as_of: datetime | None = None) -> StandClock:
    return StandClock(data_as_of=data_as_of, available_as_of=data_as_of, as_of=as_of or data_as_of)


def summary_of(records, *, since=T0 - timedelta(hours=1), data_as_of=T0 + timedelta(days=3)):
    rows = window_rows(alarm_rows(records), since=since, data_as_of=data_as_of)
    return build_summary(
        mode="replay",
        since=since,
        clock=clock(data_as_of),
        checked_at=data_as_of,
        cards=[],
        alarms=sorted(rows, key=policy.selection_key)[:50],
        alarms_total=len(rows),
    )


# --- policy -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pair", "rule_id"),
    [
        (SMOKE, "fire_smoke"),
        (HEAT, "fire_heat"),
        (("Ручной извещатель", "Не замкнут"), "fire_manual"),
        (MANUAL, "fire_manual"),
        (GAS, "gas_detected"),
        (PUMP, "flood_pump"),
        (FLOOD, "flood_sensor"),
    ],
)
def test_candidate_texts_match_exactly(pair, rule_id):
    sensor_type, value_raw = pair
    assert match_rule(sensor_type, value_raw).rule_id == rule_id
    assert match_rule(sensor_type, f" {value_raw}") is None  # verbatim text, no trimming
    assert match_rule(sensor_type, value_raw.lower()) is None


@pytest.mark.parametrize(
    ("sensor_type", "value_raw"),
    [
        ("КД Дверь", "Не замкнут"),
        ("КД Люк", "Не замкнут"),
        ("КД АВ", "Не замкнут"),
        ("Датчик дыма", "Неисправен"),
        ("Газовый датчик", "Неисправен"),
        ("Состояние насоса", "Обесточен"),
        ("Состояние фазы", "Обесточен"),
        ("Датчик температуры", "Температура ниже 3ºC"),
        ("Датчик температуры", "Температура выше 40ºC"),
        ("Датчик температуры", "Обнаружен газ"),  # a foreign lexeme of another type
        ("Датчик затопления", "Затоплен"),  # not in v1: the journal has «Не замкнут»
        ("Датчик дыма", "Дыма нет"),
        (None, "Обнаружен дым"),
    ],
)
def test_not_critical_texts(sensor_type, value_raw):
    assert match_rule(sensor_type, value_raw) is None


def test_policy_v1_is_versioned_json_and_unconfirmed():
    document = json.loads(json.dumps(policy.NOTIFICATION_POLICY, ensure_ascii=False))
    assert document["policy_version"] == policy.POLICY_VERSION == "critical-alarms-v1"
    assert document["policy_confirmed"] is False
    assert document["maintenance_check"] == "not_applied"
    # Customer answer of 28.09: the caption names the selection honestly (C0.5, ≤ 600).
    caption = document["caption"]
    assert caption.startswith("Колокольчик — сообщения источника, которые по тексту могут")
    assert "угрозу жизни людей (классификация заказчика, 28.09)" in caption
    assert "Сейчас в списке пожар, газ и затопление" in caption
    assert "охранные сработки и аномальная температура пока не входят" in caption
    assert "проверяют диспетчер и дежурный инженер: это не подтверждённая авария" in caption
    assert len(caption) <= 600
    assert document["series"]["fire_test_series"]["local_hours"] == [7, 19]
    rule_ids = [rule["rule_id"] for rule in document["rules"]]
    assert len(rule_ids) == len(set(rule_ids))
    for rule in policy.CRITICAL_RULES:
        assert len(rule.text) <= 200 and "критич" not in rule.text.lower()
        # rule_text is shown in the bell: «тревожное сообщение», not «тревога» (28.09).
        assert "тревог" not in rule.text.lower()


def test_client_group_labels_match_the_policy_rules():
    """ALARM_GROUP_LABELS in frontend labels.ts is keyed by the rule_id of this policy."""
    source = (ROOT / "frontend/src/shared/config/labels.ts").read_text(encoding="utf-8")
    block = re.search(r"ALARM_GROUP_LABELS[^{]*\{(.*?)\n\};", source, re.S)
    assert block is not None
    labels = dict(re.findall(r"^\s*(\w+): '([^']+)',$", block.group(1), re.M))
    group = {"fire": "Пожар", "gas": "Газ", "pump": "Затопление", "flood_sensor": "Затопление"}
    expected = {
        rule["rule_id"]: group[rule["family"]] for rule in policy.NOTIFICATION_POLICY["rules"]
    }
    for rule_id, series in policy.NOTIFICATION_POLICY["series"].items():
        (family,) = series["families"]
        expected[rule_id] = group[family]
    assert labels == expected


def test_working_hours_are_weekday_07_to_19_msk():
    assert working_hours(T0)
    assert working_hours(datetime(2026, 9, 24, 4, 0, tzinfo=UTC))  # 07:00 MSK
    assert not working_hours(datetime(2026, 9, 24, 3, 59, tzinfo=UTC))  # 06:59 MSK
    assert not working_hours(datetime(2026, 9, 24, 16, 0, tzinfo=UTC))  # 19:00 MSK
    assert not working_hours(NIGHT)
    assert not working_hours(SATURDAY)


# --- (а) pump «Затоплен» -----------------------------------------------------------------


def test_a_flooded_pump_next_to_power_loss_is_not_critical():
    pumps = [
        record(1, 0, PUMP, object_id="synthetic-object-01"),
        record(2, 0, PUMP, object_id="synthetic-object-02"),
        record(3, 0, PUMP, object_id=None, channel="synthetic-pump-03"),
        record(4, 0, PUMP, object_id="synthetic-object-04"),
    ]
    power = [
        # Another channel of object 01, 9 minutes later: power-loss signature.
        PowerEvent("synthetic-object-01", "synthetic-phase-01", T0 + timedelta(minutes=9)),
        # Object 02: «Неисправен» 11 minutes earlier — outside ±10 min.
        PowerEvent("synthetic-object-02", "synthetic-pump-02", T0 - timedelta(minutes=11)),
        # No object: the same channel counts.
        PowerEvent(None, "synthetic-pump-03", T0 - timedelta(minutes=2)),
        # Object 04: a power event of another object does not count.
        PowerEvent("synthetic-object-99", "synthetic-pump-04", T0),
    ]
    rows = alarm_rows(with_power_context(pumps, power))
    assert {row.ref_id for row in rows} == {"synthetic-row-0002", "synthetic-row-0004"}
    assert all(row.rule_id == "flood_pump" and row.kind == "single" for row in rows)
    assert all("±10 мин" in row.rule_text for row in rows)


# --- (б) detector test series ------------------------------------------------------------


def test_b_detector_series_on_a_weekday_folds_into_one_row():
    pairs = [SMOKE, HEAT, SMOKE, MANUAL, SMOKE, SMOKE]
    series = [record(index, index * 1.5, pair) for index, pair in enumerate(pairs, start=1)]
    series.append(record(7, 9.5, SMOKE, channel="synthetic-channel-0001"))  # repeat channel
    lone = record(20, 45, HEAT)
    rows = alarm_rows([*series, lone])
    folded = [row for row in rows if row.kind == "test_series"]
    assert len(folded) == 1 and len(rows) == 2
    (row,) = folded
    assert row.title == "Похоже на ППР или ТО: 6 извещателей — сверить с графиком"
    assert row.rule_id == "fire_test_series" and row.channels == 6 and row.records == 7
    assert row.member_ids == tuple(record.row_uid for record in series)  # nothing hidden
    assert row.ref_id == "synthetic-row-0007" and row.at == series[-1].event_at
    assert "7 записей" in row.rule_text and "раскрывается" in row.rule_text
    check_card_wording(row.title)
    assert len(row.rule_text) <= 200
    single = next(row for row in rows if row.kind == "single")
    assert single.ref_id == lone.row_uid and single.rule_id == "fire_heat"


@pytest.mark.parametrize("start", [NIGHT, SATURDAY])
def test_b_night_and_weekend_series_are_not_marked(start):
    series = [record(index, index, SMOKE, start=start) for index in range(1, 7)]
    rows = alarm_rows(series)
    assert len(rows) == 6 and {row.kind for row in rows} == {"single"}
    assert all("ППР или ТО" not in row.title for row in rows)


def test_b_four_channels_or_a_longer_span_are_not_a_series():
    four = [record(index, index * 2) for index in range(4)]
    assert series_members(four) == set()
    spread = [record(index, minutes) for index, minutes in enumerate([0, 3, 6, 9, 10.02])]
    assert series_members(spread) == set()
    mixed = [record(index, index, object_id=f"synthetic-object-{index}") for index in range(6)]
    assert len(alarm_rows(mixed)) == 6
    unbound = [record(index, index, object_id=None) for index in range(6)]
    assert len(alarm_rows(unbound)) == 6


def test_b_series_crossing_since_is_one_row_in_the_window():
    series = [record(index, index * 2 - 3) for index in range(1, 7)]  # first before T0
    summary = summary_of(series, since=T0)
    assert summary.critical_alarms == 1
    (item,) = summary.items
    assert item.title == "Похоже на ППР или ТО: 6 извещателей — сверить с графиком"
    assert item.at == series[-1].event_at


# --- (в) gas -----------------------------------------------------------------------------


def test_c_gas_series_folds_into_calibration_row():
    series = [record(index, index, GAS, alarm=index % 2 == 0) for index in range(1, 6)]
    rows = alarm_rows(series)
    (row,) = rows
    assert (row.kind, row.rule_id) == ("calibration_series", "gas_calibration_series")
    assert row.title == "Похоже на ППР или ТО: 5 газоанализаторов — сверить с графиком"
    assert row.records == 5
    assert row.member_ids == tuple(record.row_uid for record in series)
    # A gas series is folded at any hour, unlike detector tests.
    night = [record(10 + index, index, GAS, start=NIGHT) for index in range(5)]
    assert [row.kind for row in alarm_rows(night)] == ["calibration_series"]


def test_c_single_gas_at_any_alarm_is_selected_first():
    gas = [
        record(1, 0, GAS, alarm=False, object_id="synthetic-object-gas"),
        record(2, 1, GAS, alarm=True, object_id="synthetic-object-gas-2"),
    ]
    smoke = [
        record(100 + index, 10 + index, object_id=f"synthetic-object-{index}")
        for index in range(80)
    ]
    summary = summary_of([*gas, *smoke])
    assert summary.critical_alarms == 82 and len(summary.items) == 50 and summary.truncated
    listed = {item.ref_id for item in summary.items}
    assert {"synthetic-row-0001", "synthetic-row-0002"} <= listed  # older, never cut
    rules = {item.ref_id: item.rule_id for item in summary.items}
    assert rules["synthetic-row-0001"] == rules["synthetic-row-0002"] == "gas_detected"
    # C0.5: single gas records are listed first (priority 0), then newest first.
    assert [item.ref_id for item in summary.items[:2]] == [
        "synthetic-row-0002",
        "synthetic-row-0001",
    ]
    assert {item.priority for item in summary.items[:2]} == {0}
    rest = [item.at for item in summary.items[2:]]
    assert rest == sorted(rest, reverse=True)


# --- (г) flood sensor chatter -------------------------------------------------------------


def test_d_flood_sensor_chatter_is_one_row_per_channel_and_hour():
    chatter = [
        record(index, index * 7, FLOOD, channel="synthetic-flood-01") for index in range(1, 6)
    ]
    # 12:07–12:35 MSK: one hour bucket, five records.
    next_hour = record(10, 61, FLOOD, channel="synthetic-flood-01")  # 13:01 MSK
    other = record(11, 3, FLOOD, channel="synthetic-flood-02")
    rows = alarm_rows([*chatter, next_hour, other])
    by_ref = {row.ref_id: row for row in rows}
    assert len(rows) == 3
    first = by_ref["synthetic-row-0005"]
    assert (first.kind, first.records) == ("chatter", 5)
    assert first.title == "Датчик затопления: «Не замкнут» ×5"
    assert "5 записей одного канала за час, 12:07–12:35 МСК" in first.rule_text
    assert first.member_ids == tuple(record.row_uid for record in chatter)
    lone = by_ref["synthetic-row-0010"]
    assert (lone.kind, lone.title) == ("single", "Датчик затопления: «Не замкнут»")
    assert by_ref["synthetic-row-0011"].records == 1


# --- repeats of one channel (29.09) ------------------------------------------------------


def test_repeats_of_one_channel_fold_into_one_row_per_clock_hour():
    """Display only: every rule folds like (г); a lone record stays a single row."""
    smoke = [
        record(index, index * 6, channel="synthetic-detector-01", start=NIGHT)
        for index in range(1, 6)
    ]  # 23:36–00:00 MSK crosses a clock hour: 4 + 1
    manual = [
        record(20, 1, ("Ручной извещатель", "Не замкнут"), channel="synthetic-manual-01"),
        record(21, 2, MANUAL, channel="synthetic-manual-01"),  # another text of the channel
        record(22, 3, MANUAL, channel="synthetic-manual-01"),
    ]
    pumps = [record(30 + index, index, PUMP, channel="synthetic-pump-01") for index in range(3)]
    rows = alarm_rows([*smoke, *manual, *pumps])
    by_ref = {row.ref_id: row for row in rows}
    assert len(rows) == 5
    night = by_ref["synthetic-row-0004"]
    assert (night.kind, night.rule_id, night.records, night.channels) == (
        "chatter",
        "fire_smoke",
        4,
        1,
    )
    assert night.title == "Датчик дыма: «Обнаружен дым» ×4"
    assert night.member_ids == tuple(record.row_uid for record in smoke[:4])
    assert night.first_at == smoke[0].event_at and night.at == smoke[3].event_at
    assert "4 записи одного канала за час, 23:36–23:54 МСК" in night.rule_text
    assert by_ref["synthetic-row-0005"].kind == "single"  # 00:00 is the next clock hour
    assert by_ref["synthetic-row-0020"].kind == "single"
    lever = by_ref["synthetic-row-0022"]
    assert (lever.kind, lever.records, lever.rule_id) == ("chatter", 2, "fire_manual")
    assert lever.title == "Ручной извещатель: «Рычаг сдернут» ×2"
    pump = by_ref["synthetic-row-0032"]
    assert (pump.kind, pump.records, pump.rule_id) == ("chatter", 3, "flood_pump")
    for row in rows:
        check_card_wording(row.title)
        assert len(row.title) <= 200 and len(row.rule_text) <= 200
        policy.alarm_item(row)  # the contract accepts every row
    folded = {uid for row in rows for uid in row.member_ids}
    assert folded == {item.row_uid for item in [*smoke, *manual, *pumps]}  # nothing dropped


def test_repeated_gas_of_one_channel_keeps_the_first_priority():
    gas = [record(index, index * 10, GAS, channel="synthetic-gas-01") for index in range(1, 4)]
    smoke = [
        record(100 + index, 50 + index, object_id=f"synthetic-object-{index}")
        for index in range(60)
    ]
    summary = summary_of([*gas, *smoke])
    assert summary.critical_alarms == 61 and len(summary.items) == 50 and summary.truncated
    first = summary.items[0]
    assert (first.row_kind, first.rule_id, first.priority) == ("chatter", "gas_detected", 0)
    assert first.collapsed_count == 3 and first.title == "Газовый датчик: «Обнаружен газ» ×3"
    assert (first.first_at, first.at) == (gas[0].event_at, gas[-1].event_at)
    assert [member.row_uid for member in first.members] == [item.row_uid for item in gas]


# The day window of the stand copy before 30.06.2026 23:53:40 MSK (rehearsal 29.09,
# bell-seed: real fire alarm journal, June 2026). Synthetic: objects and channels renamed,
# record IDs made up, the date moved to Tuesday 22.09.2026 (30.06.2026 is a Tuesday as well),
# times of day kept. The copy answered 33 rows: 29 «Обнаружен дым», one manual detector and
# three series «Похоже на ППР или ТО»; 22 rows came from four detectors repeating «Обнаружен
# дым», and none of the series was among the first 12.
COPY_SINCE = datetime(2026, 9, 21, 20, 53, 40, tzinfo=UTC)
COPY_AS_OF = datetime(2026, 9, 22, 20, 53, 40, tzinfo=UTC)
COPY_SINGLES = {
    ("synthetic-object-g", "synthetic-detector-g1", SMOKE): (
        "18:00:20 18:08:25 22:57:48 23:02:19 23:06:21 23:10:48 23:30:27"
    ),
    ("synthetic-object-m", "synthetic-detector-m1", SMOKE): "19:41:49",
    ("synthetic-object-m", "synthetic-detector-m2", SMOKE): (
        "16:30:46 16:34:18 16:40:58 16:46:17 16:56:56"
    ),
    ("synthetic-object-m", "synthetic-detector-m3", SMOKE): "16:30:20 16:36:50 16:42:40 16:46:17",
    ("synthetic-object-m", "synthetic-detector-m4", SMOKE): "16:42:46",
    ("synthetic-object-m", "synthetic-detector-m5", SMOKE): "07:49:16",
    ("synthetic-object-k", "synthetic-detector-k1", SMOKE): "13:26:24",
    ("synthetic-object-k", "synthetic-detector-k2", SMOKE): "13:15:32",
    ("synthetic-object-k", "synthetic-detector-k3", SMOKE): "13:01:20",
    ("synthetic-object-d", "synthetic-manual-d1", ("Ручной извещатель", "Не замкнут")): "11:32:14",
    ("synthetic-object-d", "synthetic-detector-d1", SMOKE): "11:29:08",
    ("synthetic-object-p", "synthetic-detector-p1", SMOKE): (
        "06:23:12 06:30:20 06:36:31 06:45:16 06:46:51 06:48:59"
    ),
}
# Series of object d, record by record: (time of day, channel, sensor type).
COPY_SERIES_D = [
    [("11:17:02", 1, SMOKE), ("11:17:36", 2, SMOKE), ("11:18:00", 3, SMOKE)]
    + [("11:19:34", 4, SMOKE), ("11:20:18", 5, SMOKE), ("11:21:08", 6, SMOKE)],
    [("11:38:22", 7, ("Ручной извещатель", "Не замкнут")), ("11:44:54", 8, MANUAL)]
    + [("11:45:22", 9, SMOKE), ("11:45:48", 10, SMOKE), ("11:47:14", 11, SMOKE)],
]


def copy_day_records() -> list[AlarmRecord]:
    """The copy's window as records: singles, two series of object d and the large series
    of object k (308 records of 226 detectors, 09:37:38–10:46:46, spread evenly)."""
    day = datetime(2026, 9, 22, tzinfo=MSK)

    def at(clock: str) -> datetime:
        hours, minutes, seconds = (int(part) for part in clock.split(":"))
        return day + timedelta(hours=hours, minutes=minutes, seconds=seconds)

    def make(object_id: str, channel: str, pair: tuple[str, str], moment: datetime):
        return AlarmRecord(
            row_uid=f"synthetic-copy-{len(records):04d}",
            channel_id=channel,
            object_id=object_id,
            sensor_type=pair[0],
            value_raw=pair[1],
            alarm=True,
            event_at=moment,
        )

    records: list[AlarmRecord] = []
    for (object_id, channel, pair), times in COPY_SINGLES.items():
        for clock_time in times.split():
            records.append(make(object_id, channel, pair, at(clock_time)))
    for series in COPY_SERIES_D:
        for clock_time, number, pair in series:
            channel = f"synthetic-detector-d-series-{number:02d}"
            records.append(make("synthetic-object-d", channel, pair, at(clock_time)))
    start, end = at("09:37:38"), at("10:46:46")
    for index in range(308):
        moment = start + (end - start) * index / 307
        channel = f"synthetic-detector-k-series-{index % 226:03d}"
        records.append(make("synthetic-object-k", channel, SMOKE, moment))
    return records


def test_copy_day_folds_33_rows_into_17_and_keeps_every_record():
    records = copy_day_records()
    rows = window_rows(alarm_rows(records), since=COPY_SINCE, data_as_of=COPY_AS_OF)
    kinds = Counter(row.kind for row in rows)
    assert kinds == {"single": 9, "chatter": 5, "test_series": 3}
    # Before the folding of repeats every repeat was its own row: 30 + 3 series = 33.
    assert sum(row.records if row.kind == "chatter" else 1 for row in rows) == 33
    assert {uid for row in rows for uid in row.member_ids} == {r.row_uid for r in records}
    series = sorted(row.channels for row in rows if row.kind == "test_series")
    assert series == [5, 6, 226]
    summary = build_summary(
        mode="received",
        since=COPY_SINCE,
        clock=clock(COPY_AS_OF),
        checked_at=COPY_AS_OF,
        cards=[],
        alarms=sorted(rows, key=policy.selection_key)[:50],
        alarms_total=len(rows),
    )
    assert summary.critical_alarms == 17 == len(summary.items) and not summary.truncated
    titles = [item.title for item in summary.items]
    assert titles[:3] == [
        "Датчик дыма: «Обнаружен дым» ×4",
        "Датчик дыма: «Обнаружен дым»",
        "Датчик дыма: «Обнаружен дым»",
    ]
    assert titles[3] == "Датчик дыма: «Обнаружен дым» ×2"
    first = summary.items[0]
    assert (first.first_at, first.at) == (
        datetime(2026, 9, 22, 20, 2, 19, tzinfo=UTC),
        datetime(2026, 9, 22, 20, 30, 27, tzinfo=UTC),
    )  # 23:02:19–23:30:27 MSK
    listed_series = [
        index for index, item in enumerate(summary.items) if item.row_kind == "test_series"
    ]
    assert listed_series == [10, 13, 14]  # every series is listed; the list is not cut at 12


# --- stays critical, window and summary ----------------------------------------------------


def test_heat_and_manual_stay_critical_as_single_rows():
    rows = alarm_rows(
        [
            record(1, 0, HEAT, alarm=True),
            record(2, 30, ("Ручной извещатель", "Не замкнут")),
            record(3, 60, MANUAL),
        ]
    )
    assert [row.rule_id for row in rows] == ["fire_manual", "fire_manual", "fire_heat"]
    assert all(row.kind == "single" for row in rows)


def test_window_rules():
    as_of = T0
    check_window(as_of - timedelta(days=7), as_of)
    with pytest.raises(NotificationWindowError, match="aware_since_required"):
        check_window(datetime(2026, 9, 24, 8, 0), as_of)
    with pytest.raises(NotificationWindowError, match="since_after_as_of"):
        check_window(as_of + timedelta(seconds=1), as_of)
    with pytest.raises(NotificationWindowError, match="since_window_too_long"):
        check_window(as_of - timedelta(days=7, seconds=1), as_of)


def test_stand_clock_follows_published_state():
    state = forecast_fixture.forecast_state()
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    replay = stand_clock("replay", state, now=now)
    assert replay.data_as_of == state.data_as_of
    assert replay.available_as_of == state.data_as_of
    assert replay.as_of == max(state.data_as_of, state.published_at)
    received = stand_clock("received", state, now=now)
    assert received.available_as_of == now  # uploads carry their real import time
    unpublished = ForecastState(mode="replay", checked_at=now, generation=0)
    with pytest.raises(StandClockUnavailable):
        stand_clock("replay", unpublished, now=now)
    with pytest.raises(StandClockUnavailable):
        stand_clock("replay", None, now=now)


def test_new_cards_are_scored_and_published_inside_the_window():
    cards = [entry.card for entry in forecast_fixture.journal_entries()]
    state = forecast_fixture.forecast_state()
    since = state.published_at - timedelta(minutes=10)
    found = new_cards([*cards, *cards], since=since, as_of=state.published_at)
    assert found and len({card.id for card in found}) == len(found)
    assert all(card.status == "scored" for card in found)
    assert all(since < card.published_at <= state.published_at for card in found)
    abstained = [card for card in cards if card.status == "abstained"]
    assert abstained and not new_cards(abstained, since=since, as_of=state.published_at)


def test_cards_and_alarm_rows_have_separate_counters():
    state = forecast_fixture.forecast_state()
    as_of = max(state.data_as_of, state.published_at)
    since = as_of - timedelta(days=1)
    cards = new_cards(
        [entry.card for entry in forecast_fixture.journal_entries()], since=since, as_of=as_of
    )
    gas = record(1, 0, GAS, start=state.data_as_of - timedelta(hours=1))
    rows = alarm_rows([gas])
    summary = build_summary(
        mode="replay",
        since=since,
        clock=StandClock(state.data_as_of, state.data_as_of, as_of),
        checked_at=as_of,
        cards=cards,
        object_names={cards[0].object_id: "Синтетический объект"},
        alarms=rows,
        alarms_total=1,
    )
    assert summary.new_cards == len(cards) and summary.critical_alarms == 1
    assert {item.kind for item in summary.items} == {"new_card", "critical_alarm"}
    card = next(item for item in summary.items if item.ref_id == cards[0].id)
    assert card.object_name == "Синтетический объект" and card.rule_id is None
    assert summary.policy_version == "critical-alarms-v1" and not summary.policy_confirmed
    assert summary.maintenance_check == "not_applied" and not summary.truncated


def test_rows_after_the_data_watermark_are_never_listed():
    data_as_of = T0 + timedelta(minutes=30)
    past, future = record(1, 10), record(2, 31)
    summary = summary_of([past, future], since=T0, data_as_of=data_as_of)
    assert [item.ref_id for item in summary.items] == [past.row_uid]
    assert summary.critical_alarms == 1
    assert all(item.at <= summary.as_of for item in summary.items)


def test_every_row_title_passes_card_wording():
    records = [
        record(number, number, pair, object_id=f"synthetic-object-{number}")
        for number, pair in enumerate(policy.CRITICAL_PAIRS)
    ]
    records += [
        record(100 + index, index, GAS, object_id="synthetic-object-series") for index in range(5)
    ]
    for row in alarm_rows(records):
        check_card_wording(row.title)
        assert len(row.title) <= 200 and len(row.rule_text) <= 200
        item = policy.alarm_item(row)
        assert item.kind == "critical_alarm" and item.target_spec_id is None


def test_members_carry_channel_names():
    """QA 29.09, D3: a member names its channel when the layout knows it, else only the ID."""
    named = [
        replace(record(index, index, channel="synthetic-channel-7"), channel_name="Синт. дым 7")
        for index in range(1, 3)
    ]
    unnamed = record(3, 90, channel="synthetic-channel-8")
    items = [policy.alarm_item(row) for row in alarm_rows([*named, unnamed])]
    members = {member.row_uid: member for item in items for member in item.members}
    assert members[named[0].row_uid].channel_name == "Синт. дым 7"
    assert members[named[1].row_uid].channel_name == "Синт. дым 7"
    assert members[unnamed.row_uid].channel_name is None
    assert members[unnamed.row_uid].channel_id == "synthetic-channel-8"


def test_summary_rejects_a_long_window():
    with pytest.raises(NotificationWindowError):
        build_summary(
            mode="replay",
            since=T0 - timedelta(days=8),
            clock=clock(T0),
            checked_at=T0,
            cards=[],
            alarms=[],
            alarms_total=0,
        )


# --- HTTP route --------------------------------------------------------------------------

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
FIXTURE_SINCE = {"since": "2026-09-24T00:00:00+03:00"}


def client_as(mode: str, *roles: str, auth_source: str = "ldap", **settings) -> TestClient:
    app = create_app(Settings(mode=mode, _env_file=None, **settings))
    if roles:
        actor = Me(
            subject_id="synthetic-actor",
            display_name="Синтетический пользователь",
            roles=list(roles),
            auth_source=auth_source,
            session_expires_at=NOW + timedelta(hours=1) if auth_source == "ldap" else None,
        )
        app.dependency_overrides[current_actor] = lambda: actor
    return TestClient(app)


def published(monkeypatch, state=None) -> None:
    """The fixture forecast state and journal stand in for B2's ``forecast_db``."""
    monkeypatch.setattr(
        forecast_db, "forecast_state", lambda settings: state or forecast_fixture.forecast_state()
    )
    monkeypatch.setattr(
        forecast_db,
        "forecast_journal",
        lambda settings, *, as_of, **filters: forecast_fixture.forecast_journal(**filters),
    )


def test_route_permission_is_read():
    allowed = client_as("fixture", "dispatcher").get("/api/v1/notifications", params=FIXTURE_SINCE)
    assert allowed.status_code == 200
    denied = client_as("fixture", "integration", auth_source="token").get(
        "/api/v1/notifications", params=FIXTURE_SINCE
    )
    assert (denied.status_code, denied.json()["detail"]) == (403, "forbidden_role")


def test_route_rejects_naive_since_and_answers_503_without_storage():
    # B2 is merged: without a DSN the forecast read is unavailable, an unreachable
    # database is a storage outage.
    client = client_as("replay", replay_snapshot_id="synthetic", db_dsn="postgresql://x/y")
    naive = client.get("/api/v1/notifications", params={"since": "2026-09-24T00:00:00"})
    assert (naive.status_code, naive.json()["detail"]) == (422, "aware_since_required")
    down = client.get("/api/v1/notifications", params=FIXTURE_SINCE)
    assert (down.status_code, down.json()["detail"]) == (503, "notifications_storage_unavailable")
    no_dsn = client_as("replay", replay_snapshot_id="synthetic")
    stub = no_dsn.get("/api/v1/notifications", params=FIXTURE_SINCE)
    assert (stub.status_code, stub.json()["detail"]) == (503, "forecast_not_implemented")


def test_route_window_scope_and_publication(monkeypatch):
    published(monkeypatch)
    unconfigured = client_as("replay").get("/api/v1/notifications", params=FIXTURE_SINCE)
    assert (unconfigured.status_code, unconfigured.json()["detail"]) == (
        503,
        "observations_not_configured",
    )
    old = client_as("replay").get(
        "/api/v1/notifications", params={"since": "2026-09-01T00:00:00+03:00"}
    )
    assert (old.status_code, old.json()["detail"]) == (422, "since_window_too_long")
    late = client_as("replay").get(
        "/api/v1/notifications", params={"since": "2026-09-27T00:00:00+03:00"}
    )
    assert (late.status_code, late.json()["detail"]) == (422, "since_after_as_of")
    published(monkeypatch, ForecastState(mode="replay", checked_at=NOW, generation=0))
    empty = client_as("replay").get("/api/v1/notifications", params=FIXTURE_SINCE)
    assert (empty.status_code, empty.json()["detail"]) == (503, "forecast_not_published")
