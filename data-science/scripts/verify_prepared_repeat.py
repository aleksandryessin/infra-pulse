"""Compare the complete output-file hashes of two eight-year prepared snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def run(first: Path, second: Path, output: Path) -> dict:
    a = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    b = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    if a["publication_status"] != "accepted" or b["publication_status"] != "accepted":
        raise ValueError("both snapshots must be atomically published")
    files_a, files_b = a["output_sha256"], b["output_sha256"]
    differences = sorted(
        key for key in files_a.keys() | files_b.keys() if files_a.get(key) != files_b.get(key)
    )
    source_equal = {
        year: a["sources"][year]["sha256"] == b["sources"][year]["sha256"]
        for year in a["sources"].keys() | b["sources"].keys()
        if year in a["sources"] and year in b["sources"]
    }
    result = {
        "first_etl_version": a["etl_version"],
        "second_etl_version": b["etl_version"],
        "first_code_sha256": a["code_sha256"],
        "second_code_sha256": b["code_sha256"],
        "first_recovered_after_auxiliary_oom": bool(a.get("publication_recovery")),
        "hashes_compared": len(files_a.keys() | files_b.keys()),
        "all_output_hashes_identical": not differences,
        "differing_output_paths": differences,
        "source_sha256_identical_by_year": source_equal,
        "same_source_years": set(a["sources"]) == set(b["sources"]),
        "same_totals": a["totals"] == b["totals"],
        "same_policy": a["exclusion_policy"] == b["exclusion_policy"],
        "same_reference": a["reference"] == b["reference"],
        "same_coverage": a["coverage"] == b["coverage"],
        "first_runtime": a["runtime"],
        "second_runtime": b["runtime"],
        "interpretation": (
            "Bytewise output comparison across two ETL executions. Metadata and "
            "code hashes differ because the first execution was finalized after "
            "an auxiliary global-ID OOM; no model was trained."
        ),
    }
    if not result["all_output_hashes_identical"] or not all(source_equal.values()):
        raise ValueError("prepared output or source hashes differ")
    if not all(
        result[key]
        for key in (
            "same_source_years",
            "same_totals",
            "same_policy",
            "same_reference",
            "same_coverage",
        )
    ):
        raise ValueError("prepared manifest accounting differs")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.first, args.second, args.output)
    print(
        json.dumps(
            {
                "hashes_compared": result["hashes_compared"],
                "all_output_hashes_identical": result["all_output_hashes_identical"],
                "second_runtime": result["second_runtime"],
            }
        )
    )


if __name__ == "__main__":
    main()
