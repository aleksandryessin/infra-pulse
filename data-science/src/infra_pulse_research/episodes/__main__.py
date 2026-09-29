"""Local extraction, audit and resumable training; existing MLflow server optional."""

import argparse
from pathlib import Path

from infra_pulse_research.episodes.data import extract
from infra_pulse_research.episodes.runner import load_config, run
from infra_pulse_research.sequence.io import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "train", "audit"])
    parser.add_argument(
        "--config", type=Path, default=ROOT / "data-science/configs/episode_research_v1.json"
    )
    parser.add_argument(
        "--prepared", type=Path, default=ROOT / "data-science/artifacts/episodes-v1/prepared"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data-science/artifacts/episodes-v1/run"
    )
    parser.add_argument("--tracking-uri")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "prepare":
        extract(config, args.prepared, resume=args.resume)
    elif args.command == "audit":
        from infra_pulse_research.episodes.audit import audit

        audit(config, args.prepared, args.output)
    else:
        run(config, args.prepared, args.output, tracking_uri=args.tracking_uri, resume=args.resume)


if __name__ == "__main__":
    main()
