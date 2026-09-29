"""Verify byte-identical curated files and normalized manifests across two runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(first: Path, second: Path, output: Path) -> dict:
    started = time.monotonic()
    a = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    b = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    if a["publication_status"] != b["publication_status"] or a["publication_status"] != "accepted":
        raise ValueError("both snapshots must be accepted")
    left, right = dict(a), dict(b)
    left.pop("runtime")
    right.pop("runtime")
    equal_manifest = left == right
    equal_files = a["output_sha256"] == b["output_sha256"]
    for folder, manifest in ((first, a), (second, b)):
        for relative, expected in manifest["output_sha256"].items():
            if sha(folder / relative) != expected:
                raise ValueError("output file failed its own manifest hash")
    report = {
        "version": "curated-repro-v1",
        "models_fitted": 0,
        "byte_identical_outputs": equal_files,
        "normalized_manifest_identical": equal_manifest,
        "files_checked_per_run": len(a["output_sha256"]),
        "dataset_input_hashes_identical": a["sources"] == b["sources"]
        and a["reference"] == b["reference"],
        "config_hash_identical": a["config_sha256"] == b["config_sha256"],
        "code_hash_identical": a["code_sha256"] == b["code_sha256"],
        "first_manifest_sha256": sha(first / "manifest.json"),
        "second_manifest_sha256": sha(second / "manifest.json"),
        "dataset_sha256": hashlib.sha256(
            json.dumps(a["sources"], sort_keys=True).encode()
        ).hexdigest(),
        "config_sha256": a["config_sha256"],
        "etl_code_sha256": a["code_sha256"],
        "code_sha256": sha(Path(__file__)),
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        ),
        "runtime": {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2, 1),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    if not equal_manifest or not equal_files:
        raise AssertionError("curated repeat is not byte identical; inspect the aggregate report")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.first, args.second, args.output)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "byte_identical_outputs",
                    "normalized_manifest_identical",
                    "files_checked_per_run",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
