"""Frozen semantic mapping and leakage-safe timestamp grouping."""

from __future__ import annotations

import numpy as np
import pandas as pd

FAMILIES = {
    "КД Дверь": "contact",
    "КД АВ": "contact",
    "КД Люк": "contact",
    "Датчик движения": "motion",
    "Датчик температуры": "temperature",
    "Датчик дыма": "smoke",
    "Тепловой датчик": "heat",
    "Газовый датчик": "gas",
    "Состояние фазы": "phase",
}
ACTIVATIONS = {
    "Датчик дыма": "Обнаружен дым",
    "Тепловой датчик": "Не замкнут",
    "Ручной извещатель": "Не замкнут",
    "КД Дверь": "Не замкнут",
    "КД АВ": "Не замкнут",
    "КД Люк": "Не замкнут",
    "Датчик движения": "Обнаружено движение",
    "Газовый датчик": "Обнаружен газ",
    "Датчик затопления": "Не замкнут",
}
NORMAL = {"smoke": {"Дыма нет", "Норма"}, "heat": {"Замкнут", "Норма"}}
UNKNOWN = {"<CONFLICT>", "<NULL>", "Неопределен", "Отключено устройство", "Выключен"}
FEATURES = (
    "log_messages",
    "alarm_share",
    "fault_share",
    "activation_share",
    "unknown_share",
    "log_transitions",
    "log_channels",
    "log_mean_gap_hours",
    "log_max_gap_hours",
    "archive_covered",
    "log_available_onsets",
    "weekday_sin",
    "weekday_cos",
)


def mapping_table():
    return (
        [
            {
                "sensor_type": t,
                "family": f,
                "raw": "Неисправен",
                "target": "A",
                "meaning": "technical_fault_state_proxy; not physical failure",
            }
            for t, f in FAMILIES.items()
        ]
        + [
            {
                "sensor_type": t,
                "raw": s,
                "target": "B" if t in ACTIVATIONS and FAMILIES.get(t) in NORMAL else "context",
                "meaning": "activation; neither intrusion nor confirmed failure/fire",
            }
            for t, s in ACTIVATIONS.items()
        ]
        + [
            {"raw": s, "target": "unknown/context", "meaning": "not a negative failure label"}
            for s in sorted(UNKNOWN)
        ]
    )


def normalize(rows):
    """No arbitrary ordering of differing states at an equal channel timestamp."""
    rows = rows.copy()
    rows["at"] = pd.to_datetime(rows["at"])
    keys = ["object_id", "channel_id", "sensor_type", "at"]
    return (
        rows.groupby(keys, sort=True, as_index=False)
        .agg(
            state=("state", lambda s: s.iloc[0] if s.nunique(dropna=False) == 1 else "<CONFLICT>"),
            alarm=("alarm", "max"),
        )
        .fillna({"state": "<NULL>"})
    )


def derive_events(runs, covered):
    """A needs a known previous state; B needs a normal return and +/-10m isolation.

    runs include unknown/conflict states and next_state; archive barriers cannot be
    bridged. available_at is distinct from onset, especially for delayed B labels.
    """
    out = []
    acts = runs[runs.sensor_type.map(ACTIVATIONS).eq(runs.state)]
    by_obj = {}
    for obj, group in acts.groupby("object_id"):
        group = group.sort_values("at")
        by_obj[obj] = (
            group["at"].astype("datetime64[ns]").astype("int64").to_numpy(),
            group.channel_id.to_numpy(),
        )
    candidates = runs[
        (runs.state == "Неисправен")
        | (
            runs.sensor_type.isin(["Датчик дыма", "Тепловой датчик"])
            & runs.sensor_type.map(ACTIVATIONS).eq(runs.state)
        )
    ]
    for r in candidates.itertuples(index=False):
        family = FAMILIES.get(r.sensor_type)
        if family is None:
            continue
        cov = covered[family]
        prior = pd.notna(r.prev_at) and r.prev_state not in UNKNOWN
        if prior and r.state == "Неисправен":
            prior = all(d.date() in cov for d in pd.date_range(r.prev_at.date(), r.at.date()))
        component, available = None, r.at
        if r.state == "Неисправен" and prior and r.prev_state != "Неисправен":
            component = "A"
        if family in NORMAL and r.state == ACTIVATIONS[r.sensor_type]:
            limit = 120 if family == "smoke" else 60
            returned = (
                pd.notna(r.next_at)
                and r.next_state in NORMAL[family]
                and 0 < (r.next_at - r.at).total_seconds() <= limit
            )
            times, channels = by_obj[r.object_id]
            lo, hi = np.searchsorted(
                times, [r.at.value - 600_000_000_000, r.at.value + 600_000_000_000], side="left"
            )
            # Include the upper boundary, including tied timestamps.
            hi = np.searchsorted(times, r.at.value + 600_000_000_000, side="right")
            isolated = not np.any(channels[lo:hi] != r.channel_id)
            if returned and isolated:
                component = "B"
                available = max(r.next_at, r.at + pd.Timedelta(minutes=10))
        if component and all(d.date() in cov for d in pd.date_range(r.at.date(), available.date())):
            out.append(
                {
                    "object_id": str(r.object_id),
                    "family": family,
                    "channel_id": str(r.channel_id),
                    "at": r.at.isoformat(),
                    "available_at": available.isoformat(),
                    "component": component,
                }
            )
    return out


def sessions(runs):
    """Structural work-like chains and legacy alarm filter, not confirmed works."""
    fire = runs[
        runs.sensor_type.isin(["Датчик дыма", "Тепловой датчик", "Ручной извещатель"])
        & runs.state.isin(["Обнаружен дым", "Не замкнут", "Неисправен"])
    ].copy()
    fire = fire.sort_values(["object_id", "at"])
    if fire.empty:
        return []
    fire["chain"] = (
        (fire.object_id != fire.object_id.shift()) | (fire["at"].diff() > pd.Timedelta(minutes=10))
    ).cumsum()
    result = []
    for _, g in fire.groupby("chain"):
        start, end = g["at"].min(), g["at"].max()
        act = g[g.state != "Неисправен"]
        if (
            g.channel_id.nunique() >= 5
            and start.weekday() < 5
            and 7 <= start.hour < 19
            and len(act)
        ):
            result.append(
                {
                    "object_id": str(g.object_id.iloc[0]),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "channels": int(g.channel_id.nunique()),
                    "alarm_share": float(act.alarm.mean()),
                    "legacy": bool(act.alarm.mean() < 0.2),
                    "status": "inferred_work_like_not_confirmed",
                }
            )
    return result


def episode_heads(events):
    """Fixed 24h grouping per object/family; membership and available time retained.

    Continuations are never promoted after applying a maintenance mask.
    """
    groups = {}
    for event in sorted(
        events, key=lambda e: (e["object_id"], e["family"], e["at"], e["channel_id"])
    ):
        key = (event["object_id"], event["family"])
        previous = groups.get(key)
        at = pd.Timestamp(event["at"])
        if previous is None or at - pd.Timestamp(previous["last_at"]) >= pd.Timedelta(days=1):
            previous = dict(
                event, last_at=event["at"], channels=[event["channel_id"]], members=[event]
            )
            groups[key] = previous
            yield previous
        else:
            previous["last_at"] = event["at"]
            previous["members"].append(event)
            previous["channels"] = sorted(set(previous["channels"]) | {event["channel_id"]})
