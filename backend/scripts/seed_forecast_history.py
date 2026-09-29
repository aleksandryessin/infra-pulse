"""Seed the phase history and the forecast journal of a received scope (outside HTTP).

Reads the curated snapshot ``journal-curated-v4`` through DuckDB (dependency group
``data``) and writes into PostgreSQL:

1. the channel layout of every reference channel (picket and, for «Состояние фазы»,
   feeder kind by ``infra_pulse_core.features.channel_names``) and object names;
2. every «Состояние фазы» record of the working layer — the periods excluded by the
   research overlay ``alarm-spike-exclusions-v1`` are skipped exactly as in research —
   into ``dispatch_observations`` of the received scope with dense ``received_position``;
3. coverage days of the working layer (policy days marked);
4. one ``recompute``: the same core detector and list as the worker, from ``--list-from``
   to the end of the data. The scope must be empty (a new stream).

``--synthetic-day`` then appends one synthetic day (~170 000 rows, all sensor types,
after a gap like July 2026) and times the recompute of it. Nothing is written to Git.

    INFRA_DB_DSN=postgresql://... uv run --locked --group data --group platform \\
      python backend/scripts/seed_forecast_history.py --data-root /path/to/checkout \\
      --namespace local-received --stream stand-v1 --list-from 2023-01-01 --synthetic-day
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import psycopg

from infra_pulse_backend.operations import forecast_publish as fp
from infra_pulse_backend.storage import forecast_pg as pg
from infra_pulse_core.features.phase_feeder_episodes import MSK, PHASE_SENSOR_TYPE

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "backend/migrations"
SNAPSHOT = "data-science/artifacts/curated-all-sensors-exclude-2021-q2"
POLICY = "data-science/artifacts/alarm-spike-exclusions-v1"


def _q(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def apply_migrations(connection: psycopg.Connection) -> None:
    for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        connection.execute(migration.read_text(encoding="utf-8"))


def policy_rules(policy_dir: Path) -> list[dict]:
    manifest = json.loads((policy_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["policy_version"] != "alarm-spike-exclusions-v1":
        raise SystemExit("unexpected policy overlay version")
    return [rule for rule in manifest["rules"] if rule["sensor_type"] in (None, PHASE_SENSOR_TYPE)]


def load_layout(connection: psycopg.Connection, con, snapshot: Path) -> dict:
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((snapshot / "channels.parquet").read_bytes()).hexdigest()
    version = f"{manifest['etl_version']}:channels-{digest[:16]}"
    rows = con.execute(
        f"""SELECT CAST(channel_id AS VARCHAR), name_raw, CAST(object_id AS VARCHAR),
                   sensor_type, system_type, tag_raw
            FROM read_parquet({_q(snapshot / "channels.parquet")})"""
    ).fetchall()
    count = pg.upsert_layout(
        connection,
        [
            fp.layout_row(
                channel_id,
                name=" ".join((name or "").split()) or None,
                object_id=object_id,
                sensor_type=sensor_type,
                system_type=system_type,
                tag=tag,
                reference_version=version,
            )
            for channel_id, name, object_id, sensor_type, system_type, tag in rows
        ],
    )
    objects = con.execute(
        f"""SELECT "ид_объект", "диспетчерское_название_объекта"
            FROM read_parquet({_q(snapshot / "objects.parquet")})"""
    ).fetchall()
    pg.upsert_objects(connection, [(str(oid), name, version) for oid, name in objects])
    return {"channels": count, "objects": len(objects), "reference_version": version}


def open_scope(connection: psycopg.Connection, namespace: str, stream: str) -> None:
    now = datetime.now(UTC)
    connection.execute(
        """INSERT INTO dispatch_replay_snapshots (namespace_id, snapshot_id, manifest_sha256,
             window_start, window_end, row_count, alarm_count, scope_kind)
           VALUES (%s, %s, NULL, %s, %s, 0, 0, 'received')
           ON CONFLICT (namespace_id, snapshot_id) DO NOTHING""",
        (namespace, stream, now - timedelta(microseconds=1), now),
    )
    kind, rows = connection.execute(
        """SELECT scope_kind, row_count FROM dispatch_replay_snapshots
           WHERE namespace_id = %s AND snapshot_id = %s FOR UPDATE""",
        (namespace, stream),
    ).fetchone()
    if kind != "received" or rows:
        raise SystemExit("the seed needs a new, empty received stream")


def load_phase(connection, con, snapshot: Path, rules: list[dict], namespace, stream) -> dict:
    excluded = " OR ".join(
        f"(CAST(TRY_CAST(event_ts_local_raw AS TIMESTAMP) AS DATE) >= DATE {_q(r['start'])} "
        f"AND CAST(TRY_CAST(event_ts_local_raw AS TIMESTAMP) AS DATE) < DATE {_q(r['end'])})"
        for r in rules
    )
    where = f"sensor_type = {_q(PHASE_SENSOR_TYPE)} AND channel_id IS NOT NULL " + (
        "AND TRY_CAST(event_ts_local_raw AS TIMESTAMP) IS NOT NULL"
    )
    if excluded:
        where += f" AND NOT ({excluded})"
    cursor = con.execute(
        f"""SELECT row_uid, CAST(event_id AS VARCHAR), CAST(channel_id AS VARCHAR),
                   CAST(object_id AS VARCHAR), sensor_type, system_type, value_raw,
                   value_numeric, COALESCE(alarm, false),
                   TRY_CAST(event_ts_local_raw AS TIMESTAMP), source_file, source_sha256,
                   record_ordinal, event_ts_local_raw, COALESCE(is_epoch_placeholder, false)
            FROM read_parquet({_q(snapshot / "curated/*/*.parquet")})
            WHERE {where}
            ORDER BY TRY_CAST(event_ts_local_raw AS TIMESTAMP), row_uid"""
    )
    position = alarms = 0
    first = last = None
    with connection.cursor() as writer:
        with writer.copy(
            """COPY dispatch_observations (namespace_id, snapshot_id, row_uid, source_event_id,
                 channel_id, object_id, sensor_type, system_type, value_raw, value_numeric,
                 alarm, event_at, available_at, availability_basis, source_file,
                 source_sha256, record_ordinal, event_local_raw, quality_flags,
                 received_position)
               FROM STDIN"""
        ) as copy:
            while batch := cursor.fetchmany(50_000):
                for (
                    uid,
                    event_id,
                    channel,
                    obj,
                    sensor,
                    system,
                    value,
                    number,
                    alarm,
                    local,
                    source_file,
                    source_sha,
                    ordinal,
                    raw,
                    epoch,
                ) in batch:
                    position += 1
                    at = local.replace(tzinfo=MSK)
                    first = first or at
                    last = at
                    alarms += bool(alarm)
                    copy.write_row(
                        (
                            namespace,
                            stream,
                            uid,
                            event_id,
                            channel,
                            obj,
                            sensor,
                            system,
                            value if value is not None else "",
                            number,
                            alarm,
                            at,
                            at,
                            "observed",
                            source_file or "curated",
                            source_sha or "",
                            ordinal or 0,
                            raw,
                            json.dumps(["is_epoch_placeholder"] if epoch else []),
                            position,
                        )
                    )
    connection.execute(
        """UPDATE dispatch_replay_snapshots SET row_count = %s, alarm_count = %s,
             window_start = %s, window_end = %s, last_received_at = clock_timestamp()
           WHERE namespace_id = %s AND snapshot_id = %s""",
        (position, alarms, first, last + timedelta(microseconds=1), namespace, stream),
    )
    return {"rows": position, "alarms": alarms, "first": first, "last": last}


def load_coverage(connection, con, snapshot: Path, policy_dir: Path, scope: pg.Scope) -> int:
    days = con.execute(
        f"""SELECT day, working_covered FROM read_parquet({_q(snapshot / "coverage.parquet")})"""
    ).fetchall()
    policy = {
        day
        for day, sensor in con.execute(
            f"""SELECT day, sensor_type_scope
                FROM read_parquet({_q(policy_dir / "policy_days.parquet")})"""
        ).fetchall()
        if sensor in (None, PHASE_SENSOR_TYPE)
    }
    pg.upsert_coverage(
        connection,
        scope,
        [(day, bool(covered), day in policy, "curated_coverage") for day, covered in days],
    )
    return len(days)


def synthetic_day(connection, scope: pg.Scope, day: date, rows_total: int, seed: int) -> int:
    """One synthetic day after the data: phase channels keep their pattern of «Есть питание»
    / «Обесточен» with a few «Неисправен»; other rows are synthetic channels of other types.
    """
    rnd = random.Random(seed)
    phase = connection.execute(
        """SELECT channel_id, object_id FROM forecast_channel_layout
           WHERE sensor_type = %s AND object_id IS NOT NULL ORDER BY channel_id""",
        (PHASE_SENSOR_TYPE,),
    ).fetchall()
    start = datetime(day.year, day.month, day.day, tzinfo=MSK)
    top = connection.execute(
        """SELECT row_count FROM dispatch_replay_snapshots
           WHERE namespace_id = %s AND snapshot_id = %s FOR UPDATE""",
        scope.key,
    ).fetchone()[0]
    rows = []
    for channel, obj in phase:
        for hour in (3, 9, 15, 21):
            rows.append(
                (
                    channel,
                    obj,
                    PHASE_SENSOR_TYPE,
                    "Есть питание",
                    False,
                    start + timedelta(hours=hour, seconds=rnd.randrange(3600)),
                )
            )
        if rnd.random() < 0.02:
            at = start + timedelta(hours=12, seconds=rnd.randrange(3600))
            rows.append((channel, obj, PHASE_SENSOR_TYPE, "Неисправен", True, at))
            rows.append(
                (channel, obj, PHASE_SENSOR_TYPE, "Обесточен", False, at + timedelta(minutes=1))
            )
            rows.append(
                (channel, obj, PHASE_SENSOR_TYPE, "Есть питание", False, at + timedelta(hours=1))
            )
    types = ["Датчик дыма", "Датчик температуры", "Газовый датчик", "КД Дверь"]
    while len(rows) < rows_total:
        sensor = types[len(rows) % len(types)]
        value = "Норма" if sensor != "Датчик температуры" else f"{rnd.uniform(5, 30):.1f}"
        rows.append(
            (
                f"synthetic-{len(rows) % 20000}",
                None,
                sensor,
                value,
                False,
                start + timedelta(seconds=rnd.randrange(86400)),
            )
        )
    with connection.cursor() as writer:
        with writer.copy(
            """COPY dispatch_observations (namespace_id, snapshot_id, row_uid, channel_id,
                 object_id, sensor_type, value_raw, alarm, event_at, available_at,
                 availability_basis, source_file, source_sha256, record_ordinal,
                 event_local_raw, received_position) FROM STDIN"""
        ) as copy:
            for index, (channel, obj, sensor, value, alarm, at) in enumerate(rows, start=1):
                copy.write_row(
                    (
                        *scope.key,
                        f"synthetic-{day:%Y%m%d}-{index}",
                        channel,
                        obj,
                        sensor,
                        value,
                        alarm,
                        at,
                        at,
                        "observed",
                        "synthetic-day.csv",
                        "0" * 64,
                        index,
                        at.strftime("%d.%m.%Y %H:%M:%S"),
                        top + index,
                    )
                )
    connection.execute(
        """UPDATE dispatch_replay_snapshots SET row_count = row_count + %s
           WHERE namespace_id = %s AND snapshot_id = %s""",
        (len(rows), *scope.key),
    )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--data-root", type=Path, default=ROOT)
    parser.add_argument("--namespace", default=os.environ.get("INFRA_RECEIVED_NAMESPACE"))
    parser.add_argument("--stream", default=os.environ.get("INFRA_RECEIVED_STREAM_ID"))
    parser.add_argument("--list-from", type=date.fromisoformat, default=date(2023, 1, 1))
    parser.add_argument("--synthetic-day", action="store_true")
    parser.add_argument("--synthetic-date", type=date.fromisoformat, default=date(2026, 8, 1))
    parser.add_argument("--synthetic-rows", type=int, default=170_000)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    dsn = os.environ.get("INFRA_DB_DSN")
    if not dsn or not args.namespace or not args.stream:
        raise SystemExit("INFRA_DB_DSN, --namespace and --stream are required")
    snapshot = args.data_root / SNAPSHOT
    policy_dir = args.data_root / POLICY
    scope = pg.Scope(args.namespace, args.stream)
    report: dict = {"scope": list(scope.key), "list_from": args.list_from.isoformat()}
    con = duckdb.connect()
    con.execute("SET threads=4")
    started = time.perf_counter()
    with psycopg.connect(dsn) as connection:
        apply_migrations(connection)
        with connection.transaction():
            open_scope(connection, *scope.key)
            report["layout"] = load_layout(connection, con, snapshot)
            report["phase"] = load_phase(
                connection, con, snapshot, policy_rules(policy_dir), *scope.key
            )
            report["coverage_days"] = load_coverage(connection, con, snapshot, policy_dir, scope)
        report["load_seconds"] = round(time.perf_counter() - started, 1)
        started = time.perf_counter()
        result = fp.recompute(
            connection,
            data_as_of=report["phase"]["last"],
            import_id="seed:journal-curated-v4",
            namespace_id=scope.namespace_id,
            snapshot_id=scope.snapshot_id,
            list_from=args.list_from,
        )
        connection.commit()
        report["recompute_seconds"] = round(time.perf_counter() - started, 1)
        report["recompute"] = {
            "generation": result.generation,
            "cutoffs": result.cutoffs,
            "cards_issued": len(result.new_card_ids),
            "records_read": result.records_read,
            "timings": {k: round(v, 2) for k, v in result.timings.items()},
        }
        if args.synthetic_day:
            with connection.transaction():
                rows = synthetic_day(
                    connection, scope, args.synthetic_date, args.synthetic_rows, 17
                )
            end = datetime.combine(args.synthetic_date, datetime.max.time()).replace(tzinfo=MSK)
            started = time.perf_counter()
            day = fp.recompute(
                connection,
                data_as_of=end - timedelta(seconds=1),
                import_id="synthetic-day",
                namespace_id=scope.namespace_id,
                snapshot_id=scope.snapshot_id,
            )
            connection.commit()
            report["synthetic_day"] = {
                "date": args.synthetic_date.isoformat(),
                "rows": rows,
                "recompute_seconds": round(time.perf_counter() - started, 2),
                "generation": day.generation,
                "cutoffs": day.cutoffs,
                "new_cards": len(day.new_card_ids),
                "released": len(day.released_card_ids),
                "timings": {k: round(v, 3) for k, v in day.timings.items()},
            }
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
