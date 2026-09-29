"""Stream 7z CSV records into an auditable, year/month Parquet snapshot.

The input is never modified. Run one or more whole archive years; a run that omits
an archive does not claim accounting for it. A failed run leaves only a .partial
folder and never publishes a snapshot. See docs/DATA.md and
data-science/experiments/curated-etl/README.md.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import resource
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

# The long SQL projections remain aligned with the normative source schema.
# ruff: noqa: E501

ROOT = Path(__file__).resolve().parents[2]
HEADER = ["ид_события", "ид_канала_данных", "дата", "время", "тревожное", "значение_датчика"]
ETL_VERSION = "journal-curated-v4"
MAPPING_VERSION = "raw-dot-strict-monthly-rare10-epoch-payload-v3"
POLICIES = {
    "exclude-2021-v1": ("2021-01-01", "2022-01-01"),
    "exclude-2021-apr-jun-v1": ("2021-04-01", "2021-07-01"),
}
SPLITS = {
    "development_candidate": ["2019-01-01", "2025-01-01"],
    "validation": ["2025-01-01", "2025-07-01"],
    "test_2025": ["2025-07-01", "2026-01-01"],
    "test_2026": ["2026-01-01", "2026-06-01"],
    "diagnostic_gap": ["2026-06-01", "2026-07-01"],
}
RARE_LEXEME_COUNT_THRESHOLD = 10
TIMEZONE = "Europe/Moscow (assumed; UTC=local-03:00 for 2019-2026)"
EXPECTED = {
    2019: 20993328,
    2020: 28172398,
    2021: 53698948,
    2022: 43483149,
    2023: 31937125,
    2024: 49023883,
    2025: 55541796,
    2026: 30695389,
}
NUMERIC = re.compile(r"[-+]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")
PICKET = re.compile(r"ПК\s*(\d+(?:[,.]\d+)?)(?:\s*[-–]\s*(\d+(?:[,.]\d+)?))?", re.I)
TAG = re.compile(r"^(\d+)-([^.]+)\.([^.]+)\.")
STAGE_SCHEMA = pa.schema(
    [
        ("source_file", pa.string()),
        ("source_sha256", pa.string()),
        ("record_ordinal", pa.int64()),
        ("event_id_raw", pa.string()),
        ("channel_id_raw", pa.string()),
        ("date_raw", pa.string()),
        ("time_raw", pa.string()),
        ("alarm_raw", pa.string()),
        ("value_raw", pa.string()),
        ("column_count", pa.int32()),
        ("raw_cells_json", pa.string()),
    ]
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(path: Path) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256(path),
    }


def picket(name: str) -> dict:
    match = PICKET.search(name)
    if match is None:
        return {
            "picket_from": None,
            "picket_to": None,
            "picket_form": None,
            "picket_parsed": False,
            "picket_fractional": False,
            "picket_reversed": False,
        }
    first = float(match[1].replace(",", "."))
    second = float(match[2].replace(",", ".")) if match[2] else first
    return {
        "picket_from": min(first, second),
        "picket_to": max(first, second),
        "picket_form": "range" if match[2] else "point",
        "picket_parsed": True,
        "picket_fractional": first % 1 != 0 or second % 1 != 0,
        "picket_reversed": second < first,
    }


def reference(source: Path, partial: Path) -> tuple[dict, dict]:
    channel_path = source / "справочник_каналов_датчиков.csv"
    object_path = source / "справочник_объектов_диспетчер.csv"
    with channel_path.open(encoding="utf-8-sig", newline="") as stream:
        channels = list(csv.DictReader(stream))
    with object_path.open(encoding="utf-8-sig", newline="") as stream:
        objects = list(csv.DictReader(stream))
    if len(channels) != len({r["ид_канала_данных"] for r in channels}):
        raise ValueError("channel reference is not many-to-one")
    object_ids = {r["ид_объект"] for r in objects}
    if len(object_ids) != len(objects):
        raise ValueError("object reference has duplicate IDs")
    rows = []
    for row in channels:
        match = TAG.match(row["тег_инженерной_системы"])
        rows.append(
            {
                "channel_id": int(row["ид_канала_данных"]),
                "sensor_type": row["тип_датчика"],
                "system_type": row["тип_инж_системы"],
                "object_id": int(row["ид_объект"]) if row["ид_объект"] else None,
                "tag_raw": row["тег_инженерной_системы"],
                "tag_l1": match[1] if match else None,
                "tag_l2": match[2] if match else None,
                "tag_l3": match[3] if match else None,
                "name_raw": row["название_датчика"],
                **picket(row["название_датчика"]),
            }
        )
        if row["ид_объект"] not in object_ids:
            raise ValueError("channel references unknown object")
    pq.write_table(pa.Table.from_pylist(rows), partial / "channels.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(objects), partial / "objects.parquet", compression="zstd")
    return {"channels": identity(channel_path), "objects": identity(object_path)}, {
        "channels": len(channels),
        "objects_total": len(objects),
        "objects_referenced_by_channels": len({row["object_id"] for row in rows}),
        "picket_parsed": sum(r["picket_parsed"] for r in rows),
    }


@contextmanager
def archive(path: Path):
    member = path.stem + ".csv"
    process = subprocess.Popen(["bsdtar", "-xOf", str(path), member], stdout=subprocess.PIPE)
    try:
        assert process.stdout is not None
        import io

        with io.TextIOWrapper(process.stdout, encoding="utf-8-sig", newline="") as stream:
            yield stream
        if process.wait() != 0:
            raise RuntimeError(f"archive stream failed: {path.name}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def flush_stage(base: Path, bucket: str, index: int, rows: list[dict]) -> None:
    folder = base / bucket
    folder.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=STAGE_SCHEMA)
    pq.write_table(table, folder / f"batch-{index:06d}.parquet", compression="zstd")


def scan_archives(
    source: Path, years: list[int], partial: Path, batch_size: int
) -> tuple[dict, list[str], dict[str, int]]:
    staging = partial / "staging"
    buffers: dict[str, list[dict]] = defaultdict(list)
    batches = Counter()
    counts = {}
    date_counts = Counter()
    for year in years:
        path = source / f"ext-journal-{year}.7z"
        meta = identity(path)
        n = 0
        technical = 0
        malformed = 0
        with archive(path) as stream:
            reader = csv.reader(stream, strict=True)
            header = next(reader)
            if header != HEADER:
                raise ValueError(f"six-column source header mismatch: {path.name}")
            try:
                for ordinal, cells in enumerate(reader, 1):
                    n += 1
                    if cells == HEADER:
                        technical += 1
                        continue
                    column_count = len(cells)
                    raw_cells_json = json.dumps(cells, ensure_ascii=False)
                    if column_count != 6:
                        malformed += 1
                        cells = (cells + [""] * 6)[:6]
                        bucket = "invalid"
                    else:
                        date = cells[2]
                        bucket = date[:7] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else "invalid"
                        if bucket != "invalid":
                            date_counts[date] += 1
                    record = dict(
                        zip(
                            (
                                "event_id_raw",
                                "channel_id_raw",
                                "date_raw",
                                "time_raw",
                                "alarm_raw",
                                "value_raw",
                            ),
                            cells,
                            strict=True,
                        )
                    )
                    record.update(
                        source_file=path.name,
                        source_sha256=meta["sha256"],
                        record_ordinal=ordinal,
                        column_count=column_count,
                        raw_cells_json=raw_cells_json,
                    )
                    buffers[bucket].append(record)
                    if len(buffers[bucket]) >= batch_size:
                        flush_stage(staging, bucket, batches[bucket], buffers[bucket])
                        batches[bucket] += 1
                        buffers[bucket].clear()
            except (csv.Error, UnicodeError) as error:
                raise RuntimeError(
                    f"CSV boundary undecidable: {path.name}, record {n + 1}"
                ) from error
        counts[str(year)] = {
            **meta,
            "N_input_records": n,
            "N_technical_headers": technical,
            "N_quarantine_shape": malformed,
            "expected_event_records": EXPECTED[year],
        }
        # Published control figures count events. DATA-03 N_input_records also
        # includes an exact repeated technical header (one in 2025).
        if n - technical != EXPECTED[year]:
            raise ValueError(
                f"raw event count mismatch for {year}: {n} - {technical} != {EXPECTED[year]}"
            )
    for bucket, rows in buffers.items():
        if rows:
            flush_stage(staging, bucket, batches[bucket], rows)
    return counts, sorted(set(batches) | set(buffers)), dict(sorted(date_counts.items()))


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_coverage(
    date_counts: dict[str, int],
    partial: Path,
    years: list[int],
    policy: str = "exclude-2021-v1",
) -> dict:
    """Full-source calendar, with omitted archive years kept outside the scope."""
    rows = []
    uncovered = set()
    exclusion_start, exclusion_end = map(dt.date.fromisoformat, POLICIES[policy])
    for year in years:
        present = []
        for literal in date_counts:
            try:
                day = dt.date.fromisoformat(literal)
            except ValueError:
                continue
            if day.year == year:
                present.append(day)
        if not present:
            raise ValueError(f"no parseable source date for {year}")
        first, last = min(present), max(present)
        for offset in range((last - first).days + 1):
            day = first + dt.timedelta(days=offset)
            count = date_counts.get(day.isoformat(), 0)
            if not count:
                uncovered.add(day)
            rows.append(
                {
                    "day": day,
                    "source_records": count,
                    "source_covered": bool(count),
                    "policy_excluded": exclusion_start <= day < exclusion_end,
                    "working_covered": bool(count) and not (exclusion_start <= day < exclusion_end),
                    "sequence_segment": (
                        "pre-2021"
                        if day < exclusion_start
                        else "post-2021"
                        if day >= exclusion_end
                        else "excluded-2021"
                    ),
                }
            )
    pq.write_table(pa.Table.from_pylist(rows), partial / "coverage.parquet", compression="zstd")
    period_start, period_end = dt.date(2023, 1, 1), dt.date(2026, 6, 30)
    horizon_gaps = sum(period_start <= day <= period_end for day in uncovered)
    lookback_gap_dates = 0
    for offset in range((period_end - period_start).days + 1):
        day = period_start + dt.timedelta(days=offset)
        if any(day - dt.timedelta(days=back) in uncovered for back in range(1, 31)):
            lookback_gap_dates += 1
    return {
        "n_uncovered_dates": len(uncovered),
        "n_windows_gap": horizon_gaps,
        "n_cutoffs_lookback_gap": lookback_gap_dates,
        "gap_counter_unit": "calendar dates in 2023-01-01..2026-06-30; channel-day impact is computed by F1 audit",
        "calendar_rows": len(rows),
    }


def event_id_duplicates(con: duckdb.DuckDBPyConnection, partial: Path) -> list[dict]:
    """Exact accepted-record ID reuse by source archive and sensor family."""
    curated = sql_quote(str(partial / "curated/month=*/events.parquet"))
    dedup = sql_quote(str(partial / "dedup/*.parquet"))
    rows = con.execute(f"""SELECT source_file,sensor_type,
        count(*) - count(DISTINCT event_id) n_event_id_duplicates
        FROM (
            SELECT source_file,sensor_type,event_id FROM read_parquet({curated})
            UNION ALL
            SELECT source_file,sensor_type,event_id FROM read_parquet({dedup})
        ) GROUP BY 1,2 ORDER BY 1,2""").fetchall()
    return [
        {"source_file": name, "sensor_type": sensor_type, "n_event_id_duplicates": duplicate_count}
        for name, sensor_type, duplicate_count in rows
    ]


def process_bucket(
    con: duckdb.DuckDBPyConnection,
    partial: Path,
    bucket: str,
    ingested_at: str,
    reference_version: str,
    policy: str = "exclude-2021-v1",
) -> dict:
    files = str(partial / "staging" / bucket / "batch-*.parquet")
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW stage AS SELECT * FROM read_parquet({sql_quote(files)})"
    )
    con.execute("""CREATE OR REPLACE TEMP VIEW typed AS SELECT *,
        TRY_CAST(event_id_raw AS BIGINT) event_id,
        TRY_CAST(channel_id_raw AS BIGINT) channel_id,
        TRY_CAST(date_raw || ' ' || time_raw AS TIMESTAMP) event_ts_local,
        CASE WHEN alarm_raw IN ('t','true') THEN true
             WHEN alarm_raw IN ('f','false') THEN false ELSE NULL END alarm,
        CASE WHEN regexp_full_match(value_raw, '[-+]?(?:[0-9]+(?:\\.[0-9]+)?|\\.[0-9]+)')
             THEN TRY_CAST(value_raw AS DOUBLE) ELSE NULL END value_numeric,
        CASE WHEN column_count <> 6 THEN 'shape_invalid'
             WHEN event_id_raw IS NULL OR TRY_CAST(event_id_raw AS BIGINT) IS NULL THEN 'event_id_invalid'
             WHEN channel_id_raw IS NULL OR TRY_CAST(channel_id_raw AS BIGINT) IS NULL THEN 'channel_id_invalid'
             WHEN TRY_CAST(date_raw || ' ' || time_raw AS TIMESTAMP) IS NULL THEN 'time_invalid'
             WHEN alarm_raw NOT IN ('t','f','true','false') THEN 'alarm_invalid'
             ELSE NULL END quarantine_reason
        FROM stage""")
    n_input = con.execute("SELECT count(*) FROM stage").fetchone()[0]
    exclusion_start, exclusion_end = POLICIES[policy]
    scope_sql = (
        f"COALESCE(TRY_CAST(date_raw AS DATE) >= DATE '{exclusion_start}' "
        f"AND TRY_CAST(date_raw AS DATE) < DATE '{exclusion_end}', false)"
    )
    out_of_scope = con.execute(f"SELECT count(*) FROM typed WHERE {scope_sql}").fetchone()[0]
    reasons = dict(
        con.execute(
            f"SELECT quarantine_reason, count(*) FROM typed WHERE quarantine_reason IS NOT NULL AND NOT ({scope_sql}) GROUP BY 1"
        ).fetchall()
    )
    quarantined = sum(reasons.values())
    qdir = partial / "quarantine"
    qdir.mkdir(exist_ok=True)
    con.execute(
        f"COPY (SELECT source_file, source_sha256, record_ordinal, quarantine_reason, raw_cells_json FROM typed WHERE quarantine_reason IS NOT NULL AND NOT ({scope_sql}) ORDER BY source_file, record_ordinal) TO {sql_quote(str(qdir / (bucket + '.parquet')))} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    edir = partial / "excluded"
    edir.mkdir(exist_ok=True)
    exclusion_reason = "excluded_2021" if policy == "exclude-2021-v1" else "excluded_2021_apr_jun"
    con.execute(
        f"COPY (SELECT source_file,source_sha256,record_ordinal,{sql_quote(exclusion_reason)} reason,{sql_quote(policy)} exclusion_policy_version,raw_cells_json FROM typed WHERE {scope_sql} ORDER BY source_file,record_ordinal) TO {sql_quote(str(edir / (bucket + '.parquet')))} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    con.execute(f"""CREATE OR REPLACE TEMP VIEW valid AS SELECT t.*,
        t.event_ts_local + INTERVAL '3 hours' * -1 AS event_ts_utc,
        c.sensor_type, c.system_type, c.object_id, c.tag_l1, c.tag_l2, c.tag_l3,
        c.picket_from, c.picket_to, c.picket_form, c.picket_parsed,
        c.picket_fractional, c.picket_reversed,
        CASE WHEN c.channel_id IS NULL THEN 'unmatched' ELSE 'matched' END reference_status,
        CASE WHEN c.channel_id IS NULL THEN 'unknown' WHEN c.sensor_type = 'Газовый датчик'
             THEN 'gas' ELSE 'other' END cohort_membership,
        (t.event_ts_local IN (TIMESTAMP '1970-01-01 03:00:00', TIMESTAMP '1970-01-01 03:00:01')
         OR t.value_raw IN ('01.01.1970 03:00:00', '01.01.1970 03:00:01')) is_epoch_placeholder,
        sha256(t.source_sha256 || ':' || t.record_ordinal::VARCHAR) row_uid
        FROM typed t LEFT JOIN read_parquet({sql_quote(str(partial / "channels.parquet"))}) c
            ON t.channel_id = c.channel_id
        WHERE t.quarantine_reason IS NULL AND NOT ({scope_sql})""")
    accepted = n_input - quarantined - out_of_scope
    # An exact duplicate is only (channel, timestamp, raw value, alarm). Different
    # values at the same second remain independent records and are never ordered by ID.
    con.execute("DROP TABLE IF EXISTS grouped")
    con.execute("""CREATE TEMP TABLE grouped AS SELECT *,
        row_number() OVER (PARTITION BY channel_id,event_ts_local,value_raw,alarm ORDER BY source_file,record_ordinal) dedup_rank,
        first_value(row_uid) OVER (PARTITION BY channel_id,event_ts_local,value_raw,alarm ORDER BY source_file,record_ordinal) survivor_uid,
        count(*) OVER (PARTITION BY channel_id,event_ts_local) ts_group_size,
        count(DISTINCT value_raw) OVER (PARTITION BY channel_id,event_ts_local) ts_group_distinct_values,
        max(value_numeric) OVER (PARTITION BY channel_id,event_ts_local) companion_numeric
        FROM valid""")
    dedup = con.execute("SELECT count(*) FROM grouped WHERE dedup_rank > 1").fetchone()[0]
    ddir = partial / "dedup"
    ddir.mkdir(exist_ok=True)
    con.execute(
        f"COPY (SELECT source_file,source_sha256,record_ordinal,row_uid,survivor_uid,event_id,sensor_type,'exact_duplicate' reason FROM grouped WHERE dedup_rank > 1 ORDER BY source_file,record_ordinal) TO {sql_quote(str(ddir / (bucket + '.parquet')))} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    cdir = partial / "curated" / f"month={bucket}"
    cdir.mkdir(parents=True, exist_ok=True)
    con.execute(f"""COPY (SELECT row_uid,source_file,source_sha256,record_ordinal,event_id,channel_id,
        event_ts_utc,date_raw || ' ' || time_raw event_ts_local_raw,alarm,value_raw,
        value_numeric,value_numeric IS NOT NULL is_numeric,value_numeric IN (327.68,-127,0) sentinel_candidate,
        CASE WHEN value_numeric IS NULL THEN count(*) OVER (PARTITION BY sensor_type,value_raw) < {RARE_LEXEME_COUNT_THRESHOLD}
             ELSE false END rare_lexeme,
        CAST(NULL AS VARCHAR) state_code,
        CASE WHEN value_numeric IS NULL THEN value_raw ELSE NULL END state_lexeme,
        is_epoch_placeholder,ts_group_size,ts_group_distinct_values,companion_numeric,
        reference_status,cohort_membership,sensor_type,system_type,object_id,tag_l1,tag_l2,tag_l3,
        picket_from,picket_to,picket_form,picket_parsed,picket_fractional,picket_reversed,
        true source_covered,
        CASE WHEN event_ts_local < TIMESTAMP '{exclusion_start}' THEN 'pre-2021'
             WHEN event_ts_local >= TIMESTAMP '{exclusion_end}' THEN 'post-2021'
             ELSE 'excluded-2021' END sequence_segment,
        {sql_quote(policy)} exclusion_policy_version,
        false reference_historical_validity_known,
        '{ETL_VERSION}' etl_version,'{MAPPING_VERSION}' mapping_version,
        {sql_quote(reference_version)} reference_version,
        CAST({sql_quote(ingested_at)} AS TIMESTAMPTZ) ingested_at,
        CAST(NULL AS TIMESTAMPTZ) available_at
        FROM grouped WHERE dedup_rank = 1 ORDER BY source_file,record_ordinal)
        TO {sql_quote(str(cdir / "events.parquet"))} (FORMAT PARQUET, COMPRESSION ZSTD)""")
    metrics = con.execute("""SELECT count(*), count(*) FILTER (WHERE reference_status='unmatched'),
        count(*) FILTER (WHERE is_epoch_placeholder),
        count(*) FILTER (WHERE value_numeric IS NOT NULL),
        count(*) FILTER (WHERE value_raw LIKE '%,%' AND value_numeric IS NULL),
        count(DISTINCT (channel_id,event_ts_local)),
        count(DISTINCT (channel_id,event_ts_local)) FILTER (WHERE ts_group_size>1),
        count(DISTINCT (channel_id,event_ts_local)) FILTER (WHERE ts_group_distinct_values>1),
        count(*) FILTER (WHERE value_raw='Обнаружен газ' AND ts_group_distinct_values>1),
        count(*) FILTER (WHERE value_raw=''),
        count(DISTINCT channel_id) FILTER (WHERE reference_status='unmatched'),
        count(*) FILTER (WHERE regexp_full_match(value_raw, '[-+]?(?:[0-9]+(?:,[0-9]+)?|,[0-9]+)') AND value_raw LIKE '%,%')
        FROM grouped WHERE dedup_rank=1""").fetchone()
    lexeme_dir = partial / "lexemes"
    lexeme_dir.mkdir(exist_ok=True)
    con.execute(
        f"""COPY (SELECT source_file,sensor_type,value_raw lexeme,count(*) records
        FROM grouped WHERE dedup_rank=1 AND value_numeric IS NULL
        GROUP BY 1,2,3 ORDER BY 1,2,3)
        TO {sql_quote(str(lexeme_dir / (bucket + ".parquet")))}
        (FORMAT PARQUET, COMPRESSION ZSTD)"""
    )
    source_rows = con.execute(
        """SELECT source_file, count(*) N_accepted,
        count(*) FILTER (WHERE dedup_rank=1) N_curated,
        count(*) FILTER (WHERE dedup_rank>1) N_dedup_removed
        FROM grouped GROUP BY source_file ORDER BY source_file"""
    ).fetchall()
    event_id_audit = [
        {
            "source_file": name,
            "sensor_type": sensor_type,
            "n_event_id_duplicates_within_month": n - distinct_n,
        }
        for name, sensor_type, n, distinct_n in con.execute(
            """SELECT source_file,sensor_type,count(*),count(DISTINCT event_id)
            FROM grouped GROUP BY 1,2 ORDER BY 1,2"""
        ).fetchall()
    ]
    quarantine_rows = dict(
        con.execute(
            f"SELECT source_file,count(*) FROM typed WHERE quarantine_reason IS NOT NULL AND NOT ({scope_sql}) GROUP BY source_file"
        ).fetchall()
    )
    excluded_rows = dict(
        con.execute(
            f"SELECT source_file,count(*) FROM typed WHERE {scope_sql} GROUP BY source_file"
        ).fetchall()
    )
    per_source = {
        name: {
            "N_accepted": a,
            "N_curated": c,
            "N_dedup_removed": d,
            "N_quarantine": quarantine_rows.pop(name, 0),
            "N_out_of_scope": excluded_rows.pop(name, 0),
        }
        for name, a, c, d in source_rows
    }
    for name, count in quarantine_rows.items():
        per_source[name] = {
            "N_accepted": 0,
            "N_curated": 0,
            "N_dedup_removed": 0,
            "N_quarantine": count,
            "N_out_of_scope": excluded_rows.pop(name, 0),
        }
    for name, count in excluded_rows.items():
        per_source[name] = {
            "N_accepted": 0,
            "N_curated": 0,
            "N_dedup_removed": 0,
            "N_quarantine": 0,
            "N_out_of_scope": count,
        }
    # A comma-bearing value may be text; publish a candidate count and do not parse it.
    result = {
        "N_input_records": n_input,
        "N_accepted": accepted,
        "N_quarantine": quarantined,
        "N_technical_headers": 0,
        "N_out_of_scope": out_of_scope,
        "N_curated": metrics[0],
        "N_dedup_removed": dedup,
        "quarantine_reasons": reasons,
        "n_unmatched_rows": metrics[1],
        "n_cohort_membership_unknown": metrics[1],
        "n_epoch_placeholder": metrics[2],
        "n_numeric_dot": metrics[3],
        "n_comma_candidate": metrics[4],
        "n_ts_groups": metrics[5],
        "n_ts_groups_multi_row": metrics[6],
        "n_ts_groups_multi_value": metrics[7],
        "n_target_messages_in_multi_value_groups": metrics[8],
        "n_value_null": metrics[9],
        "n_unmatched_channels": metrics[10],
        "n_numeric_comma_candidate": metrics[11],
        "by_source": per_source,
        "event_id_audit": event_id_audit,
    }
    assert n_input == accepted + quarantined + out_of_scope
    assert accepted == metrics[0] + dedup
    return result


def run(
    source: Path,
    output: Path,
    years: list[int],
    ingested_at: str,
    batch_size: int,
    policy: str = "exclude-2021-v1",
    maintenance_manifest: Path | None = None,
) -> dict:
    start = time.monotonic()
    if output.exists() or output.with_name(output.name + ".partial").exists():
        raise FileExistsError("snapshot or partial already exists; no overwrite")
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("output must be outside raw source")
    partial = output.with_name(output.name + ".partial")
    partial.mkdir(parents=True)
    try:
        references, ref_counts = reference(source, partial)
        source_counts, buckets, date_counts = scan_archives(source, years, partial, batch_size)
        con = duckdb.connect()
        con.execute("SET threads=2")
        con.execute("SET memory_limit='4GB'")
        con.execute(f"SET temp_directory={sql_quote(str(partial / 'spill'))}")
        by_bucket = {}
        for bucket in buckets:
            by_bucket[bucket] = process_bucket(
                con, partial, bucket, ingested_at, references["channels"]["sha256"], policy
            )
        if sum(month["n_numeric_comma_candidate"] for month in by_bucket.values()):
            raise ValueError("numeric comma values require a new explicit mapping_version")
        # A global DISTINCT over ~300M IDs exceeds the bounded 4 GB DuckDB
        # memory budget. Exact content deduplication above is the DATA-03 gate;
        # ID reuse is an auxiliary diagnostic, counted inside each month.
        monthly_event_id_duplicates = [
            {"month": month, **item}
            for month, result in by_bucket.items()
            for item in result["event_id_audit"]
        ]
        con.close()
        totals = {
            key: sum(bucket[key] for bucket in by_bucket.values())
            for key in (
                "N_input_records",
                "N_accepted",
                "N_quarantine",
                "N_technical_headers",
                "N_out_of_scope",
                "N_curated",
                "N_dedup_removed",
            )
        }
        totals["N_input_records"] += sum(m["N_technical_headers"] for m in source_counts.values())
        totals["N_technical_headers"] += sum(
            m["N_technical_headers"] for m in source_counts.values()
        )
        assert (
            totals["N_input_records"]
            == totals["N_accepted"]
            + totals["N_quarantine"]
            + totals["N_technical_headers"]
            + totals["N_out_of_scope"]
        )
        assert totals["N_accepted"] == totals["N_curated"] + totals["N_dedup_removed"]
        assert totals["N_input_records"] == sum(
            m["N_input_records"] for m in source_counts.values()
        )
        for year, source in source_counts.items():
            name = f"ext-journal-{year}.7z"
            parts = [month["by_source"].get(name, {}) for month in by_bucket.values()]
            for key in (
                "N_accepted",
                "N_curated",
                "N_dedup_removed",
                "N_quarantine",
                "N_out_of_scope",
            ):
                source[key] = sum(part.get(key, 0) for part in parts)
            assert source["N_input_records"] == (
                source["N_accepted"]
                + source["N_quarantine"]
                + source["N_technical_headers"]
                + source["N_out_of_scope"]
            )
            assert source["N_accepted"] == source["N_curated"] + source["N_dedup_removed"]
        # Raw staging is an implementation detail; all survivors and rejects have
        # source hash and logical ordinal in the published files.
        shutil.rmtree(partial / "staging")
        coverage = build_coverage(date_counts, partial, years, policy)
        output_hashes = {
            path.relative_to(partial).as_posix(): sha256(path)
            for folder in ("curated", "dedup", "quarantine", "excluded", "lexemes")
            for path in sorted((partial / folder).rglob("*.parquet"))
        }
        output_hashes["coverage.parquet"] = sha256(partial / "coverage.parquet")
        output_hashes["channels.parquet"] = sha256(partial / "channels.parquet")
        output_hashes["objects.parquet"] = sha256(partial / "objects.parquet")
        report = {
            "etl_version": ETL_VERSION,
            "mapping_version": MAPPING_VERSION,
            "timezone_assumption": TIMEZONE,
            "years": years,
            "exclusion_policy": {"version": policy, "interval": list(POLICIES[policy])},
            "split_reservations": SPLITS,
            "maintenance": (
                json.loads(maintenance_manifest.read_text(encoding="utf-8"))
                if maintenance_manifest is not None
                else None
            ),
            "ingested_at": ingested_at,
            "reference": references,
            "reference_counts": ref_counts,
            "sources": source_counts,
            "by_month": by_bucket,
            "event_id_duplicates_by_source_type": None,
            "event_id_duplicates_within_month_by_source_type": monthly_event_id_duplicates,
            "totals": totals,
            "coverage": coverage,
            "rare_lexeme_policy": {
                "threshold": RARE_LEXEME_COUNT_THRESHOLD,
                "unit": "surviving records per sensor type × literal in each event month",
                "effect": "flag only; no text record removed",
            },
            "output_sha256": output_hashes,
            "code_sha256": sha256(Path(__file__)),
            "config_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "years": years,
                        "exclusion_policy": policy,
                        "split_reservations": SPLITS,
                        "mapping": MAPPING_VERSION,
                        "timezone": TIMEZONE,
                        "ingested_at": ingested_at,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            "git_sha": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "git_dirty": bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=ROOT, text=True
                ).strip()
            ),
            "runtime": {
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "peak_rss_mb": round(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2, 1
                ),
            },
            "models_fitted": 0,
            "publication_status": "accepted",
        }
        (partial / "manifest.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        partial.rename(output)
        return report
    except Exception:
        (partial / "FAILED").write_text("Import incomplete; no accepted publication.\n")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--years", nargs="+", type=int, default=[2019, 2020, 2022, 2023, 2024, 2025, 2026]
    )
    parser.add_argument("--policy", choices=sorted(POLICIES), default="exclude-2021-v1")
    parser.add_argument("--maintenance-manifest", type=Path)
    parser.add_argument(
        "--ingested-at", required=True, help="Pinned UTC import time for bytewise reruns"
    )
    parser.add_argument("--batch-size", type=int, default=100000)
    args = parser.parse_args()
    if not set(args.years) <= EXPECTED.keys() or len(set(args.years)) != len(args.years):
        parser.error("years must be distinct known archive years")
    dt.datetime.fromisoformat(args.ingested_at.replace("Z", "+00:00"))
    report = run(
        args.source,
        args.output,
        sorted(args.years),
        args.ingested_at,
        args.batch_size,
        args.policy,
        args.maintenance_manifest,
    )
    print(
        json.dumps({"totals": report["totals"], "runtime": report["runtime"]}, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
