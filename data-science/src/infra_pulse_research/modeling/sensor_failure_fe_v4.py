"""Extended channel × 00:00-cutoff features for ``sensor-failure-tuning-v4``.

Groups (column prefixes), all point in time — strictly from records before t:

- ``n4__`` N, hidden nodes (idea I1): the channel's co-failure node from the
  monthly snapshot of :mod:`sensor_failure_nodes`; the node's history is the
  union of its channels' history (recency, counts, joint share, durations of
  ended episodes) plus card aggregates over the object × sensor type.
- ``e4__`` E, decayed counters and renewal (I4-lite): EWMA of episode starts
  (channel) and events (node, object × type, object) with half-lives 1/3/7/30 d,
  their short/long ratios, inter-event interval statistics and the expected next
  event, Hawkes-like intensities of candidate records (τ 6 h / 1 d / 7 d).
- ``k4__`` K, calendar of the forecast day: statutory holidays of the Labour
  Code of the Russian Federation, art. 112 (weekend transfers by government
  decrees are NOT applied), working day, day before a day off, day after a
  holiday, week and day of year as sin/cos. Weekday, weekend and month already
  are ``cal__`` columns of F2 and are not repeated.
- ``s4__`` S, network background: objects and nodes with events in 1/3/7 days in
  the network, the channel's sensor type and its area (level-2 parent of the
  object in the current reference), share of channels in an active episode.
- ``r4__`` R, relative anomaly: 7-day event counts of the object and the node
  against their own 7-day counts at the previous 180 daily cutoffs (z, rank).
- ``t4__`` T, PIT target encoding: events of the target known to qualify before
  t within 365 days, per object × sensor type and per node, shrunk to a
  type / node-size prior over the same 365 days.
- ``w4__`` W, power and works over the past day (idea I2): journal records of the
  object's *other* channels in ``[t − 24 h, t)`` — "Обесточен" of pumps/fans and of
  any type (flag and count), "Выключен" of devices other than pumps, fans and
  switches, "Не замкнут" of "КД Люк" (flags).

An *event* is a W-cluster of label episodes (any duration); a level sees it at
the first start of one of its members. Members of a cluster straddling t are
counted only if they started before t. Episode ends are read only when they are
before t (durations, active episode); the target's ``qualifying`` flag only as
"known to qualify" (start + filter < t). Rows: every registry channel with
``first_seen < t`` at every local midnight of the month (the F1–F3 grid).
"""

from __future__ import annotations

import datetime as dt
import math

import duckdb
import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_nodes as nodes_mod
from infra_pulse_research.modeling.sensor_failure_evaluation import static_list_scores
from infra_pulse_research.modeling.subsystem_grid import month_bounds

GROUPS = ("N", "E", "K", "S", "R", "T", "W")
PREFIX = {
    "N": "n4__",
    "E": "e4__",
    "K": "k4__",
    "S": "s4__",
    "R": "r4__",
    "T": "t4__",
    "W": "w4__",
}
ACTUATORS = ("Переключатель", "Состояние насоса", "Состояние вентилятора")
# Group W (texts as in artifacts/sensor-failure-v1/ideas/i2/i2.py): records of other
# channels of the object in [t − 24 h, t).
W_SIGNALS = {
    "pumpfan_deenergized": (
        "value_raw = 'Обесточен' AND sensor_type IN ('Состояние насоса', 'Состояние вентилятора')"
    ),
    "deenergized": "value_raw = 'Обесточен'",
    "switched_off_device": (
        "value_raw = 'Выключен' AND sensor_type NOT IN ("
        + ", ".join(f"'{a}'" for a in ACTUATORS)
        + ")"
    ),
    "hatch_open": "value_raw = 'Не замкнут' AND sensor_type = 'КД Люк'",
}
W_COUNTS = ("pumpfan_deenergized", "deenergized")
W_HOURS = 24
HALF_LIVES = (1, 3, 7, 30)
EWMA_WINDOW_DAYS = 300  # 10 longest half-lives: truncation below 0.1%
COUNT_DAYS = (7, 30, 90)
NET_DAYS = (1, 3, 7)
HAWKES_TAUS = {"6h": 0.25, "1d": 1.0, "7d": 7.0}
INTERVALS = 10
HISTORY_DAYS = 180
TE_DAYS = 365
TE_PRIOR_DAYS = 90
STATUTORY_HOLIDAYS = [(1, d) for d in range(1, 9)] + [
    (2, 23),
    (3, 8),
    (5, 1),
    (5, 9),
    (6, 12),
    (11, 4),
]
KEY_COLS = ["channel_id", "issued_at"]


def holidays(years, month_days=STATUTORY_HOLIDAYS) -> set[dt.date]:
    return {dt.date(y, m, d) for y in years for m, d in month_days}


def work_signals(
    con: duckdb.DuckDBPyConnection,
    events_view: str,
    end: str,
    *,
    month_column: str | None = None,
) -> pd.DataFrame:
    """Group W journal records (distinct object × channel × time × signal) before ``end``."""
    month_filter = f"AND {month_column} < '{end[:7]}'" if month_column else ""
    union = " UNION ALL ".join(
        f"SELECT object_id, channel_id, ts, '{name}' AS signal FROM j WHERE {cond}"
        for name, cond in W_SIGNALS.items()
    )
    return con.execute(f"""
        WITH j AS (
          SELECT object_id, channel_id, sensor_type, value_raw,
                 TRY_CAST(event_ts_local_raw AS TIMESTAMP) AS ts
          FROM {events_view}
          WHERE value_raw IN ('Обесточен', 'Выключен', 'Не замкнут') {month_filter}
            AND object_id IS NOT NULL AND channel_id IS NOT NULL
        )
        SELECT DISTINCT object_id, channel_id, ts, signal FROM ({union})
        WHERE ts IS NOT NULL AND ts < TIMESTAMP '{end}'
    """).df()


def month_grid(registry: pd.DataFrame, month: str) -> pd.DataFrame:
    """Registry channels with ``first_seen < t`` × local midnights of ``month``."""
    start, end = month_bounds(month)
    days = pd.date_range(start, end - dt.timedelta(days=1), freq="D")
    grid = registry.merge(pd.DataFrame({"issued_at": days}), how="cross")
    grid = grid[pd.to_datetime(grid["first_seen"]) < grid["issued_at"]]
    return grid.sort_values(KEY_COLS).reset_index(drop=True)


def _level_events(members: pd.DataFrame, key_cols: list[str], end) -> pd.DataFrame:
    """One row per (level key, event): first member start of that level (< end)."""
    m = members[pd.to_datetime(members["start_at"]) < pd.Timestamp(end)]
    out = m.groupby([*key_cols, "event_id"], sort=False)["start_at"].min().reset_index()
    return out.rename(columns={"start_at": "ts"})


class V4Month:
    """Feature groups of one month; ``inputs`` are development-only frames."""

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        registry: pd.DataFrame,
        members: pd.DataFrame,
        candidates: pd.DataFrame,
        month: str,
        *,
        episode_filter=None,
        holiday_dates: set[dt.date] | None = None,
        signals: pd.DataFrame | None = None,
    ):
        self.con, self.month = con, month
        self.signals = signals
        self.start, self.end = (pd.Timestamp(d) for d in month_bounds(month))
        self.registry = registry
        self.members = members[pd.to_datetime(members["start_at"]) < self.end].copy()
        self.candidates = candidates[pd.to_datetime(candidates["event_at"]) < self.end]
        self.episode_filter = episode_filter
        self.holiday_dates = holiday_dates
        self.grid = month_grid(registry, month)
        groups = nodes_mod.cofailure_groups(self.members, self.start)
        self.nodes = nodes_mod.node_map(registry["channel_id"], groups)
        self.grid = self.grid.merge(self.nodes, on="channel_id", how="left")
        self.grid["objtype"] = pd.factorize(
            pd.MultiIndex.from_frame(self.grid[["object_id", "sensor_type"]])
        )[0]
        types = self.grid[["object_id", "sensor_type", "objtype"]].drop_duplicates()
        m = self.members.merge(self.nodes[["channel_id", "node_id"]], on="channel_id", how="left")
        m["node_id"] = m["node_id"].fillna(m["channel_id"]).astype("int64")
        self.node_members = m
        self.levels = {
            "ch": _level_events(m, ["channel_id"], self.end).rename(columns={"channel_id": "key"}),
            "node": _level_events(m, ["node_id"], self.end).rename(columns={"node_id": "key"}),
            "objtype": _level_events(
                m.merge(types, on=["object_id", "sensor_type"]), ["objtype"], self.end
            ).rename(columns={"objtype": "key"}),
            "obj": _level_events(m, ["object_id"], self.end).rename(columns={"object_id": "key"}),
        }
        self.grid_key = {
            "ch": "channel_id",
            "node": "node_id",
            "objtype": "objtype",
            "obj": "object_id",
        }

    # -- helpers ------------------------------------------------------------------------

    def _queries(self, level: str) -> pd.DataFrame:
        col = self.grid_key[level]
        with_events = set(self.levels[level]["key"])
        q = (
            self.grid[[col, "issued_at"]]
            .drop_duplicates()
            .rename(columns={col: "key", "issued_at": "t"})
        )
        return q[q["key"].isin(with_events)].reset_index(drop=True)

    def _sql(self, sql: str, **frames) -> pd.DataFrame:
        for name, frame in frames.items():
            self.con.register(name, frame)
        try:
            return self.con.execute(sql).df()
        finally:
            for name in frames:
                self.con.unregister(name)

    def _to_grid(self, level: str, table: pd.DataFrame, cols: dict[str, str], fill=None):
        col = self.grid_key[level]
        right = table.rename(columns={"key": col, "t": "issued_at", **cols})
        right["issued_at"] = pd.to_datetime(right["issued_at"]).astype("datetime64[ns]")
        right[col] = right[col].astype(self.grid[col].dtype)
        out = self.grid[list(dict.fromkeys([*KEY_COLS, col]))].merge(
            right[[col, "issued_at", *cols.values()]], on=[col, "issued_at"], how="left"
        )
        if len(out) != len(self.grid):
            raise ValueError("level join changed the number of rows")
        frame = out[list(cols.values())]
        return frame.fillna(fill) if fill is not None else frame

    def _counts_ewma(self, level: str) -> pd.DataFrame:
        ewma = ", ".join(
            f"COALESCE(SUM(pow(2.0, -(epoch(q.t) - epoch(e.ts)) / 86400.0 / {h})), 0) AS ewma_{h}"
            for h in HALF_LIVES
        )
        counts = ", ".join(
            f"COUNT(e.ts) FILTER (WHERE e.ts >= q.t - INTERVAL {d} DAY) AS n_{d}"
            for d in COUNT_DAYS
        )
        return self._sql(
            f"""
            SELECT q.key, q.t, {counts}, {ewma}
            FROM q LEFT JOIN e ON e.key = q.key AND e.ts < q.t
             AND e.ts >= q.t - INTERVAL {EWMA_WINDOW_DAYS} DAY
            GROUP BY q.key, q.t
            """,
            q=self._queries(level),
            e=self.levels[level][["key", "ts"]],
        )

    def _intervals(self, level: str) -> pd.DataFrame:
        return self._sql(
            f"""
            WITH x AS (
              SELECT key, ts, epoch(ts) - epoch(LAG(ts) OVER (PARTITION BY key ORDER BY ts)) AS gap
              FROM e
            ), y AS (
              SELECT key, ts,
                     median(gap) OVER w AS med, avg(gap) OVER w AS mu,
                     stddev_samp(gap) OVER w AS sd, count(gap) OVER w AS n
              FROM x WINDOW w AS (PARTITION BY key ORDER BY ts
                                  ROWS BETWEEN {INTERVALS - 1} PRECEDING AND CURRENT ROW)
            )
            SELECT q.key, q.t, y.ts AS last_ts, y.med, y.mu, y.sd, y.n
            FROM q ASOF LEFT JOIN y ON y.key = q.key AND q.t > y.ts
            """,
            q=self._queries(level),
            e=self.levels[level][["key", "ts"]],
        )

    # -- groups -------------------------------------------------------------------------

    def group_n(self) -> pd.DataFrame:
        p = PREFIX["N"]
        out = self.grid[KEY_COLS].copy()
        out[f"{p}in_node"] = self.grid["in_node"].astype(bool).to_numpy()
        out[f"{p}node_size"] = self.grid["node_size"].astype("int64").to_numpy()
        counts = self._counts_ewma("node")
        cols = {f"n_{d}": f"{p}node_events_{d}d" for d in COUNT_DAYS}
        c = self._to_grid("node", counts, cols, fill=0)
        for col in cols.values():
            out[col] = c[col].astype("int64").to_numpy()
        last = self._intervals("node")
        last["hours"] = (last["t"] - last["last_ts"]).dt.total_seconds() / 3600
        out[f"{p}hours_since_node_event"] = self._to_grid(
            "node", last, {"hours": f"{p}hours_since_node_event"}
        )[f"{p}hours_since_node_event"].to_numpy()
        mem = self.node_members[["node_id", "event_id", "start_at", "end_at"]].rename(
            columns={"node_id": "key"}
        )
        joint = self._sql(
            f"""
            WITH per AS (
              SELECT q.key, q.t, m.event_id, COUNT(*) AS k
              FROM q JOIN m ON m.key = q.key AND m.start_at < q.t
               AND m.start_at >= q.t - INTERVAL {TE_DAYS} DAY
              GROUP BY q.key, q.t, m.event_id
            ), dur AS (
              SELECT q.key, q.t,
                     MEDIAN(date_diff('second', m.start_at, m.end_at) / 3600.0) AS med_h
              FROM q JOIN m ON m.key = q.key AND m.end_at < q.t
               AND m.start_at >= q.t - INTERVAL {TE_DAYS} DAY
              GROUP BY q.key, q.t
            )
            SELECT q.key, q.t, AVG(per.k) AS members, ANY_VALUE(dur.med_h) AS med_h
            FROM q LEFT JOIN per USING (key, t) LEFT JOIN dur USING (key, t)
            GROUP BY q.key, q.t
            """,
            q=self._queries("node"),
            m=mem,
        )
        j = self._to_grid(
            "node",
            joint,
            {"members": f"{p}node_members_per_event", "med_h": f"{p}node_duration_median_h"},
        )
        share = j[f"{p}node_members_per_event"].to_numpy() / out[f"{p}node_size"].to_numpy()
        out[f"{p}node_joint_share"] = np.where(out[f"{p}in_node"].to_numpy(), share, np.nan)
        out[f"{p}node_duration_median_h"] = j[f"{p}node_duration_median_h"].to_numpy()
        card = pd.DataFrame(
            {
                "object_id": self.grid["object_id"],
                "sensor_type": self.grid["sensor_type"],
                "issued_at": self.grid["issued_at"],
                "group_id": self.grid["node_id"].where(out[f"{p}in_node"]),
                "in_node": out[f"{p}in_node"],
                "ev30": out[f"{p}node_events_30d"],
                "hours": out[f"{p}hours_since_node_event"],
            }
        )
        keys = [card["object_id"], card["sensor_type"], card["issued_at"]]
        out[f"{p}card_groups"] = card.groupby(keys)["group_id"].transform("nunique").to_numpy()
        out[f"{p}card_share_in_node"] = card.groupby(keys)["in_node"].transform("mean").to_numpy()
        out[f"{p}card_max_node_events_30d"] = card.groupby(keys)["ev30"].transform("max").to_numpy()
        out[f"{p}card_min_hours_since_node_event"] = (
            card.groupby(keys)["hours"].transform("min").to_numpy()
        )
        return out

    def group_e(self) -> pd.DataFrame:
        p = PREFIX["E"]
        out = self.grid[KEY_COLS].copy()
        for level in ("ch", "node", "objtype", "obj"):
            counts = self._counts_ewma(level)
            cols = {f"ewma_{h}": f"{p}{level}_ewma_{h}d" for h in HALF_LIVES}
            c = self._to_grid(level, counts, cols, fill=0.0)
            for col in cols.values():
                out[col] = c[col].to_numpy()
            with np.errstate(divide="ignore", invalid="ignore"):
                e1, e7, e30 = (out[f"{p}{level}_ewma_{h}d"].to_numpy() for h in (1, 7, 30))
                out[f"{p}{level}_ratio_1_7"] = np.where(e7 > 0, e1 / e7, np.nan)
                out[f"{p}{level}_ratio_7_30"] = np.where(e30 > 0, e7 / e30, np.nan)
            iv = self._intervals(level)
            iv["med_days"] = iv["med"] / 86400
            iv["cv"] = np.where(iv["mu"] > 0, iv["sd"] / iv["mu"], np.nan)
            since_last = (iv["t"] - iv["last_ts"]).dt.total_seconds()
            iv["to_expected"] = (iv["med"] - since_last) / 86400  # last + median − t
            iv["due"] = (iv["to_expected"] <= 1).astype(float).where(iv["to_expected"].notna())
            cols = {
                "med_days": f"{p}{level}_interval_median_d",
                "cv": f"{p}{level}_interval_cv",
                "n": f"{p}{level}_intervals",
                "to_expected": f"{p}{level}_days_to_expected",
                "due": f"{p}{level}_due_within_1d",
            }
            v = self._to_grid(level, iv, cols)
            for col in cols.values():
                out[col] = v[col].to_numpy()
            out[f"{p}{level}_intervals"] = out[f"{p}{level}_intervals"].fillna(0)
        cand = self.candidates.merge(
            self.grid[["channel_id", "object_id", "objtype"]].drop_duplicates("channel_id"),
            on="channel_id",
        )
        for level, col in (("objtype", "objtype"), ("obj", "object_id")):
            buckets = self._sql(
                f"""
                SELECT {col} AS key, date_trunc('hour', event_at) + INTERVAL 30 MINUTE AS ts,
                       COUNT(*)::DOUBLE AS w
                FROM c GROUP BY 1, 2
                """,
                c=cand,
            )
            keys = (
                self.grid[[col, "issued_at"]]
                .drop_duplicates()
                .rename(columns={col: "key", "issued_at": "t"})
            )
            keys = keys[keys["key"].isin(set(buckets["key"]))]
            sums = ", ".join(
                f"COALESCE(SUM(b.w * exp(-(epoch(q.t) - epoch(b.ts)) / 86400.0 / {tau})), 0)"
                f" AS h_{name}"
                for name, tau in HAWKES_TAUS.items()
            )
            window = int(math.ceil(10 * max(HAWKES_TAUS.values())))
            hk = self._sql(
                f"""
                SELECT q.key, q.t, {sums}
                FROM q LEFT JOIN b ON b.key = q.key AND b.ts < q.t
                 AND b.ts >= q.t - INTERVAL {window} DAY
                GROUP BY q.key, q.t
                """,
                q=keys,
                b=buckets,
            )
            cols = {f"h_{name}": f"{p}{level}_hawkes_{name}" for name in HAWKES_TAUS}
            h = self._to_grid(level, hk, cols, fill=0.0)
            for c in cols.values():
                out[c] = h[c].to_numpy()
        return out

    def group_k(self) -> pd.DataFrame:
        p = PREFIX["K"]
        days = pd.to_datetime(self.grid["issued_at"]).dt.normalize()
        uniq = pd.DataFrame({"day": days.unique()})
        hol = sorted(self.holiday_dates or set())
        d = uniq["day"]

        def off(x: pd.Series) -> np.ndarray:
            return (x.dt.dayofweek >= 5).to_numpy() | x.dt.date.isin(hol).to_numpy()

        is_hol = d.dt.date.isin(hol).to_numpy()
        day_off = off(d)
        nxt = off(d + pd.Timedelta(days=1))
        after = np.zeros(len(d), dtype=bool)
        for k in (1, 2, 3):
            after |= (d - pd.Timedelta(days=k)).dt.date.isin(hol).to_numpy()
        to_next = np.full(len(d), 7.0)
        for k in range(6, -1, -1):
            to_next = np.where(off(d + pd.Timedelta(days=k)), k, to_next)
        week = d.dt.isocalendar().week.astype(float).to_numpy()
        doy = d.dt.dayofyear.astype(float).to_numpy()
        uniq[f"{p}is_holiday"] = is_hol
        uniq[f"{p}is_working_day"] = ~day_off
        uniq[f"{p}pre_day_off"] = ~day_off & nxt
        uniq[f"{p}after_holiday"] = ~day_off & after
        uniq[f"{p}days_to_day_off"] = to_next
        uniq[f"{p}week_sin"] = np.sin(2 * np.pi * week / 53)
        uniq[f"{p}week_cos"] = np.cos(2 * np.pi * week / 53)
        uniq[f"{p}doy_sin"] = np.sin(2 * np.pi * doy / 366)
        uniq[f"{p}doy_cos"] = np.cos(2 * np.pi * doy / 366)
        merged = pd.DataFrame({"day": days}).merge(uniq, on="day", how="left")
        out = self.grid[KEY_COLS].copy()
        for col in uniq.columns.drop("day"):
            out[col] = merged[col].to_numpy()
        return out

    def group_s(self) -> pd.DataFrame:
        p = PREFIX["S"]
        t_days = pd.DataFrame({"t": self.grid["issued_at"].drop_duplicates().sort_values()})
        obj = self.levels["obj"].rename(columns={"key": "object_id"})
        area = self.registry[["object_id", "area_id"]].drop_duplicates("object_id")
        obj = obj.merge(area, on="object_id", how="left")
        typed = self.node_members[["object_id", "sensor_type", "event_id", "start_at"]].rename(
            columns={"start_at": "ts"}
        )
        typed = typed.groupby(["object_id", "sensor_type", "event_id"])["ts"].min().reset_index()
        nodes_ev = self.levels["node"].merge(
            self.nodes.loc[self.nodes["in_node"], ["node_id"]].drop_duplicates(),
            left_on="key",
            right_on="node_id",
        )
        win = ", ".join(
            f"COUNT(DISTINCT x.k) FILTER (WHERE x.ts >= q.t - INTERVAL {d} DAY) AS n_{d}"
            for d in NET_DAYS
        )
        by = max(NET_DAYS)

        def distinct(frame: pd.DataFrame, group: str | None) -> pd.DataFrame:
            g = f", x.{group}" if group else ""
            return self._sql(
                f"""
                SELECT q.t{g}, {win}
                FROM q JOIN x ON x.ts < q.t AND x.ts >= q.t - INTERVAL {by} DAY
                GROUP BY q.t{g}
                """,
                q=t_days,
                x=frame,
            )

        out = self.grid[KEY_COLS].copy()
        net = t_days.merge(distinct(obj.assign(k=obj["object_id"]), None), on="t", how="left")
        nod = t_days.merge(distinct(nodes_ev.assign(k=nodes_ev["key"]), None), on="t", how="left")
        typ = distinct(typed.assign(k=typed["object_id"]), "sensor_type")
        are = distinct(obj.assign(k=obj["object_id"]).dropna(subset=["area_id"]), "area_id")
        g = self.grid[["issued_at", "sensor_type", "area_id"]].rename(columns={"issued_at": "t"})
        for d in NET_DAYS:
            out[f"{p}net_objects_{d}d"] = (
                g.merge(net, on="t", how="left")[f"n_{d}"].fillna(0).to_numpy()
            )
            out[f"{p}net_nodes_{d}d"] = (
                g.merge(nod, on="t", how="left")[f"n_{d}"].fillna(0).to_numpy()
            )
            out[f"{p}type_objects_{d}d"] = (
                g.merge(typ, on=["t", "sensor_type"], how="left")[f"n_{d}"].fillna(0).to_numpy()
            )
            area_n = g.merge(are, on=["t", "area_id"], how="left")[f"n_{d}"].fillna(0)
            out[f"{p}area_objects_{d}d"] = area_n.where(g["area_id"].notna()).to_numpy()
        active = self._sql(
            """
            SELECT g.channel_id, g.issued_at,
                   BOOL_OR(m.start_at IS NOT NULL) AS active
            FROM g LEFT JOIN m ON m.channel_id = g.channel_id AND m.start_at < g.issued_at
             AND (m.end_at IS NULL OR m.end_at >= g.issued_at)
            GROUP BY g.channel_id, g.issued_at
            """,
            g=self.grid[KEY_COLS],
            m=self.members[["channel_id", "start_at", "end_at"]],
        )
        a = self.grid[[*KEY_COLS, "sensor_type", "area_id"]].merge(active, on=KEY_COLS, how="left")
        a["active"] = a["active"].fillna(False).astype(float)
        out[f"{p}net_active_share"] = a.groupby("issued_at")["active"].transform("mean").to_numpy()
        out[f"{p}type_active_share"] = (
            a.groupby(["issued_at", "sensor_type"])["active"].transform("mean").to_numpy()
        )
        area_share = a.groupby(["issued_at", "area_id"])["active"].transform("mean")
        out[f"{p}area_active_share"] = area_share.where(a["area_id"].notna()).to_numpy()
        return out

    def _relative(self, level: str) -> pd.DataFrame:
        """7-day event count at t against the same count at the previous 180 cutoffs."""
        ev = self.levels[level]
        days = pd.date_range(
            self.start - pd.Timedelta(days=HISTORY_DAYS), self.end - pd.Timedelta(days=1), freq="D"
        ).to_numpy(dtype="datetime64[ns]")
        n_month = len(days) - HISTORY_DAYS
        week = np.timedelta64(7, "D")
        frames = []
        for key, part in ev.groupby("key", sort=False):
            ts = np.sort(part["ts"].to_numpy(dtype="datetime64[ns]"))
            x = np.searchsorted(ts, days, side="left") - np.searchsorted(
                ts, days - week, side="left"
            )
            if not x.any():
                continue
            hist = np.lib.stride_tricks.sliding_window_view(x, HISTORY_DAYS)[:n_month]
            now = x[HISTORY_DAYS:]
            sd = np.maximum(hist.std(axis=1, ddof=1), 0.25)
            rank = (hist < now[:, None]).sum(axis=1) + 0.5 * (hist == now[:, None]).sum(axis=1)
            frames.append(
                pd.DataFrame(
                    {
                        "key": key,
                        "t": days[HISTORY_DAYS:],
                        "x": now,
                        "z": (now - hist.mean(axis=1)) / sd,
                        "rank": rank / HISTORY_DAYS,
                    }
                )
            )
        if not frames:
            return pd.DataFrame(columns=["key", "t", "x", "z", "rank"])
        return pd.concat(frames, ignore_index=True)

    def group_r(self) -> pd.DataFrame:
        p = PREFIX["R"]
        out = self.grid[KEY_COLS].copy()
        for level, name in (("obj", "obj"), ("node", "node")):
            rel = self._relative(level)
            rel["t"] = pd.to_datetime(rel["t"])
            cols = {
                "x": f"{p}{name}_events_7d",
                "z": f"{p}{name}_events_7d_z",
                "rank": f"{p}{name}_events_7d_rank",
            }
            r = self._to_grid(level, rel, cols)
            out[cols["x"]] = r[cols["x"]].fillna(0).to_numpy()
            out[cols["z"]] = r[cols["z"]].fillna(0.0).to_numpy()
            out[cols["rank"]] = r[cols["rank"]].fillna(0.5).to_numpy()
        return out

    def group_t(self) -> pd.DataFrame:
        p = PREFIX["T"]
        out = self.grid[KEY_COLS].copy()
        g = self.grid
        first = g.groupby(["object_id", "sensor_type"])["first_seen"].transform("min")
        node_first = g.groupby("node_id")["first_seen"].transform("min")
        units = {
            "objtype": (["object_id", "sensor_type"], first, g["sensor_type"]),
            "node": (["node_id"], node_first, np.minimum(g["node_size"], 5).astype(str)),
        }
        for name, (unit, first_seen, prior_group) in units.items():
            cards = g[[*unit, "issued_at"]].copy()
            cards["prior_group"] = np.asarray(prior_group)
            cards["exposure"] = np.minimum(
                (g["issued_at"] - pd.to_datetime(first_seen)).dt.total_seconds() / 86400, TE_DAYS
            )
            cards = cards.drop_duplicates([*unit, "issued_at"]).reset_index(drop=True)
            cards["k"] = static_list_scores(
                cards, self.node_members, self.episode_filter, window_days=TE_DAYS, unit=tuple(unit)
            ).to_numpy()
            pool = cards.groupby(["issued_at", "prior_group"])[["k", "exposure"]].transform("sum")
            prior = pool["k"] / pool["exposure"].where(pool["exposure"] > 0)
            cards["te"] = (cards["k"] + TE_PRIOR_DAYS * prior.fillna(0)) / (
                cards["exposure"] + TE_PRIOR_DAYS
            )
            merged = g[[*unit, "issued_at"]].merge(
                cards[[*unit, "issued_at", "te"]], on=[*unit, "issued_at"], how="left"
            )
            out[f"{p}{name}_te"] = merged["te"].to_numpy()
        return out

    def group_w(self) -> pd.DataFrame:
        """Power and works over the past day on the object's other channels."""
        if self.signals is None:
            raise ValueError("group W needs journal signals (work_signals)")
        p = PREFIX["W"]
        sig = self.signals[
            (pd.to_datetime(self.signals["ts"]) < self.end)
            & (pd.to_datetime(self.signals["ts"]) >= self.start - pd.Timedelta(hours=W_HOURS))
        ]
        counts = ", ".join(
            f"COUNT(s.ts) FILTER (WHERE s.signal = '{name}' AND s.channel_id <> g.channel_id)"
            f" AS {name}"
            for name in W_SIGNALS
        )
        table = self._sql(
            f"""
            SELECT g.channel_id, g.issued_at, {counts}
            FROM g LEFT JOIN s ON s.object_id = g.object_id AND s.ts < g.issued_at
             AND s.ts >= g.issued_at - INTERVAL {W_HOURS} HOUR
            GROUP BY g.channel_id, g.issued_at
            """,
            g=self.grid[[*KEY_COLS, "object_id"]],
            s=sig,
        )
        table["issued_at"] = pd.to_datetime(table["issued_at"]).astype("datetime64[ns]")
        merged = self.grid[KEY_COLS].merge(table, on=KEY_COLS, how="left")
        out = self.grid[KEY_COLS].copy()
        for name in W_SIGNALS:
            n = merged[name].fillna(0).astype("int64").to_numpy()
            out[f"{p}{name}"] = n > 0
            if name in W_COUNTS:
                out[f"{p}{name}_n"] = n
        return out

    def build(self, groups=GROUPS) -> dict[str, pd.DataFrame]:
        makers = {
            "N": self.group_n,
            "E": self.group_e,
            "K": self.group_k,
            "S": self.group_s,
            "R": self.group_r,
            "T": self.group_t,
            "W": self.group_w,
        }
        result = {}
        for group in groups:
            frame = makers[group]()
            if len(frame) != len(self.grid) or frame.duplicated(KEY_COLS).any():
                raise ValueError(f"group {group}: rows differ from the channel-cutoff grid")
            result[group] = frame
        return result
