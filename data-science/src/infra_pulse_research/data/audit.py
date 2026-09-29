"""Small reproducible audit helpers; not a production importer or label builder."""

import csv
import math
from collections import Counter
from datetime import date, timedelta
from typing import TextIO


def parse_alarm(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "t"}:
        return True
    if normalized in {"false", "f"}:
        return False
    raise ValueError(f"Unknown alarm flag: {value!r}")


def profile_events(stream: TextIO, types: dict[str, str], check_ids: bool = False) -> dict:
    reader = csv.DictReader(stream)
    required = {"ид_события", "ид_канала_данных", "дата", "время", "тревожное", "значение_датчика"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("Unexpected event schema")
    count = alarms = invalid_flags = unmatched_rows = duplicate_ids = nonnumeric = fault_rows = 0
    channels: set[str] = set()
    seen_ids: set[str] = set()
    nulls: Counter = Counter()
    states: Counter = Counter()
    alarm_types: Counter = Counter()
    fault_types: Counter = Counter()
    dates: Counter = Counter()
    fault_channels: set[str] = set()
    for row in reader:
        count += 1
        for key in required:
            if row.get(key) is None or not row[key].strip():
                nulls[key] += 1
        channel_id = row["ид_канала_данных"]
        channels.add(channel_id)
        if channel_id not in types:
            unmatched_rows += 1
        if check_ids:
            event_id = row["ид_события"]
            duplicate_ids += event_id in seen_ids
            seen_ids.add(event_id)
        try:
            alarm = parse_alarm(row["тревожное"])
        except (ValueError, AttributeError):
            invalid_flags += 1
            alarm = None
        value = (row["значение_датчика"] or "").strip()
        try:
            numeric = float(value.replace(",", "."))
            if not math.isfinite(numeric):
                raise ValueError("nonfinite")
        except ValueError:
            nonnumeric += 1
            # Known statuses only: never dump free-text values or names from source data.
            if value in {"Неисправен", "Норма", "Обнаружен дым"}:
                states[value] += 1
        if alarm:
            alarms += 1
            alarm_types[types.get(channel_id, "unmapped")] += 1
            if value == "Неисправен":
                fault_rows += 1
                fault_channels.add(channel_id)
                fault_types[types.get(channel_id, "unmapped")] += 1
        dates[row["дата"]] += 1
    parsed_dates = {date.fromisoformat(value) for value in dates}
    missing_dates = []
    if parsed_dates:
        current = min(parsed_dates)
        while current <= max(parsed_dates):
            if current not in parsed_dates:
                missing_dates.append(current.isoformat())
            current += timedelta(days=1)
    return {
        "rows": count,
        "channels": len(channels),
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
        "distinct_dates": len(dates),
        "missing_calendar_dates": missing_dates,
        "alarm_rows": alarms,
        "alarm_share": alarms / count if count else None,
        "invalid_alarm_flags": invalid_flags,
        "nulls": dict(nulls),
        "unmatched_rows": unmatched_rows,
        "duplicate_event_ids": duplicate_ids if check_ids else None,
        "nonnumeric_value_rows": nonnumeric,
        "known_state_rows": dict(states),
        "alarm_rows_by_sensor_type": dict(alarm_types),
        "technical_fault_state_rows": fault_rows,
        "technical_fault_channels": len(fault_channels),
        "fault_state_without_alarm_rows": states.get("Неисправен", 0) - fault_rows,
        "technical_fault_rows_by_sensor_type": dict(fault_types),
    }
