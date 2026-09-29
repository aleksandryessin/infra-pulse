"""In-app notifications (N1, ТЗ §10, UI-03, OUT-03): versioned policy and summary.

The bell counts two things after ``since`` and never mixes them:

- ``new_card`` — scored forecast cards published after ``since`` (B2 publications);
- ``critical_alarm`` — rows of current source records that by their text may mean an
  emergency in the customer's sense, a threat to people's lives (customer answer of
  28.09: fire, flooding, gas, intrusion, abnormal temperature). Policy v1 selects fire, gas
  and flooding only (``NOTIFICATION_POLICY``, ``critical-alarms-v1``); intrusion and
  temperature are not in it yet, and the caption says so. The source ``alarm`` flag is not
  required. Every message is checked by the dispatcher and the duty engineer: a row is not a
  confirmed emergency.

Policy v1 (technologist and the internal acceptance review). Load on 2023–2024 (729 days, counted by
this module's code — ``CRITICAL_PAIRS``, ``with_power_context``, ``alarm_rows``; data analyst,
29.09): candidate records p90 367 a day under v0; rows under v1 — median 4 a day, p90 32,
p95 61, mean 13.7 (before the folding of repeats, which only lowers them). Nothing is
hidden: a folded row states how many records and channels it holds and keeps their IDs to
expand into the list (``AlarmRow.member_ids``).

- (а) Pump «Затоплен» is critical only when the object has no «Обесточен» or
  «Неисправен» within ±10 min; otherwise it is the power-loss signature (99%) and is not
  a critical record.
- (б) Smoke, heat and manual detectors: ≥ 5 channels of one object within 10 min on a
  weekday 07:00–19:00 MSK fold into one row «Похоже на ППР или ТО: N извещателей — сверить
  с графиком» (customer answer of 28.09: planned works are «ППР» and «ТО»; the service has
  no schedule of works). Night and weekend series are not marked: each record stays its own
  row.
- (в) Gas: ≥ 5 channels of one object within 10 min fold into «Похоже на ППР или ТО:
  N газоанализаторов — сверить с графиком». A single «Обнаружен газ» (any ``alarm``) is
  selected first, so it is never cut from the 50 listed items.
- (г) Flood sensor «Не замкнут»: chatter folds into one row per channel and clock hour
  (MSK) with the number of records.
- Repeats (29.09, the rehearsal on June 2026 data): outside a series, records of one channel
  with the same text within a clock hour (MSK) fold into one ``chatter`` row «Датчик дыма:
  «Обнаружен дым» ×5» (the UI adds «с ЧЧ:ММ по ЧЧ:ММ» from ``first_at``/``at``) for every
  rule, as (г) does for flood sensors; a lone record stays ``single``. It
  is display only: the selection does not change and the row keeps every record. On
  30.06.2026 it gives 17 rows instead of 33 (22 of them came from four smoke detectors
  repeating «Обнаружен дым»). Gas outside a series keeps the first priority.
- Heat «Не замкнут», manual «Не замкнут» and «Рычаг сдернут» stay critical.
  Not selected by v1: doors, hatches, «Неисправен», temperature. Intrusion and abnormal
  temperature are emergencies for the customer (28.09); rules for them are the next step
  (``critical-alarms-v2``), and until then the caption names the gap.

``critical_alarms`` counts rows. Whether a record lies inside a maintenance session is not
checked: ``maintenance_check = "not_applied"`` until the maintenance mask runs in runtime
(B4). ``policy_confirmed = false`` until the customer confirms the list. No sound, external
channel or «critical» styling is carried in the data.

Everything here is pure; ``storage/notifications_pg.py`` reads the records.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from infra_pulse_backend.ingestion.journal_csv import MSK
from infra_pulse_core.contracts.forecast import ForecastCard, ForecastMode, ForecastState
from infra_pulse_core.contracts.notifications import (
    MAX_NOTIFICATION_ITEMS,
    MAX_NOTIFICATION_WINDOW,
    MAX_ROW_MEMBERS,
    PRIORITY_DEFAULT,
    PRIORITY_FIRST,
    NotificationItem,
    NotificationMember,
    NotificationSummary,
)

# Customer answer of 28.09: an emergency («авария») is a threat to people's lives; loss of
# communication or power is an incident. The caption names the gap of v1 honestly.
NOTIFICATION_CAPTION = (
    "Колокольчик — сообщения источника, которые по тексту могут означать аварию, то есть "
    "угрозу жизни людей (классификация заказчика, 28.09). Сейчас в списке пожар, газ и "
    "затопление; охранные сработки и аномальная температура пока не входят. Каждое "
    "сообщение проверяют диспетчер и дежурный инженер: это не подтверждённая авария. Серии, "
    "похожие на ППР или ТО, и повторы одного датчика за час показываются одной строкой и не "
    "скрываются. Правило отбора с заказчиком пока не согласовано."
)

# Versioned policy «JSON в коде». Texts are exact journal lexemes by sensor type
# (data-science/experiments/sensor-failure/TECHNOLOGIST_REVIEW.md, appendix): the state
# reference of the customer is partial and contradicts the journal.
NOTIFICATION_POLICY: dict = {
    "policy_version": "critical-alarms-v1",
    "policy_confirmed": False,
    "maintenance_check": "not_applied",
    "caption": NOTIFICATION_CAPTION,
    "matching": "exact sensor_type and verbatim value_raw; the source alarm flag is ignored",
    "counts": "rows: a folded series or the repeats of one channel within an hour are one row",
    "rules": [
        {
            "rule_id": "fire_smoke",
            "family": "fire",
            "sensor_types": ["Датчик дыма"],
            "values": ["Обнаружен дым"],
            "text": "дымовой извещатель: «Обнаружен дым», независимо от отметки «тревожное»",
        },
        {
            "rule_id": "fire_heat",
            "family": "fire",
            "sensor_types": ["Тепловой датчик"],
            "values": ["Не замкнут"],
            "text": "тепловой извещатель: «Не замкнут», независимо от отметки «тревожное»",
        },
        {
            "rule_id": "fire_manual",
            "family": "fire",
            "sensor_types": ["Ручной извещатель"],
            "values": ["Не замкнут", "Рычаг сдернут"],
            "text": (
                "ручной извещатель: «Не замкнут» или «Рычаг сдернут», "
                "независимо от отметки «тревожное»"
            ),
        },
        {
            "rule_id": "gas_detected",
            "family": "gas",
            "sensor_types": ["Газовый датчик"],
            "values": ["Обнаружен газ"],
            "text": (
                "газовый датчик: «Обнаружен газ», независимо от отметки «тревожное», "
                "вне серии проверки"
            ),
        },
        {
            "rule_id": "flood_pump",
            "family": "pump",
            "sensor_types": ["Состояние насоса"],
            "values": ["Затоплен"],
            "text": "насос: «Затоплен», на объекте в ±10 мин нет «Обесточен» и «Неисправен»",
        },
        {
            "rule_id": "flood_sensor",
            "family": "flood_sensor",
            "sensor_types": ["Датчик затопления"],
            "values": ["Не замкнут"],
            "text": "датчик затопления: «Не замкнут»; одна строка на канал в час",
        },
    ],
    "pump_power_context": {
        "values": ["Обесточен", "Неисправен"],
        "scope": "object; the channel when the object is unknown",
        "window_minutes": 10,
    },
    "series": {
        "fire_test_series": {
            "families": ["fire"],
            "min_channels": 5,
            "window_minutes": 10,
            "weekdays": [0, 1, 2, 3, 4],
            "local_hours": [7, 19],
            "label": "Похоже на ППР или ТО — сверить с графиком",
        },
        "gas_calibration_series": {
            "families": ["gas"],
            "min_channels": 5,
            "window_minutes": 10,
            "label": "Похоже на ППР или ТО — сверить с графиком",
        },
    },
    # Display, not selection (29.09): repeats of one channel fold for every rule, as (г).
    "debounce": {
        "repeats": (
            "outside a series, records of one channel with the same text within a clock hour "
            "(MSK) are one row «…×N» from first_at to at; the row keeps every record"
        ),
    },
    "order": "gas outside a series is selected first; items are listed newest first",
    "not_critical": [
        "КД Дверь",
        "КД Люк",
        "Неисправен",
        "Датчик температуры",
        "«Затоплен» насоса рядом с «Обесточен» или «Неисправен»",
    ],
}

POLICY_VERSION: str = NOTIFICATION_POLICY["policy_version"]
POLICY_CONFIRMED: bool = NOTIFICATION_POLICY["policy_confirmed"]
MAINTENANCE_CHECK: str = NOTIFICATION_POLICY["maintenance_check"]
_FIRE_SERIES = NOTIFICATION_POLICY["series"]["fire_test_series"]
TEST_SERIES_MIN_CHANNELS: int = _FIRE_SERIES["min_channels"]
TEST_SERIES_WINDOW = timedelta(minutes=_FIRE_SERIES["window_minutes"])
FIRE_SERIES_WEEKDAYS = frozenset(_FIRE_SERIES["weekdays"])
FIRE_SERIES_HOURS: tuple[int, int] = (
    _FIRE_SERIES["local_hours"][0],
    _FIRE_SERIES["local_hours"][1],
)
POWER_TEXTS: tuple[str, ...] = tuple(NOTIFICATION_POLICY["pump_power_context"]["values"])
POWER_WINDOW = timedelta(minutes=NOTIFICATION_POLICY["pump_power_context"]["window_minutes"])
# The storage reads candidates this long before ``since``: a series needs 10 minutes of
# neighbours, an hour of repeats of one channel starts at most an hour earlier.
LOOKBACK = timedelta(hours=1)

Family = Literal["fire", "gas", "pump", "flood_sensor"]
RowKind = Literal["single", "test_series", "calibration_series", "chatter"]


@dataclass(frozen=True)
class CriticalRule:
    rule_id: str
    family: Family
    sensor_types: tuple[str, ...]
    values: tuple[str, ...]
    text: str


CRITICAL_RULES: tuple[CriticalRule, ...] = tuple(
    CriticalRule(
        rule_id=rule["rule_id"],
        family=rule["family"],
        sensor_types=tuple(rule["sensor_types"]),
        values=tuple(rule["values"]),
        text=rule["text"],
    )
    for rule in NOTIFICATION_POLICY["rules"]
)
_RULE_BY_PAIR: dict[tuple[str, str], CriticalRule] = {
    (sensor_type, value): rule
    for rule in CRITICAL_RULES
    for sensor_type in rule.sensor_types
    for value in rule.values
}
# (sensor_type, value_raw) pairs read by the storage query, in a stable order.
CRITICAL_PAIRS: tuple[tuple[str, str], ...] = tuple(_RULE_BY_PAIR)
PUMP_PAIRS: tuple[tuple[str, str], ...] = tuple(
    pair for pair, rule in _RULE_BY_PAIR.items() if rule.family == "pump"
)


def match_rule(sensor_type: str | None, value_raw: str) -> CriticalRule | None:
    """The rule of an exact (sensor type, verbatim text) pair, else None."""
    if sensor_type is None:
        return None
    return _RULE_BY_PAIR.get((sensor_type, value_raw))


@dataclass(frozen=True)
class AlarmRecord:
    """A candidate source record (``storage/notifications_pg.py``).

    ``power_nearby`` — for a pump «Затоплен»: «Обесточен» or «Неисправен» on the same
    object (the same channel without an object) within ±10 min, not later than the data
    watermark. ``channel_name`` — from the channel layout, set only for listed rows.
    """

    row_uid: str
    channel_id: str
    object_id: str | None
    sensor_type: str
    value_raw: str
    alarm: bool
    event_at: datetime
    object_name: str | None = None
    power_nearby: bool = False
    channel_name: str | None = None


@dataclass(frozen=True)
class PowerEvent:
    """«Обесточен» or «Неисправен» of any sensor type near a pump «Затоплен» (rule (а))."""

    object_id: str | None
    channel_id: str
    event_at: datetime


def with_power_context(
    records: Iterable[AlarmRecord], power_events: Iterable[PowerEvent]
) -> list[AlarmRecord]:
    """Set ``power_nearby`` of every pump «Затоплен»: a power event of the same object
    (of the same channel when the object is unknown) within ±``POWER_WINDOW``."""
    by_object: dict[str, list[datetime]] = {}
    by_channel: dict[str, list[datetime]] = {}
    for event in power_events:
        if event.object_id is not None:
            by_object.setdefault(event.object_id, []).append(event.event_at)
        by_channel.setdefault(event.channel_id, []).append(event.event_at)
    for times in (*by_object.values(), *by_channel.values()):
        times.sort()
    marked = []
    for record in records:
        rule = match_rule(record.sensor_type, record.value_raw)
        if rule is not None and rule.family == "pump":
            times = (
                by_object.get(record.object_id, [])
                if record.object_id is not None
                else by_channel.get(record.channel_id, [])
            )
            index = bisect_left(times, record.event_at - POWER_WINDOW)
            nearby = index < len(times) and times[index] <= record.event_at + POWER_WINDOW
            record = replace(record, power_nearby=nearby)
        marked.append(record)
    return marked


@dataclass(frozen=True)
class AlarmRow:
    """One line of the bell: a single record, a folded series or the repeats of one channel
    within a clock hour (``chatter``).

    ``ref_id``/``at`` are the newest record's; ``member_ids`` are every record of the row,
    oldest first; ``members`` keep those records so the item carries the newest 50 of them
    (contract ``NotificationItem.members``, C0.5). ``gas_single`` — «Обнаружен газ» outside
    a calibration series (one record or repeats of one channel): the first priority.
    """

    kind: RowKind
    rule_id: str
    rule_text: str
    title: str
    ref_id: str
    at: datetime
    first_at: datetime
    object_id: str | None
    object_name: str | None
    records: int
    channels: int
    member_ids: tuple[str, ...]
    gas_single: bool = False
    members: tuple[AlarmRecord, ...] = ()


class NotificationWindowError(ValueError):
    """Bad ``since`` for the stand clock (HTTP 422 with the message as detail)."""


@dataclass(frozen=True)
class StandClock:
    """Where the notification window ends.

    ``data_as_of`` bounds event time of source records (nothing later is shown);
    ``available_as_of`` bounds their arrival: the simulated availability in replay,
    the wall clock in received (uploads carry the real import time). ``as_of`` of the
    summary is on the data timeline: the data watermark or the latest card publication
    (``settle_on_cards``); ``stand_clock`` gives only its upper bound for the journal read.
    """

    data_as_of: datetime
    available_as_of: datetime
    as_of: datetime


class StandClockUnavailable(LookupError):
    """No published forecast state gives the stand's data watermark (HTTP 503)."""


def stand_clock(mode: ForecastMode, state: ForecastState | None, *, now: datetime) -> StandClock:
    """Stand clock from the published forecast state (B2) and the wall clock."""
    if state is None or state.data_as_of is None:
        raise StandClockUnavailable("forecast_not_published")
    data_as_of = state.data_as_of
    as_of = max(data_as_of, state.published_at or data_as_of)
    available_as_of = now if mode == "received" else data_as_of
    return StandClock(data_as_of=data_as_of, available_as_of=available_as_of, as_of=as_of)


def settle_on_cards(clock: StandClock, cards: Iterable[ForecastCard]) -> StandClock:
    """End the window at the data watermark or at the latest card publication.

    ``ForecastState.published_at`` is when a run was published on the wall clock (B2); on
    a historical or late scope it lies far after the data and cannot end a data-time
    window (``NotificationSummary.as_of`` is the data watermark). B2 publishes a card at
    its cutoff, never after ``data_as_of``; the fixture publishes cards minutes after the
    watermark. Both are covered by taking the cards' own publication times.
    """
    latest = max(
        (card.published_at for card in cards if card.published_at <= clock.as_of), default=None
    )
    as_of = clock.data_as_of if latest is None else max(clock.data_as_of, latest)
    return replace(clock, as_of=as_of)


def check_window(since: datetime, as_of: datetime) -> None:
    """``since`` is aware, not after ``as_of`` and at most 7 days before it."""
    if since.tzinfo is None or since.utcoffset() is None:
        raise NotificationWindowError("aware_since_required")
    if since > as_of:
        raise NotificationWindowError("since_after_as_of")
    if as_of - since > MAX_NOTIFICATION_WINDOW:
        raise NotificationWindowError("since_window_too_long")


# --- rows -------------------------------------------------------------------------------


def plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def working_hours(moment: datetime) -> bool:
    """Weekday 07:00–19:00 MSK (no holiday calendar)."""
    local = moment.astimezone(MSK)
    start, end = FIRE_SERIES_HOURS
    return local.weekday() in FIRE_SERIES_WEEKDAYS and start <= local.hour < end


def series_members(records: Iterable[AlarmRecord]) -> set[str]:
    """Row UIDs inside a window of ≥ ``TEST_SERIES_MIN_CHANNELS`` channels of one object
    within ``TEST_SERIES_WINDOW`` (inclusive span). Records without an object never form a
    series."""
    by_object: dict[str, list[AlarmRecord]] = {}
    for record in records:
        if record.object_id is not None:
            by_object.setdefault(record.object_id, []).append(record)
    marked: set[str] = set()
    for rows in by_object.values():
        rows.sort(key=lambda record: (record.event_at, record.row_uid))
        channels: Counter[str] = Counter()
        left = 0
        marked_up_to = -1
        for right, record in enumerate(rows):
            channels[record.channel_id] += 1
            while record.event_at - rows[left].event_at > TEST_SERIES_WINDOW:
                channels[rows[left].channel_id] -= 1
                if channels[rows[left].channel_id] == 0:
                    del channels[rows[left].channel_id]
                left += 1
            if len(channels) >= TEST_SERIES_MIN_CHANNELS:
                for index in range(max(left, marked_up_to + 1), right + 1):
                    marked.add(rows[index].row_uid)
                marked_up_to = right
    return marked


def _clusters(records: list[AlarmRecord]) -> list[list[AlarmRecord]]:
    """Marked records of one object chained while the gap is ≤ ``TEST_SERIES_WINDOW``."""
    clusters: list[list[AlarmRecord]] = []
    for record in sorted(records, key=lambda item: (item.event_at, item.row_uid)):
        if clusters and record.event_at - clusters[-1][-1].event_at <= TEST_SERIES_WINDOW:
            clusters[-1].append(record)
        else:
            clusters.append([record])
    return clusters


def _hm(moment: datetime) -> str:
    return moment.astimezone(MSK).strftime("%H:%M")


def _single(record: AlarmRecord, rule: CriticalRule) -> AlarmRow:
    return AlarmRow(
        kind="single",
        rule_id=rule.rule_id,
        rule_text=rule.text,
        title=f"{record.sensor_type}: «{record.value_raw}»",
        ref_id=record.row_uid,
        at=record.event_at,
        first_at=record.event_at,
        object_id=record.object_id,
        object_name=record.object_name,
        records=1,
        channels=1,
        member_ids=(record.row_uid,),
        gas_single=rule.family == "gas",
        members=(record,),
    )


def _series(members: list[AlarmRecord], kind: RowKind) -> AlarmRow:
    first, last = members[0], members[-1]
    channels = len({record.channel_id for record in members})
    records = f"{len(members)} {plural(len(members), 'запись', 'записи', 'записей')}"
    span = f"{_hm(first.event_at)}–{_hm(last.event_at)} МСК"
    if kind == "test_series":
        rule_id = "fire_test_series"
        title = (
            f"Похоже на ППР или ТО: {channels} "
            f"{plural(channels, 'извещатель', 'извещателя', 'извещателей')} — сверить с графиком"
        )
        text = (
            f"серия дыма, тепловых и ручных извещателей объекта: {records}, {span}, "
            "будни 07–19; раскрывается в список записей"
        )
    else:
        rule_id = "gas_calibration_series"
        title = (
            f"Похоже на ППР или ТО: {channels} "
            f"{plural(channels, 'газоанализатор', 'газоанализатора', 'газоанализаторов')}"
            " — сверить с графиком"
        )
        text = f"серия «Обнаружен газ» объекта: {records}, {span}; раскрывается в список записей"
    return AlarmRow(
        kind=kind,
        rule_id=rule_id,
        rule_text=text,
        title=title,
        ref_id=last.row_uid,
        at=last.event_at,
        first_at=first.event_at,
        object_id=last.object_id,
        object_name=next((record.object_name for record in members if record.object_name), None),
        records=len(members),
        channels=channels,
        member_ids=tuple(record.row_uid for record in members),
        members=tuple(members),
    )


def _chatter(members: list[AlarmRecord], rule: CriticalRule) -> AlarmRow:
    """Two or more records of one channel with one text within a clock hour, oldest first:
    «Датчик дыма: «Обнаружен дым» ×5»; the span is ``first_at``–``at`` and in ``rule_text``
    (a short title keeps the object visible in the bell line)."""
    first, last = members[0], members[-1]
    count = len(members)
    start, end = _hm(first.event_at), _hm(last.event_at)
    records = f"{count} {plural(count, 'запись', 'записи', 'записей')}"
    return AlarmRow(
        kind="chatter",
        rule_id=rule.rule_id,
        rule_text=f"{rule.text}; {records} одного канала за час, {start}–{end} МСК, "
        "раскрывается в список",
        title=f"{last.sensor_type}: «{last.value_raw}» ×{count}",
        ref_id=last.row_uid,
        at=last.event_at,
        first_at=first.event_at,
        object_id=last.object_id,
        object_name=last.object_name,
        records=count,
        channels=1,
        member_ids=tuple(record.row_uid for record in members),
        gas_single=rule.family == "gas",
        members=tuple(members),
    )


def repeat_key(record: AlarmRecord) -> tuple[str, str, datetime]:
    """Records outside a series with one key fold into one row: (channel, verbatim text,
    clock hour MSK) — rule (г) for every rule since 29.09."""
    hour = record.event_at.astimezone(MSK).replace(minute=0, second=0, microsecond=0)
    return record.channel_id, record.value_raw, hour


def _repeat_rows(records: Iterable[AlarmRecord], rules: dict[str, CriticalRule]) -> list[AlarmRow]:
    """A lone record is ``single``; repeats of one channel within a clock hour — ``chatter``."""
    groups: dict[tuple[str, str, datetime], list[AlarmRecord]] = {}
    for record in records:
        groups.setdefault(repeat_key(record), []).append(record)
    rows = []
    for members in groups.values():
        members.sort(key=lambda record: (record.event_at, record.row_uid))
        rule = rules[members[0].row_uid]
        rows.append(_single(members[0], rule) if len(members) == 1 else _chatter(members, rule))
    return rows


def alarm_rows(records: Iterable[AlarmRecord]) -> list[AlarmRow]:
    """Rows of policy v1 from candidate records (rules (а)–(г), repeats), newest first."""
    fire: list[AlarmRecord] = []
    gas: list[AlarmRecord] = []
    loose: list[AlarmRecord] = []  # outside any series: a single record or repeats
    rows: list[AlarmRow] = []
    rules: dict[str, CriticalRule] = {}
    for record in records:
        rule = match_rule(record.sensor_type, record.value_raw)
        if rule is None:
            continue
        rules[record.row_uid] = rule
        if rule.family == "fire":
            fire.append(record)
        elif rule.family == "gas":
            gas.append(record)
        elif rule.family != "pump" or not record.power_nearby:
            loose.append(record)  # (а) a pump next to power loss is its signature, not listed
    # (б) only weekday working-hour detector records can form a test series; (в) gas
    folds = (
        (fire, series_members(r for r in fire if working_hours(r.event_at)), "test_series"),
        (gas, series_members(gas), "calibration_series"),
    )
    for candidates, marked, kind in folds:
        by_object: dict[str, list[AlarmRecord]] = {}
        for record in candidates:
            if record.row_uid in marked:
                by_object.setdefault(record.object_id or "", []).append(record)
            else:
                loose.append(record)
        for members in by_object.values():
            rows.extend(_series(cluster, kind) for cluster in _clusters(members))
    rows.extend(_repeat_rows(loose, rules))
    return sorted(rows, key=lambda row: (row.at, row.ref_id), reverse=True)


def window_rows(
    rows: Iterable[AlarmRow], *, since: datetime, data_as_of: datetime
) -> list[AlarmRow]:
    """Rows whose newest record lies in ``(since, data_as_of]``."""
    return [row for row in rows if since < row.at <= data_as_of]


def selection_key(row: AlarmRow) -> tuple:
    """Gas outside a series first (rule (в)), then newest first."""
    return (not row.gas_single, -row.at.timestamp(), row.ref_id)


def alarm_item(row: AlarmRow) -> NotificationItem:
    """A bell row with its kind, counts and the newest 50 records (C0.5); «Обнаружен газ»
    outside a calibration series gets the first priority (rule (в))."""
    newest = row.members[-MAX_ROW_MEMBERS:]
    return NotificationItem(
        kind="critical_alarm",
        ref_id=row.ref_id,
        object_id=row.object_id,
        object_name=row.object_name,
        title=row.title,
        at=row.at,
        rule_id=row.rule_id,
        rule_text=row.rule_text,
        priority=PRIORITY_FIRST if row.gas_single else PRIORITY_DEFAULT,
        row_kind=row.kind,
        collapsed_count=row.records,
        channels_count=row.channels,
        first_at=row.first_at,
        members=[
            NotificationMember(
                row_uid=record.row_uid,
                channel_id=record.channel_id,
                channel_name=record.channel_name,
                event_at=record.event_at,
                value_raw=record.value_raw,
            )
            for record in newest
        ],
    )


def card_item(card: ForecastCard, object_name: str | None = None) -> NotificationItem:
    return NotificationItem(
        kind="new_card",
        ref_id=card.id,
        object_id=card.object_id,
        object_name=object_name,
        title=f"Новая карточка: {card.target_label}",
        at=card.published_at,
        target_spec_id=card.target_spec_id,
    )


def new_cards(
    cards: Iterable[ForecastCard], *, since: datetime, as_of: datetime
) -> list[ForecastCard]:
    """Scored cards published in ``(since, as_of]``, newest first, one per card ID.

    Abstained cards are «прогноз не выдан» and are not announced.
    """
    unique = {
        card.id: card
        for card in cards
        if card.status == "scored" and since < card.published_at <= as_of
    }
    return sorted(unique.values(), key=lambda card: (card.published_at, card.id), reverse=True)


def build_summary(
    *,
    mode: ForecastMode,
    since: datetime,
    clock: StandClock,
    checked_at: datetime,
    cards: Sequence[ForecastCard],
    object_names: dict[str, str] | None = None,
    alarms: Sequence[AlarmRow],
    alarms_total: int,
    test_rows: Iterable[str] = (),
) -> NotificationSummary:
    """Up to 50 items with exact counters; ``critical_alarms`` counts rows.

    ``cards`` are every new card of the window; ``alarms`` are rows of the window chosen by
    ``selection_key`` (at least ``min(alarms_total, 50)`` of them) and ``alarms_total`` the
    exact number of rows. Selection keeps gas outside a series first, then the newest items;
    the listed items are ordered by priority (gas outside a series first), then newest first,
    as the contract requires (ties: cards first). ``test_rows`` is unused since v1: series
    are folded into rows.
    """
    check_window(since, clock.as_of)
    names = object_names or {}
    in_window = window_rows(alarms, since=since, data_as_of=clock.data_as_of)
    if len(in_window) > alarms_total:
        raise ValueError("alarm rows exceed their exact count")
    ranked: list[tuple[tuple, NotificationItem]] = [
        (
            (True, -card.published_at.timestamp(), card.id),
            card_item(card, names.get(card.object_id or "")),
        )
        for card in cards
    ]
    ranked += [(selection_key(row), alarm_item(row)) for row in in_window]
    items = [item for _, item in sorted(ranked, key=lambda pair: pair[0])]
    items = items[:MAX_NOTIFICATION_ITEMS]
    items.sort(
        key=lambda item: (
            item.priority,
            -item.at.timestamp(),
            item.kind != "new_card",
            item.ref_id,
        )
    )
    return NotificationSummary(
        mode=mode,
        since=since,
        as_of=clock.as_of,
        checked_at=checked_at,
        new_cards=len(cards),
        critical_alarms=alarms_total,
        items=items,
        truncated=len(items) < len(cards) + alarms_total,
        policy_version=POLICY_VERSION,
        policy_confirmed=POLICY_CONFIRMED,
        maintenance_check=MAINTENANCE_CHECK,
        policy_caption=NOTIFICATION_CAPTION,
    )
