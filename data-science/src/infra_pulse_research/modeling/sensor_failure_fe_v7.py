"""Pair-level (object × sensor type) 00:00-cutoff features of ``sensor-failure-features-v7``.

A row is a card: pair × local midnight t, the universe of the model-protocol cards
(``model_labels_v5/cards``). All features are point in time — strictly from what is
known before t; the forecast window ``[t, t + D)`` enters only through the calendar.

Groups (column prefixes):

- ``p7h__`` H, pair history of the target: events of the pair in 7/14/30/35/90/180/365
  days, decayed counts (half-life 30/90 days), days since the last event, median and
  CV of the last up to 10 intervals. An event counts for the pair at the later of its
  first member start on the pair and the moment the event is known to qualify (the
  static-list rule of ``sensor_failure_evaluation.static_list_scores``), so
  ``p7h__events_365d`` equals the static list.
- ``p7s__`` S, severity of the history (365 days): durations of the pair's episodes that
  ended before t (median, maximum, share >= 1 h, share < 2 s), episodes, share of mass
  events (>= 5 members started before t), pair channels per event, and the last episode
  (still open at t, its duration if it ended, hours since its end).
- ``p7c__`` C, composition: known and candidate channels (label cards, point in time),
  channels in an active episode at t, share of channels with an episode in 365 days,
  age of the pair in days.
- ``p7x__`` X, context for pairs without recent history: events of the object that do
  not involve the pair (7/30 days), 365- and 30-day event rate of the sensor type per
  pair in the network and 365-day rate in the area (the pair itself excluded),
  "Обесточен" of pumps and fans of the object in ``[t − 24 h, t)`` (group W of v4), and
  the flag pre-registered from the DS-A study: "Неопределен" on the pair's channels on
  the previous day OR a message burst of the object on the previous day (>= 3 messages
  and more than 3 times the object's rate over days ``[t − 35, t − 8]``).
- ``p7k__`` K, forecast window: working days and statutory holidays (Labour Code art.
  112, no decree transfers) in ``[t, t + D)`` for D = 7 and 14 days, season as sin/cos.

Not included by the DS-A findings: weather, message-rate drops and silence, calendar
periodicity of the pair.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from infra_pulse_research.modeling.sensor_failure_evaluation import _known_after

KEYS = ["object_id", "sensor_type", "issued_at"]
UNIT = ["object_id", "sensor_type"]
PREFIX = {"H": "p7h__", "S": "p7s__", "C": "p7c__", "X": "p7x__", "K": "p7k__"}
GROUPS = tuple(PREFIX)
COUNT_DAYS = (7, 14, 30, 35, 90, 180, 365)
DECAY_DAYS = (30, 90)
INTERVALS = 10
SEVERITY_DAYS = 365
MASS_SIZE = 5
OBJECT_DAYS = (7, 30)
WINDOW_DAYS = (7, 14)
BURST_NORM = (35, 8)  # object message norm over days [t - 35, t - 8] (28 days)
PUMP_FAN = ("Состояние насоса", "Состояние вентилятора")
STATUTORY_HOLIDAYS = [(1, d) for d in range(1, 9)] + [
    (2, 23),
    (3, 8),
    (5, 1),
    (5, 9),
    (6, 12),
    (11, 4),
]
_DAY = np.timedelta64(1, "D")
_HOUR = np.timedelta64(1, "h")


def holidays(years, month_days=STATUTORY_HOLIDAYS) -> set[dt.date]:
    return {dt.date(y, m, d) for y in years for m, d in month_days}


def pair_event_times(members: pd.DataFrame, rule) -> pd.DataFrame:
    """Per (event, pair): ``first`` member start on the pair and ``count_at`` — the moment
    the event counts for the pair (later of ``first`` and the event known to qualify)."""
    m = members.copy()
    m["start_at"] = pd.to_datetime(m["start_at"])
    known = _known_after(m["start_at"], rule).where(m["qualifying"].astype(bool))
    event_known = known.groupby(m["event_id"]).min().rename("event_known")
    first = m.groupby(["event_id", *UNIT])["start_at"].min().rename("first").reset_index()
    first = first.merge(event_known, left_on="event_id", right_index=True, how="left")
    first = first.dropna(subset=["event_known"])
    first["count_at"] = first[["first", "event_known"]].max(axis=1)
    return first.drop(columns="event_known").sort_values("count_at").reset_index(drop=True)


def _positions(cards: pd.DataFrame, key) -> dict:
    return cards.groupby(key, sort=False).indices


def _tuple(key) -> tuple:
    return key if isinstance(key, tuple) else (key,)


def _window_counts(stamps: np.ndarray, t: np.ndarray, days: int) -> np.ndarray:
    w = np.timedelta64(int(days), "D")
    return np.searchsorted(stamps, t, "left") - np.searchsorted(stamps, t - w, "left")


def _decayed(stamps: np.ndarray, t: np.ndarray, half_life: float) -> np.ndarray:
    """Sum over stamps < t of 2^(-age / half-life), age in days (all history)."""
    if not len(stamps):
        return np.zeros(len(t))
    ref = stamps[0]
    x = (stamps - ref) / _DAY / half_life
    prefix = np.concatenate([[0.0], np.cumsum(np.exp2(x - x[-1]))])
    idx = np.searchsorted(stamps, t, "left")
    return np.exp2(x[-1] - ((t - ref) / _DAY) / half_life) * prefix[idx]


def _intervals(stamps: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Median (days) and CV of the last up to ``INTERVALS`` gaps between stamps < t."""
    med = np.full(len(t), np.nan)
    cv = np.full(len(t), np.nan)
    if len(stamps) < 2:
        return med, cv
    gaps = np.diff(stamps) / _DAY
    idx = np.searchsorted(stamps, t, "left")
    for i, n in enumerate(idx):
        if n < 2:
            continue
        g = gaps[max(0, n - 1 - INTERVALS) : n - 1]
        med[i] = np.median(g)
        if len(g) >= 2 and g.mean() > 0:
            cv[i] = g.std(ddof=1) / g.mean()
    return med, cv


def _chunks(n: int, rows_per_chunk: int):
    for start in range(0, n, rows_per_chunk):
        yield slice(start, min(n, start + rows_per_chunk))


class PairFeatures:
    """Build the v7 groups for a card table.

    ``cards`` needs ``object_id, sensor_type, issued_at`` (and optionally
    ``known_channels, candidate_channels``); ``members`` are the target's event members
    (all episodes, any duration) with ``start_at < data end`` and ends at or after the
    data end set to NaT; ``registry`` has ``channel_id, object_id, sensor_type,
    first_seen`` (and optionally ``area_id``); ``pair_days`` / ``object_days`` are the
    journal day aggregates of :func:`journal_day_aggregates` and ``signals`` the
    pump/fan "Обесточен" records (``object_id, ts``).
    """

    def __init__(
        self,
        cards: pd.DataFrame,
        members: pd.DataFrame,
        registry: pd.DataFrame,
        *,
        rule,
        pair_days: pd.DataFrame,
        object_days: pd.DataFrame,
        signals: pd.DataFrame,
        holiday_dates: set[dt.date],
    ):
        self.cards = cards.reset_index(drop=True)
        self.t = pd.to_datetime(self.cards["issued_at"]).to_numpy(dtype="datetime64[ns]")
        m = members.copy()
        m["start_at"] = pd.to_datetime(m["start_at"])
        m["end_at"] = pd.to_datetime(m["end_at"])
        # Stable, fully keyed order: "the last episode" of a pair (group S) must not depend
        # on the input order when several of its channels start at the same moment.
        order = [c for c in ("start_at", "channel_id", "episode_id") if c in m]
        self.members = m.sort_values(order, kind="mergesort").reset_index(drop=True)
        self.times = pair_event_times(self.members, rule)
        self.registry = registry
        self.pair_days = pair_days
        self.object_days = object_days
        self.signals = signals
        self.holiday_dates = holiday_dates

    # -- H ------------------------------------------------------------------------------

    def group_h(self) -> pd.DataFrame:
        p = PREFIX["H"]
        n = len(self.cards)
        out = {f"{p}events_{d}d": np.zeros(n) for d in COUNT_DAYS}
        out |= {f"{p}decay_{h}d": np.zeros(n) for h in DECAY_DAYS}
        out[f"{p}days_since_last"] = np.full(n, np.nan)
        out[f"{p}interval_median_d"] = np.full(n, np.nan)
        out[f"{p}interval_cv"] = np.full(n, np.nan)
        stamps = {
            k: part["count_at"].to_numpy(dtype="datetime64[ns]")
            for k, part in self.times.groupby(UNIT, sort=False)
        }
        for key, pos in _positions(self.cards, UNIT).items():
            s = stamps.get(key)
            if s is None:
                continue
            s = np.sort(s)
            t = self.t[pos]
            for d in COUNT_DAYS:
                out[f"{p}events_{d}d"][pos] = _window_counts(s, t, d)
            for h in DECAY_DAYS:
                out[f"{p}decay_{h}d"][pos] = _decayed(s, t, h)
            idx = np.searchsorted(s, t, "left")
            last = np.where(idx > 0, (t - s[np.maximum(idx - 1, 0)]) / _DAY, np.nan)
            out[f"{p}days_since_last"][pos] = last
            med, cv = _intervals(s, t)
            out[f"{p}interval_median_d"][pos] = med
            out[f"{p}interval_cv"][pos] = cv
        return pd.DataFrame(out)

    # -- S ------------------------------------------------------------------------------

    def group_s(self) -> pd.DataFrame:
        p = PREFIX["S"]
        n = len(self.cards)
        cols = [
            "episodes_365d",
            "dur_median_s_365d",
            "dur_max_h_365d",
            "share_ge1h_365d",
            "share_lt2s_365d",
            "mass_share_365d",
            "channels_per_event_365d",
            "last_open",
            "last_duration_h",
            "hours_since_last_end",
        ]
        out = {f"{p}{c}": np.full(n, np.nan) for c in cols}
        out[f"{p}episodes_365d"] = np.zeros(n)
        out[f"{p}last_open"] = np.zeros(n)
        window = np.timedelta64(SEVERITY_DAYS, "D")
        m = self.members
        # Moment an event becomes mass: start of its MASS_SIZE-th member.
        order = m.sort_values(["event_id", "start_at"])
        rank = order.groupby("event_id").cumcount()
        mass_at = order.loc[rank == MASS_SIZE - 1].set_index("event_id")["start_at"]
        times = self.times.merge(mass_at.rename("mass_at"), on="event_id", how="left")
        pair_events = {k: part for k, part in times.groupby(UNIT, sort=False)}
        pair_members = {k: part for k, part in m.groupby(UNIT, sort=False)}
        for key, pos in _positions(self.cards, UNIT).items():
            t_all = self.t[pos]
            mem = pair_members.get(key)
            if mem is not None:
                start = mem["start_at"].to_numpy(dtype="datetime64[ns]")
                end = mem["end_at"].to_numpy(dtype="datetime64[ns]")
                dur = mem["duration_hours"].to_numpy(dtype=float)
                ended = ~np.isnat(end)
                for sl in _chunks(len(pos), max(1, 2_000_000 // max(len(start), 1))):
                    t = t_all[sl][:, None]
                    started = start[None, :] < t
                    in_win = started & (start[None, :] >= t - window)
                    done = in_win & ended[None, :] & (end[None, :] < t)
                    idx = pos[sl]
                    out[f"{p}episodes_365d"][idx] = in_win.sum(axis=1)
                    k = done.sum(axis=1)
                    with np.errstate(invalid="ignore", divide="ignore"):
                        d = np.where(done, dur[None, :], np.nan)
                        has = k > 0
                        med = np.full(len(idx), np.nan)
                        mx = np.full(len(idx), np.nan)
                        if has.any():
                            med[has] = np.nanmedian(d[has], axis=1) * 3600
                            mx[has] = np.nanmax(d[has], axis=1)
                        out[f"{p}dur_median_s_365d"][idx] = med
                        out[f"{p}dur_max_h_365d"][idx] = mx
                        out[f"{p}share_ge1h_365d"][idx] = np.where(
                            has, (done & (dur[None, :] >= 1)).sum(axis=1) / k, np.nan
                        )
                        out[f"{p}share_lt2s_365d"][idx] = np.where(
                            has, (done & (dur[None, :] * 3600 < 2)).sum(axis=1) / k, np.nan
                        )
                    # Last episode started before t (members are sorted by start).
                    last = np.searchsorted(start, t_all[sl], "left") - 1
                    ok = last >= 0
                    li = np.maximum(last, 0)
                    open_ = ok & (~ended[li] | (end[li] >= t_all[sl]))
                    out[f"{p}last_open"][idx] = open_.astype(float)
                    closed = ok & ~open_
                    out[f"{p}last_duration_h"][idx] = np.where(closed, dur[li], np.nan)
                    out[f"{p}hours_since_last_end"][idx] = np.where(
                        closed, (t_all[sl] - end[li]) / _HOUR, np.nan
                    )
            pe = pair_events.get(key)
            if pe is None:
                continue
            count_at = pe["count_at"].to_numpy(dtype="datetime64[ns]")
            mass = pe["mass_at"].to_numpy(dtype="datetime64[ns]")
            ev_ids = pe["event_id"].to_numpy()
            pm = mem[mem["event_id"].isin(set(ev_ids))] if mem is not None else None
            code = {e: i for i, e in enumerate(ev_ids)}
            m_ev = pm["event_id"].map(code).to_numpy() if pm is not None else np.array([], int)
            m_start = (
                pm["start_at"].to_numpy(dtype="datetime64[ns]")
                if pm is not None
                else np.array([], "datetime64[ns]")
            )
            for sl in _chunks(len(pos), max(1, 2_000_000 // max(len(count_at) + len(m_start), 1))):
                t = t_all[sl][:, None]
                in_win = (count_at[None, :] < t) & (count_at[None, :] >= t - window)
                k = in_win.sum(axis=1)
                is_mass = in_win & ~np.isnat(mass)[None, :] & (mass[None, :] < t)
                idx = pos[sl]
                with np.errstate(invalid="ignore", divide="ignore"):
                    out[f"{p}mass_share_365d"][idx] = np.where(
                        k > 0, is_mass.sum(axis=1) / k, np.nan
                    )
                    if len(m_start):
                        counted = in_win[:, m_ev] & (m_start[None, :] < t)
                        out[f"{p}channels_per_event_365d"][idx] = np.where(
                            k > 0, counted.sum(axis=1) / k, np.nan
                        )
        return pd.DataFrame(out)

    # -- C ------------------------------------------------------------------------------

    def group_c(self) -> pd.DataFrame:
        p = PREFIX["C"]
        n = len(self.cards)
        out = {}
        for col in ("known_channels", "candidate_channels"):
            if col in self.cards:
                out[f"{p}{col}"] = self.cards[col].to_numpy(dtype=float)
        out[f"{p}active_channels"] = np.zeros(n)
        out[f"{p}share_channels_with_history_365d"] = np.full(n, np.nan)
        out[f"{p}pair_age_days"] = np.full(n, np.nan)
        reg = self.registry.copy()
        reg["first_seen"] = pd.to_datetime(reg["first_seen"])
        reg_pairs = {k: part for k, part in reg.groupby(UNIT, sort=False)}
        m = self.members
        pair_members = {k: part for k, part in m.groupby(UNIT, sort=False)}
        window = np.timedelta64(SEVERITY_DAYS, "D")
        for key, pos in _positions(self.cards, UNIT).items():
            t = self.t[pos]
            r = reg_pairs.get(key)
            if r is not None:
                first = np.sort(r["first_seen"].to_numpy(dtype="datetime64[ns]"))
                known = np.searchsorted(first, t, "left")
                out[f"{p}pair_age_days"][pos] = np.where(known > 0, (t - first[0]) / _DAY, np.nan)
            mem = pair_members.get(key)
            if mem is None:
                if r is not None:
                    out[f"{p}share_channels_with_history_365d"][pos] = np.where(
                        known > 0, 0.0, np.nan
                    )
                continue
            start = mem["start_at"].to_numpy(dtype="datetime64[ns]")
            end = mem["end_at"].to_numpy(dtype="datetime64[ns]")
            chan = mem["channel_id"].to_numpy()
            codes, uniq = pd.factorize(chan)
            for sl in _chunks(len(pos), max(1, 2_000_000 // max(len(start), 1))):
                tt = t[sl][:, None]
                active = (start[None, :] < tt) & (np.isnat(end)[None, :] | (end[None, :] >= tt))
                recent = (start[None, :] < tt) & (start[None, :] >= tt - window)
                act = np.zeros((len(tt), len(uniq)), dtype=bool)
                rec = np.zeros((len(tt), len(uniq)), dtype=bool)
                for j in range(len(uniq)):
                    cols = codes == j
                    act[:, j] = active[:, cols].any(axis=1)
                    rec[:, j] = recent[:, cols].any(axis=1)
                idx = pos[sl]
                out[f"{p}active_channels"][idx] = act.sum(axis=1)
                if r is not None:
                    kn = known[sl]
                    with np.errstate(invalid="ignore", divide="ignore"):
                        out[f"{p}share_channels_with_history_365d"][idx] = np.where(
                            kn > 0, np.minimum(rec.sum(axis=1) / np.maximum(kn, 1), 1.0), np.nan
                        )
        return pd.DataFrame(out)

    # -- X ------------------------------------------------------------------------------

    def group_x(self) -> pd.DataFrame:
        p = PREFIX["X"]
        n = len(self.cards)
        out = {f"{p}obj_other_events_{d}d": np.zeros(n) for d in OBJECT_DAYS}
        times = self.times
        # Object events that do not involve the pair: per object, event count time is the
        # earliest count over its pairs on the object.
        obj_events = times.groupby(["object_id", "event_id"])["count_at"].min().reset_index()
        pairs_of_event = times.groupby(["object_id", "event_id"])["sensor_type"].agg(frozenset)
        obj_events = obj_events.merge(
            pairs_of_event.rename("types").reset_index(), on=["object_id", "event_id"]
        )
        by_obj = {k: part for k, part in obj_events.groupby("object_id", sort=False)}
        for (obj, sensor), pos in _positions(self.cards, UNIT).items():
            part = by_obj.get(obj)
            if part is None:
                continue
            other = part[~part["types"].map(lambda s, x=sensor: x in s)]
            s = np.sort(other["count_at"].to_numpy(dtype="datetime64[ns]"))
            for d in OBJECT_DAYS:
                out[f"{p}obj_other_events_{d}d"][pos] = _window_counts(s, self.t[pos], d)
        # Rate of the sensor type per pair (network, area), the pair itself excluded.
        reg = self.registry.copy()
        reg["first_seen"] = pd.to_datetime(reg["first_seen"])
        pair_first = reg.groupby(UNIT)["first_seen"].min().rename("pair_first").reset_index()
        if "area_id" in reg:
            area = reg.drop_duplicates("object_id").set_index("object_id")["area_id"]
        else:
            area = pd.Series(dtype=float)
        pair_first["area_id"] = pair_first["object_id"].map(area)
        tm = times.merge(pair_first[[*UNIT, "area_id"]], on=UNIT, how="left")
        own = {
            k: np.sort(part["count_at"].to_numpy(dtype="datetime64[ns]"))
            for k, part in times.groupby(UNIT, sort=False)
        }
        cards_area = self.cards["object_id"].map(area).to_numpy()
        for scope, days in (("net", 365), ("net", 30), ("area", 365)):
            name = f"{p}type_rate_{scope}_{days}d"
            out[name] = np.full(n, np.nan)
            level = ["sensor_type"] if scope == "net" else ["sensor_type", "area_id"]
            stamps = {
                _tuple(k): np.sort(part["count_at"].to_numpy(dtype="datetime64[ns]"))
                for k, part in tm.dropna(subset=level).groupby(level, sort=False)
            }
            firsts = {
                _tuple(k): np.sort(part["pair_first"].to_numpy(dtype="datetime64[ns]"))
                for k, part in pair_first.dropna(subset=level).groupby(level, sort=False)
            }
            frame = self.cards[["object_id", "sensor_type"]].copy()
            frame["area_id"] = cards_area
            for key, pos in frame.groupby(level, sort=False, dropna=True).indices.items():
                key = _tuple(key)
                t = self.t[pos]
                s = stamps.get(key, np.array([], dtype="datetime64[ns]"))
                f = firsts.get(key, np.array([], dtype="datetime64[ns]"))
                total = _window_counts(s, t, days) if len(s) else np.zeros(len(t))
                pairs = np.searchsorted(f, t, "left")
                own_counts = np.zeros(len(t))
                objs = self.cards["object_id"].to_numpy()[pos]
                sensors = self.cards["sensor_type"].to_numpy()[pos]
                for pk in set(zip(objs, sensors, strict=True)):
                    sel = (objs == pk[0]) & (sensors == pk[1])
                    so = own.get(pk)
                    if so is not None:
                        own_counts[sel] = _window_counts(so, t[sel], days)
                with np.errstate(invalid="ignore", divide="ignore"):
                    out[name][pos] = np.where(
                        pairs > 1, (total - own_counts) / np.maximum(pairs - 1, 1), np.nan
                    )
        # Power of pumps and fans over the past day (group W of v4, object level).
        sig = self.signals
        out[f"{p}pumpfan_deenergized_n_1d"] = np.zeros(n)
        by_sig = {
            k: np.sort(part["ts"].to_numpy(dtype="datetime64[ns]"))
            for k, part in sig.groupby("object_id", sort=False)
        }
        for obj, pos in _positions(self.cards, "object_id").items():
            s = by_sig.get(obj)
            if s is None:
                continue
            t = self.t[pos]
            out[f"{p}pumpfan_deenergized_n_1d"][pos] = np.searchsorted(
                s, t, "left"
            ) - np.searchsorted(s, t - np.timedelta64(24, "h"), "left")
        out[f"{p}pumpfan_deenergized_1d"] = (out[f"{p}pumpfan_deenergized_n_1d"] > 0).astype(float)
        out[f"{p}health_flag_1d"] = self._health_flag()
        return pd.DataFrame(out)

    def _health_flag(self) -> np.ndarray:
        """DS-A flag: "Неопределен" on the pair on day t − 1 OR object message burst on t − 1."""
        day = pd.to_datetime(self.cards["issued_at"]).dt.normalize() - pd.Timedelta(days=1)
        pd_ = self.pair_days[["object_id", "sensor_type", "day", "undet"]].copy()
        pd_["day"] = pd.to_datetime(pd_["day"]).astype("datetime64[ns]")
        key = self.cards[UNIT].assign(day=day.to_numpy().astype("datetime64[ns]"))
        undet = key.merge(pd_, on=[*UNIT, "day"], how="left")["undet"].fillna(0).to_numpy() > 0
        od = self.object_days[["object_id", "day", "msg"]].copy()
        od["day"] = pd.to_datetime(od["day"]).astype("datetime64[ns]")
        burst = np.zeros(len(self.cards), dtype=bool)
        lo, hi = BURST_NORM
        for obj, part in od.groupby("object_id", sort=False):
            days = pd.date_range(part["day"].min() - pd.Timedelta(days=lo), part["day"].max())
            dense = pd.Series(0.0, index=days)
            dense.loc[part["day"].to_numpy()] = part["msg"].to_numpy(dtype=float)
            cum = np.concatenate([[0.0], np.cumsum(dense.to_numpy())])
            pos = np.flatnonzero(self.cards["object_id"].to_numpy() == obj)
            if not len(pos):
                continue
            prev = day.to_numpy()[pos]
            i = ((prev - days[0].to_datetime64()) / _DAY).astype(int)
            ok = (i >= 0) & (i < len(days))
            ii = np.clip(i, 0, len(days) - 1)
            msg1 = np.where(ok, cum[ii + 1] - cum[ii], 0.0)
            # Norm: days [t - 35, t - 8] = [prev - 34, prev - 7] (28 days).
            a = np.clip(ii - (lo - 1), 0, len(days))
            b = np.clip(ii - (hi - 1) + 1, 0, len(days))
            norm = np.where(ok, cum[b] - cum[a], 0.0) / (lo - hi + 1)
            burst[pos] = ok & (msg1 >= 3) & (msg1 > 3 * np.maximum(norm, 1 / (lo - hi + 1)))
        return (undet | burst).astype(float)

    # -- K ------------------------------------------------------------------------------

    def group_k(self) -> pd.DataFrame:
        p = PREFIX["K"]
        days = pd.to_datetime(self.cards["issued_at"]).dt.normalize()
        uniq = pd.DataFrame({"day": days.unique()})
        hol = sorted(self.holiday_dates)
        for d in WINDOW_DAYS:
            work = np.zeros(len(uniq))
            hcount = np.zeros(len(uniq))
            for k in range(d):
                x = uniq["day"] + pd.Timedelta(days=k)
                is_h = x.dt.date.isin(hol).to_numpy()
                work += ((x.dt.dayofweek < 5).to_numpy() & ~is_h).astype(float)
                hcount += is_h.astype(float)
            uniq[f"{p}workdays_{d}d"] = work
            uniq[f"{p}holidays_{d}d"] = hcount
        doy = uniq["day"].dt.dayofyear.to_numpy(dtype=float)
        uniq[f"{p}season_sin"] = np.sin(2 * np.pi * doy / 366)
        uniq[f"{p}season_cos"] = np.cos(2 * np.pi * doy / 366)
        merged = pd.DataFrame({"day": days}).merge(uniq, on="day", how="left")
        return merged.drop(columns="day").reset_index(drop=True)

    def build(self, groups=GROUPS) -> pd.DataFrame:
        makers = {
            "H": self.group_h,
            "S": self.group_s,
            "C": self.group_c,
            "X": self.group_x,
            "K": self.group_k,
        }
        out = self.cards[KEYS].copy()
        for col in ("sensor_type", "system_type"):
            if col in self.cards:
                out[f"cat__{col}"] = self.cards[col].astype(str).to_numpy()
        for group in groups:
            frame = makers[group]()
            if len(frame) != len(out):
                raise ValueError(f"group {group}: rows differ from the card table")
            for col in frame.columns:
                out[col] = frame[col].to_numpy()
        return out


def journal_day_aggregates(
    con, events_view: str, end: str, *, start: str | None = None, month_column: str | None = None
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Journal inputs of group X, strictly before ``end``: pair × day "Неопределен" rows,
    object × day messages (all channels of the object) and pump/fan "Обесточен" records."""
    lo = f"AND ts >= TIMESTAMP '{start}'" if start else ""
    months = ""
    if month_column:
        months = f"AND {month_column} <= '{end[:7]}'"
        if start:
            months += f" AND {month_column} >= '{start[:7]}'"
    base = f"""
        SELECT object_id, channel_id, sensor_type, value_raw,
               TRY_CAST(event_ts_local_raw AS TIMESTAMP) AS ts
        FROM {events_view}
        WHERE object_id IS NOT NULL {months}
    """
    pair_days = con.execute(f"""
        WITH j AS ({base})
        SELECT object_id, sensor_type, CAST(ts AS DATE) AS day, COUNT(*) AS undet
        FROM j WHERE value_raw = 'Неопределен' AND ts IS NOT NULL AND ts < TIMESTAMP '{end}' {lo}
        GROUP BY ALL
    """).df()
    object_days = con.execute(f"""
        WITH j AS ({base})
        SELECT object_id, CAST(ts AS DATE) AS day, COUNT(*) AS msg
        FROM j WHERE ts IS NOT NULL AND ts < TIMESTAMP '{end}' {lo}
        GROUP BY ALL
    """).df()
    fans = ", ".join(f"'{t}'" for t in PUMP_FAN)
    signals = con.execute(f"""
        WITH j AS ({base})
        SELECT DISTINCT object_id, channel_id, ts FROM j
        WHERE value_raw = 'Обесточен' AND sensor_type IN ({fans})
          AND ts IS NOT NULL AND ts < TIMESTAMP '{end}' {lo}
    """).df()
    return pair_days, object_days, signals


def within_day_rank(frame: pd.DataFrame, columns, *, by: str = "issued_at") -> pd.DataFrame:
    """Percentile rank of each column among the cards of the same issue day (ties share
    the average rank; missing stays missing). Point in time: every card of day t is
    known at t."""
    out = frame.copy()
    for col in columns:
        out[col] = frame.groupby(by)[col].rank(pct=True, method="average")
    return out
