"""Compare two complete publications of the alarm-spike research overlay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def run(first: Path, second: Path, output: Path) -> dict:
    a = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    b = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    if (
        a["status"] != "accepted_research_overlay_not_training_approval"
        or b["status"] != a["status"]
    ):
        raise ValueError("both overlays must be published")
    paths = a["output_sha256"].keys() | b["output_sha256"].keys()
    differences = sorted(
        path for path in paths if a["output_sha256"].get(path) != b["output_sha256"].get(path)
    )
    result = {
        "policy_version": a["policy_version"],
        "source_manifest_sha256_identical": a["base_manifest_sha256"] == b["base_manifest_sha256"],
        "code_sha256_identical": a["code_sha256"] == b["code_sha256"],
        "rules_identical": a["rules"] == b["rules"],
        "counts_identical": (
            a["excluded_curated_rows"] == b["excluded_curated_rows"]
            and a["working_curated_rows"] == b["working_curated_rows"]
            and a["by_rule_month"] == b["by_rule_month"]
        ),
        "output_files_compared": len(paths),
        "all_output_hashes_identical": not differences,
        "differing_output_paths": differences,
        "first_runtime": a["runtime"],
        "second_runtime": b["runtime"],
    }
    if not all(
        result[key]
        for key in (
            "source_manifest_sha256_identical",
            "code_sha256_identical",
            "rules_identical",
            "counts_identical",
            "all_output_hashes_identical",
        )
    ):
        raise ValueError("overlay repeats differ")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
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
                "output_files_compared": result["output_files_compared"],
                "all_output_hashes_identical": result["all_output_hashes_identical"],
            }
        )
    )


if __name__ == "__main__":
    main()
