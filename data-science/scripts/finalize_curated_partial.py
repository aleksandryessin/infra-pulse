"""Recover a fully written monthly ETL after an auxiliary final audit failed.

This never trusts the presence of files alone: every persisted stage record is
reconciled with one curated, excluded, quarantine or dedup record by month and
archive. Only after all checks does it remove staging and publish atomically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import curate_journal as etl
import duckdb
import pyarrow.parquet as pq


def _rows(path: Path) -> int:
    if not path.exists():
        raise FileNotFoundError(path)
    return pq.ParquetFile(path).metadata.num_rows


def _stage_rows(folder: Path) -> int:
    files = sorted(folder.glob("batch-*.parquet"))
    if not files:
        raise ValueError(f"empty stage month: {folder.name}")
    return sum(_rows(file) for file in files)


def run(
    source: Path,
    partial: Path,
    output: Path,
    years: list[int],
    policy: str,
    maintenance_manifest: Path,
    ingested_at: str,
    generation_code_sha256: str,
) -> dict:
    start = time.monotonic()
    if output.exists() or not (partial / "FAILED").is_file():
        raise ValueError("expected failed partial and no published output")
    if set(years) != set(etl.EXPECTED):
        raise ValueError("recovery requires all eight archive years")
    folders = sorted((partial / "staging").glob("????-??"))
    if len(folders) != 90 or len(list((partial / "curated").glob("month=*"))) != 90:
        raise ValueError("expected all 90 monthly stage and curated partitions")
    with tempfile.TemporaryDirectory() as directory:
        references, ref_counts = etl.reference(source, Path(directory))
        for name in ("channels.parquet", "objects.parquet"):
            if etl.sha256(Path(directory) / name) != etl.sha256(partial / name):
                raise ValueError(f"reference output mismatch: {name}")
    sources = {}
    for year in years:
        meta = etl.identity(source / f"ext-journal-{year}.7z")
        sources[str(year)] = {
            **meta,
            "N_input_records": 0,
            "N_technical_headers": 1 if year == 2025 else 0,
            "expected_event_records": etl.EXPECTED[year],
            "N_quarantine_shape": 0,
            "N_accepted": 0,
            "N_curated": 0,
            "N_dedup_removed": 0,
            "N_quarantine": 0,
            "N_out_of_scope": 0,
        }
    by_month = {}
    for folder in folders:
        month = folder.name
        year = int(month[:4])
        name = f"ext-journal-{year}.7z"
        first = sorted(folder.glob("batch-*.parquet"))[0]
        batch = pq.read_table(first, columns=["source_file", "source_sha256"]).slice(0, 1)
        sample = batch.to_pylist()[0]
        if sample != {"source_file": name, "source_sha256": sources[str(year)]["sha256"]}:
            raise ValueError(f"stage source identity mismatch: {month}")
        n_stage = _stage_rows(folder)
        n_curated = _rows(partial / "curated" / f"month={month}" / "events.parquet")
        n_dedup = _rows(partial / "dedup" / f"{month}.parquet")
        n_excluded = _rows(partial / "excluded" / f"{month}.parquet")
        n_quarantine = _rows(partial / "quarantine" / f"{month}.parquet")
        n_accepted = n_curated + n_dedup
        if n_stage != n_accepted + n_quarantine + n_excluded:
            raise ValueError(f"DATA-03 month mismatch: {month}")
        counts = {
            "N_input_records": n_stage,
            "N_accepted": n_accepted,
            "N_quarantine": n_quarantine,
            "N_technical_headers": 0,
            "N_out_of_scope": n_excluded,
            "N_curated": n_curated,
            "N_dedup_removed": n_dedup,
        }
        by_month[month] = {**counts, "accounting_source": "persisted Parquet row-group metadata"}
        for key, value in counts.items():
            sources[str(year)][key] += value
    for year in years:
        item = sources[str(year)]
        item["N_input_records"] += item["N_technical_headers"]
        if item["N_input_records"] - item["N_technical_headers"] != etl.EXPECTED[year]:
            raise ValueError(f"archive count mismatch: {year}")
        if item["N_input_records"] != (
            item["N_accepted"]
            + item["N_quarantine"]
            + item["N_technical_headers"]
            + item["N_out_of_scope"]
        ):
            raise ValueError(f"DATA-03 archive mismatch: {year}")
        if item["N_accepted"] != item["N_curated"] + item["N_dedup_removed"]:
            raise ValueError(f"dedup archive mismatch: {year}")
    totals = {
        key: sum(item[key] for item in sources.values())
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
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='4GB'")
    con.execute(f"SET temp_directory={etl.sql_quote(str(partial / 'spill'))}")
    dates = con.execute(f"""
        SELECT date_raw, count(*)
        FROM read_parquet({etl.sql_quote(str(partial / "staging/*/batch-*.parquet"))})
        GROUP BY 1
    """).fetchall()
    con.close()
    date_counts = dict(dates)
    if sum(date_counts.values()) != totals["N_input_records"] - totals["N_technical_headers"]:
        raise ValueError("source date accounting mismatch")
    coverage = etl.build_coverage(date_counts, partial, years, policy)
    hashes = {
        path.relative_to(partial).as_posix(): etl.sha256(path)
        for folder in ("curated", "dedup", "quarantine", "excluded", "lexemes")
        for path in sorted((partial / folder).rglob("*.parquet"))
    }
    for name in ("coverage.parquet", "channels.parquet", "objects.parquet"):
        hashes[name] = etl.sha256(partial / name)
    maintenance = json.loads(maintenance_manifest.read_text(encoding="utf-8"))
    config = {
        "years": years,
        "exclusion_policy": policy,
        "split_reservations": etl.SPLITS,
        "mapping": etl.MAPPING_VERSION,
        "timezone": etl.TIMEZONE,
        "ingested_at": ingested_at,
    }
    report = {
        "etl_version": etl.ETL_VERSION,
        "mapping_version": etl.MAPPING_VERSION,
        "timezone_assumption": etl.TIMEZONE,
        "years": years,
        "exclusion_policy": {"version": policy, "interval": list(etl.POLICIES[policy])},
        "split_reservations": etl.SPLITS,
        "maintenance": maintenance,
        "ingested_at": ingested_at,
        "reference": references,
        "reference_counts": ref_counts,
        "sources": sources,
        "by_month": by_month,
        "event_id_duplicates_by_source_type": None,
        "event_id_duplicate_audit_status": (
            "global ID reuse not computed after 4 GB OOM; exact content duplicates accounted"
        ),
        "totals": totals,
        "coverage": coverage,
        "rare_lexeme_policy": {
            "threshold": etl.RARE_LEXEME_COUNT_THRESHOLD,
            "unit": "surviving records per sensor type × literal in each event month",
            "effect": "flag only; no text record removed",
        },
        "output_sha256": hashes,
        "code_sha256": generation_code_sha256,
        "finalizer_code_sha256": etl.sha256(Path(__file__)),
        "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=etl.ROOT, text=True
        ).strip(),
        "git_dirty": True,
        "runtime": {
            "recovery_elapsed_seconds": round(time.monotonic() - start, 3),
            "recovery_peak_rss_mb": round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2, 1
            ),
            "generation_peak_rss_mb": None,
            "generation_peak_rss_reason": "first process exited on auxiliary global-ID OOM",
        },
        "models_fitted": 0,
        "publication_status": "accepted",
        "publication_recovery": (
            "all monthly outputs reconciled to persisted stage; global ID audit omitted"
        ),
    }
    # Staging held the full raw payload and is unnecessary after every outcome
    # has been reconciled to one published category.
    shutil.rmtree(partial / "staging")
    (partial / "FAILED").unlink()
    (partial / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    partial.rename(output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--partial", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--years", nargs="+", type=int, required=True)
    parser.add_argument("--policy", choices=sorted(etl.POLICIES), required=True)
    parser.add_argument("--maintenance-manifest", type=Path, required=True)
    parser.add_argument("--ingested-at", required=True)
    parser.add_argument("--generation-code-sha256", required=True)
    args = parser.parse_args()
    report = run(
        args.source,
        args.partial,
        args.output,
        sorted(args.years),
        args.policy,
        args.maintenance_manifest,
        args.ingested_at,
        args.generation_code_sha256,
    )
    print(json.dumps({"totals": report["totals"], "runtime": report["runtime"]}))


if __name__ == "__main__":
    main()
