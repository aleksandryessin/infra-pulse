"""Maintenance (TO) sessions of fire detectors and the 'fire detector failure' target.

Definitions fixed 27.09.2026 (DS-A v2, ``artifacts/next-2026-09-27/ds-a-tz-targets``,
``t5_labels_v2.py``; technologist review ``technologist-tz-targets/REVIEW.md``; the mask scope
"smoke and heat only" is the user's decision relayed by Head DS on 27.09.2026).

- A TO session is a group test of fire detectors on an object. It is inferred from the journal
  and was never confirmed by the customer. A masked event is not a proven non-failure.
- A self-reset is an **unconfirmed** activation that returned to a non-alarm state within
  seconds. It is not a confirmed false alarm, and a connection loss is not a confirmed failure.

Every function takes and returns ``pandas.DataFrame`` objects and never reads files, so the
same code serves the development folds and the holdout; the caller chooses the period.
Timestamps are naive local times (Europe/Moscow assumption of the curated journal).

Pipeline::

    runs = state_runs(text_rows)                 # text state rows of the object's channels
    sessions = detect_maintenance_sessions(runs)
    events = fire_detector_events(connection_losses, runs, sessions)
    flagged = apply_maintenance_mask(any_events, sessions)   # other targets: smoke/heat only
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DEFINITION_VERSION = "sensor-failure-maintenance-v1"
DEFINED_ON = "2026-09-27"

SMOKE, HEAT, MANUAL = "Датчик дыма", "Тепловой датчик", "Ручной извещатель"
FAULT_STATE = "Неисправен"
NEUTRAL_STATE = "Неопределен"

# TO sessions.
FIRE_DETECTOR_TYPES = (SMOKE, HEAT, MANUAL)
SESSION_STATES = ("Обнаружен дым", "Не замкнут", FAULT_STATE)
SESSION_GAP = pd.Timedelta(minutes=10)  # a new chain when the gap between starts is larger
SESSION_MIN_CHANNELS = 5
SESSION_WEEKDAYS = (0, 1, 2, 3, 4)  # Monday..Friday of the chain start
SESSION_HOURS = (7, 19)  # chain start hour in [7, 19): 07:00-18:59
SESSION_MAX_ALARM_SHARE = 0.2  # share of alarm_first among activation runs must be below

# Mask.
MASK_MARGIN = pd.Timedelta(minutes=30)
MASKED_SENSOR_TYPES = (SMOKE, HEAT)  # user decision: the phase and other types are not masked

# Activation states of any channel type (isolation of a self-reset).
ACTIVATION_STATES = {
    SMOKE: ("Обнаружен дым",),
    HEAT: ("Не замкнут",),
    MANUAL: ("Не замкнут",),
    "КД Дверь": ("Не замкнут",),
    "КД АВ": ("Не замкнут",),
    "КД Люк": ("Не замкнут",),
    "Стекло": ("Не замкнут",),
    "Датчик затопления": ("Не замкнут",),
    "Газовый датчик": ("Обнаружен газ",),
    "Датчик движения": ("Обнаружено движение",),
    "Датчик температуры": ("Температура ниже 3ºC", "Температура выше 40ºC"),
}

# Target 'fire detector failure' = (a) connection loss outside the mask + (b1) series of
# unconfirmed self-resets outside the mask.
TARGET_SENSOR_TYPES = (SMOKE, HEAT)
SELF_RESET_STATE = {SMOKE: "Обнаружен дым", HEAT: "Не замкнут"}
SELF_RESET_MAX = {SMOKE: pd.Timedelta(seconds=120), HEAT: pd.Timedelta(seconds=60)}
ISOLATION_WINDOW = pd.Timedelta(minutes=10)  # no activation of another channel, any alarm
SERIES_GAP = pd.Timedelta(hours=72)  # self-resets of one channel glued while gap <= 72 h
PAIR_WINDOW = pd.Timedelta(hours=24)  # 'b2' flag: >= 2 self-resets within 24 h in the series

ROW_COLUMNS = ("channel_id", "object_id", "sensor_type", "value_raw", "alarm", "ts")
RUN_COLUMNS = (
    "channel_id",
    "object_id",
    "sensor_type",
    "state",
    "start",
    "alarm_first",
    "next_start",
)
SESSION_COLUMNS = (
    "object_id",
    "session_start",
    "session_end",
    "channels",
    "activation_runs",
    "alarm_share",
)
SERIES_COLUMNS = ("object_id", "sensor_type", "channel_id", "start_at", "resets", "b2")
EVENT_COLUMNS = ("component", *SERIES_COLUMNS)


def _require(frame: pd.DataFrame, columns, name: str) -> None:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"{name}: missing columns {missing}")


def state_runs(rows: pd.DataFrame) -> pd.DataFrame:
    """State runs of text rows: a run starts when the channel's state changes.

    ``rows`` has ``channel_id, object_id, sensor_type, value_raw, alarm, ts`` and optionally
    ``ord`` (record ordinal, tie-break for equal ``ts``) and ``value_numeric`` (rows with a
    number are dropped: runs are defined on text states). Rows in the neutral state
    'Неопределен', without time or without object are dropped. ``alarm_first`` is the alarm
    flag of the first row of the run; ``next_start`` is the start of the next run of the
    channel (NaT for the last run, whose duration is unknown).
    """
    _require(rows, ROW_COLUMNS, "rows")
    keep = rows["ts"].notna() & rows["object_id"].notna() & rows["value_raw"].ne(NEUTRAL_STATE)
    if "value_numeric" in rows.columns:
        keep &= rows["value_numeric"].isna()
    order = ["channel_id", "ts", "ord"] if "ord" in rows.columns else ["channel_id", "ts"]
    r = rows.loc[keep].sort_values(order, kind="stable")
    ts = pd.to_datetime(r["ts"])
    same_channel = r["channel_id"].eq(r["channel_id"].shift())
    change = ~(same_channel & r["value_raw"].eq(r["value_raw"].shift()))
    starts = r.loc[change.to_numpy()]
    out = pd.DataFrame(
        {
            "channel_id": starts["channel_id"].to_numpy(),
            "object_id": starts["object_id"].to_numpy(),
            "sensor_type": starts["sensor_type"].to_numpy(),
            "state": starts["value_raw"].to_numpy(),
            "start": ts.loc[change.to_numpy()].to_numpy(),
            "alarm_first": starts["alarm"].fillna(False).astype(bool).to_numpy(),
        }
    )
    nxt = out["start"].shift(-1)
    last = out["channel_id"].ne(out["channel_id"].shift(-1))
    out["next_start"] = nxt.mask(last)
    return out.reset_index(drop=True)


def _chains(frame: pd.DataFrame, gap: pd.Timedelta) -> np.ndarray:
    """Chain ids over ``object_id, start`` (sorted): a new chain on a new object or gap > gap."""
    new = frame["object_id"].ne(frame["object_id"].shift()) | (frame["start"].diff() > gap)
    return new.cumsum().to_numpy()


def detect_maintenance_sessions(runs: pd.DataFrame) -> pd.DataFrame:
    """TO sessions per object: ``object_id, session_start, session_end, channels,
    activation_runs, alarm_share`` (the format of ``modeling.sensor_failure_service_mask``).

    Chain of run starts of fire detectors (smoke, heat, manual) in 'Обнаружен дым',
    'Не замкнут' or 'Неисправен', a new chain when the gap exceeds 10 min. A chain is a session
    if it covers >= 5 distinct channels, starts on a weekday at 07:00-18:59 and has at least one
    activation run (not 'Неисправен') with a share of ``alarm_first`` below 0.2.
    """
    _require(runs, RUN_COLUMNS[:6], "runs")
    f = runs[
        runs["sensor_type"].isin(FIRE_DETECTOR_TYPES) & runs["state"].isin(SESSION_STATES)
    ].sort_values(["object_id", "start"], kind="stable")
    if f.empty:
        return pd.DataFrame(columns=list(SESSION_COLUMNS))
    f = f.assign(
        _chain=_chains(f, SESSION_GAP),
        _act=f["state"].ne(FAULT_STATE),
    )
    f["_alarm_act"] = f["_act"] & f["alarm_first"].astype(bool)
    g = f.groupby("_chain", sort=True).agg(
        object_id=("object_id", "first"),
        session_start=("start", "min"),
        session_end=("start", "max"),
        channels=("channel_id", "nunique"),
        activation_runs=("_act", "sum"),
        _alarm=("_alarm_act", "sum"),
    )
    g["alarm_share"] = np.where(
        g["activation_runs"] > 0, g["_alarm"] / g["activation_runs"].clip(lower=1), np.nan
    )
    start = pd.to_datetime(g["session_start"])
    ok = (
        (g["channels"] >= SESSION_MIN_CHANNELS)
        & start.dt.dayofweek.isin(SESSION_WEEKDAYS)
        & (start.dt.hour >= SESSION_HOURS[0])
        & (start.dt.hour < SESSION_HOURS[1])
        & (g["activation_runs"] >= 1)
        & (g["alarm_share"] < SESSION_MAX_ALARM_SHARE)
    )
    return g.loc[ok, list(SESSION_COLUMNS)].reset_index(drop=True)


def maintenance_flags(
    events: pd.DataFrame,
    sessions: pd.DataFrame,
    margin: pd.Timedelta = MASK_MARGIN,
    time_col: str = "start_at",
    sensor_types=MASKED_SENSOR_TYPES,
) -> np.ndarray:
    """Boolean per event: the sensor type is masked and ``time_col`` lies in
    ``[session_start - margin, session_end + margin]`` of a session of the same object
    (both ends included)."""
    _require(events, ("object_id", "sensor_type", time_col), "events")
    out = np.zeros(len(events), dtype=bool)
    if not len(events) or sessions is None or not len(sessions):
        return out
    typed = events["sensor_type"].isin(set(sensor_types)).to_numpy()
    if not typed.any():
        return out
    t = pd.to_datetime(events[time_col]).to_numpy()
    obj = events["object_id"].to_numpy()
    spans = {
        o: (
            (pd.to_datetime(g["session_start"]) - margin).to_numpy(),
            (pd.to_datetime(g["session_end"]) + margin).to_numpy(),
        )
        for o, g in sessions.groupby("object_id", sort=False)
    }
    for o in pd.unique(obj[typed]):
        if o not in spans:
            continue
        a, b = spans[o]
        idx = np.flatnonzero(typed & (obj == o))
        ti = t[idx][:, None]
        out[idx] = ((a[None, :] <= ti) & (ti <= b[None, :])).any(axis=1)
    return out


def apply_maintenance_mask(
    events: pd.DataFrame,
    sessions: pd.DataFrame,
    margin: pd.Timedelta = MASK_MARGIN,
    time_col: str = "start_at",
    drop: bool = False,
) -> pd.DataFrame:
    """Copy of ``events`` with ``maintenance_masked``: True for smoke and heat events inside
    a session of their object widened by ``margin``. Other sensor types (the phase included)
    are never masked. With ``drop=True`` the masked events are removed."""
    out = events.copy()
    out["maintenance_masked"] = maintenance_flags(events, sessions, margin, time_col)
    if drop:
        out = out[~out["maintenance_masked"]].reset_index(drop=True)
    return out


def activation_starts(runs: pd.DataFrame) -> pd.DataFrame:
    """Runs in an activation state of their sensor type (any alarm flag)."""
    allowed = pd.MultiIndex.from_tuples(
        [(t, s) for t, states in ACTIVATION_STATES.items() for s in states]
    )
    hit = pd.MultiIndex.from_arrays([runs["sensor_type"], runs["state"]]).isin(allowed)
    return runs.loc[hit].reset_index(drop=True)


def _isolated(cand: pd.DataFrame, acts: pd.DataFrame, window: pd.Timedelta) -> np.ndarray:
    """No activation start of another channel of the object in ``[t - window, t + window]``."""
    out = np.ones(len(cand), dtype=bool)
    by_obj = {o: g for o, g in acts.groupby("object_id", sort=False)}
    w = window.to_timedelta64()
    starts = pd.to_datetime(cand["start"]).to_numpy()
    channels = cand["channel_id"].to_numpy()
    for o, idx in cand.groupby("object_id", sort=False).indices.items():
        g = by_obj.get(o)
        if g is None:
            continue
        at = pd.to_datetime(g["start"]).to_numpy()
        order = np.argsort(at, kind="stable")
        at, ach = at[order], g["channel_id"].to_numpy()[order]
        for i in idx:
            lo = np.searchsorted(at, starts[i] - w, "left")
            hi = np.searchsorted(at, starts[i] + w, "right")
            out[i] = not np.any(ach[lo:hi] != channels[i])
    return out


def self_reset_candidates(runs: pd.DataFrame, sessions: pd.DataFrame) -> pd.DataFrame:
    """Unconfirmed self-resets with flags: smoke 'Обнаружен дым' / heat 'Не замкнут' with
    ``alarm_first`` and a known duration <= 120 s / 60 s; ``masked`` (inside a TO session
    +- 30 min) and ``isolated`` (no activation of another channel of the object in +-10 min,
    any alarm flag)."""
    _require(runs, RUN_COLUMNS, "runs")
    r = runs[runs["sensor_type"].isin(TARGET_SENSOR_TYPES)]
    r = r[r["state"].eq(r["sensor_type"].map(SELF_RESET_STATE)) & r["alarm_first"].astype(bool)]
    dur = pd.to_datetime(r["next_start"]) - pd.to_datetime(r["start"])
    limit = r["sensor_type"].map(SELF_RESET_MAX)
    cand = r[dur.notna() & (dur <= limit)].reset_index(drop=True)
    cand["masked"] = maintenance_flags(cand, sessions, time_col="start")
    cand["isolated"] = _isolated(cand, activation_starts(runs), ISOLATION_WINDOW)
    return cand


def glue_series(resets: pd.DataFrame) -> pd.DataFrame:
    """Series of self-resets per channel, glued while the next starts <= 72 h after the
    previous one: ``object_id, sensor_type, channel_id, start_at`` (first reset), ``resets``
    and ``b2`` (the series has two resets <= 24 h apart)."""
    if resets.empty:
        return pd.DataFrame(columns=list(SERIES_COLUMNS))
    s = resets.sort_values(["channel_id", "start"], kind="stable").reset_index(drop=True)
    gap = s["start"].diff()
    same = s["channel_id"].eq(s["channel_id"].shift())
    s["_series"] = (~(same & (gap <= SERIES_GAP))).cumsum()
    s["_pair"] = same & (gap <= PAIR_WINDOW) & s["_series"].eq(s["_series"].shift())
    g = s.groupby("_series", sort=True).agg(
        object_id=("object_id", "first"),
        sensor_type=("sensor_type", "first"),
        channel_id=("channel_id", "first"),
        start_at=("start", "min"),
        resets=("start", "size"),
        b2=("_pair", "any"),
    )
    return g.reset_index(drop=True)


def fire_detector_events(
    connection_losses: pd.DataFrame, runs: pd.DataFrame, sessions: pd.DataFrame
) -> pd.DataFrame:
    """Events of the target 'fire detector failure' (smoke and heat):
    ``component, object_id, sensor_type, channel_id, start_at, resets, b2``.

    - ``a``: connection-loss starts (``connection_losses``: ``object_id, sensor_type,
      start_at`` and optionally ``channel_id``) of smoke / heat outside the TO mask;
    - ``b1``: series of isolated unconfirmed self-resets outside the TO mask (``b2`` marks the
      stricter variant: >= 2 self-resets within 24 h).
    """
    _require(connection_losses, ("object_id", "sensor_type", "start_at"), "connection_losses")
    cl = connection_losses[connection_losses["sensor_type"].isin(TARGET_SENSOR_TYPES)]
    cl = apply_maintenance_mask(cl, sessions, drop=True)
    a = pd.DataFrame(
        {
            "component": "a",
            "object_id": cl["object_id"].to_numpy(),
            "sensor_type": cl["sensor_type"].to_numpy(),
            "channel_id": cl["channel_id"].to_numpy() if "channel_id" in cl else pd.NA,
            "start_at": pd.to_datetime(cl["start_at"]).to_numpy(),
            "resets": 0,
            "b2": False,
        }
    )
    cand = self_reset_candidates(runs, sessions)
    b = glue_series(cand[cand["isolated"] & ~cand["masked"]]).assign(component="b1")
    parts = [f[list(EVENT_COLUMNS)] for f in (a, b) if len(f)]
    if not parts:
        return pd.DataFrame(columns=list(EVENT_COLUMNS))
    out = pd.concat(parts, ignore_index=True)
    out = out[list(EVENT_COLUMNS)].sort_values(["start_at", "component"], kind="stable")
    return out.reset_index(drop=True)
