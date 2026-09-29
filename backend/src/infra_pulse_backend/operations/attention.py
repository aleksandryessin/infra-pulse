"""Provisional, deterministic order for reviewing already available messages."""

from datetime import datetime
from typing import Literal

from infra_pulse_core.contracts.attention import (
    AttentionEntry,
    AttentionList,
    ObservedMessage,
    SourceAlarmObject,
    SourceAlarmWindow,
)

POLICY_VERSION = "provisional-exact-text-received-v2"

# Descriptive candidates from the accepted 2025 catalog, not a customer-approved
# response policy. Alarm=true is retained regardless of these pairs.
WATCH_PAIRS = (
    ("Газовый датчик", "Обнаружен газ"),
    ("Газовый датчик", "Неисправен"),
    ("Датчик дыма", "Обнаружен дым"),
    ("Датчик дыма", "Неисправен"),
    ("Состояние насоса", "Неисправен"),
    ("Состояние насоса", "Обесточен"),
    ("Состояние насоса", "Затоплен"),
    ("Состояние вентилятора", "Неисправен"),
    ("Состояние вентилятора", "Обесточен"),
    ("Состояние фазы", "Обесточен"),
    ("ИБП", "Питание от батарей"),
    ("ИБП", "Батарея разряжена"),
    ("ИБП", "Батарея неисправна"),
    ("Датчик температуры", "Температура ниже 3ºC"),
    ("Датчик температуры", "Температура выше 40ºC"),
    ("Состояние охраны", "Много неисправных устройств"),
)
WATCH_PAIR_SET = frozenset(WATCH_PAIRS)


def is_watch_text(message: ObservedMessage) -> bool:
    return (message.sensor_type, message.value_raw) in WATCH_PAIR_SET


def explain_attention(
    *, alarm: bool, sensor_type: str | None, value_raw: str
) -> tuple[str, list[str]]:
    if alarm:
        return "source_alarm", ["source_alarm_true", "received_order"]
    if (sensor_type, value_raw) in WATCH_PAIR_SET:
        return "watch_text", ["exact_text_candidate", "received_order"]
    return "chronological", ["received_order"]


def attention_band(message: ObservedMessage) -> str:
    return explain_attention(
        alarm=message.alarm, sensor_type=message.sensor_type, value_raw=message.value_raw
    )[0]


def order_for_review(
    messages: list[ObservedMessage],
    *,
    as_of: datetime,
    mode: Literal["fixture", "replay", "received", "live"],
    view: Literal["attention", "all"] = "attention",
    offset: int = 0,
    limit: int | None = None,
) -> AttentionList:
    """Show source alarms first, then arrival order; preserve every source row.

    This is a provisional navigation order pending D01, not a severity score.
    Unknown historical delivery times must be assigned an explicit simulated
    availability by a replay loader before calling this function.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    available = [message for message in messages if message.available_at <= as_of]
    all_records_total = len(available)
    seen = set()
    for message in available:
        identity = (message.source_namespace, message.snapshot_id, message.row_uid)
        if identity in seen:
            raise ValueError(f"duplicate operational identity: {identity}")
        seen.add(identity)
    if offset < 0 or (limit is not None and limit < 1):
        raise ValueError("invalid attention page")
    if view == "attention":
        available = [message for message in available if message.alarm or is_watch_text(message)]
    source_alarm_count = sum(message.alarm for message in available)
    watch_text_count = sum(not message.alarm and is_watch_text(message) for message in available)
    chronological_count = len(available) - source_alarm_count - watch_text_count
    available.sort(
        key=lambda message: (
            message.source_namespace,
            message.snapshot_id,
            message.row_uid,
        )
    )
    available.sort(key=lambda message: message.available_at, reverse=True)
    if view == "attention":
        available.sort(
            key=lambda message: {"source_alarm": 0, "watch_text": 1, "chronological": 2}[
                attention_band(message)
            ]
        )
    total = len(available)
    page = available[offset:] if limit is None else available[offset : offset + limit]
    entries = []
    for index, message in enumerate(page, start=offset + 1):
        band, reasons = explain_attention(
            alarm=message.alarm,
            sensor_type=message.sensor_type,
            value_raw=message.value_raw,
        )
        entries.append(
            AttentionEntry(
                message=message,
                review_order=index,
                attention_band=band,
                reason_codes=reasons,
                policy_version=POLICY_VERSION,
            )
        )
    return AttentionList(
        mode=mode,
        view=view,
        as_of=as_of,
        received_watermark=len(messages) if mode == "received" else None,
        policy_version=POLICY_VERSION,
        items=entries,
        offset=offset,
        total=total,
        all_records_total=all_records_total,
        source_alarm_count=source_alarm_count,
        watch_text_count=watch_text_count,
        chronological_count=chronological_count,
    )


def summarize_source_alarms(
    messages: list[ObservedMessage],
    *,
    as_of: datetime,
    event_from: datetime,
    mode: Literal["fixture", "replay", "received"],
    received_watermark: int | None = None,
    latest_limit: int = 10,
    object_limit: int = 100,
    scheme_lines: frozenset[tuple[str, str]] = frozenset(),
) -> SourceAlarmWindow:
    """«Сейчас» over in-memory records: the rules of ``storage.attention_pg`` for the fixture.

    Records with ``alarm=true``, event time from ``event_from``, available at ``as_of``;
    objects and latest records by event time, newest first (ties: larger ``row_uid``).
    ``scheme_lines`` — (object, channel) pairs drawn on the object's scheme.
    """
    window = sorted(
        (
            message
            for message in messages
            if message.alarm is True
            and message.event_at >= event_from
            and message.available_at <= as_of
        ),
        key=lambda message: (message.event_at, message.row_uid),
        reverse=True,
    )
    per_object: dict[str, list[ObservedMessage]] = {}
    for message in window:
        if message.object_id is not None:
            per_object.setdefault(message.object_id, []).append(message)
    objects = sorted(
        (
            SourceAlarmObject(
                object_id=object_id,
                object_name=records[0].object_name,
                record_count=len(records),
                channel_count=len({record.channel_id for record in records}),
                scheme_record_count=sum(
                    (object_id, record.channel_id) in scheme_lines for record in records
                ),
                first_event_at=records[-1].event_at,
                last_event_at=records[0].event_at,
                last_message=records[0],
            )
            for object_id, records in per_object.items()
        ),
        key=lambda item: item.object_id,
    )
    objects.sort(key=lambda item: item.last_event_at, reverse=True)
    return SourceAlarmWindow(
        mode=mode,
        as_of=as_of,
        received_watermark=received_watermark,
        event_from=event_from,
        record_count=len(window),
        object_count=len(objects),
        without_object_count=sum(message.object_id is None for message in window),
        objects=objects[:object_limit],
        latest=window[:latest_limit],
    )
