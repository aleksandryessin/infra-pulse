"""Latency of the observation API (B1x): POST -> terminal import status, synthetic data.

WARNING: only for a local or an empty copy, never for the stand with history. The
batches are dated from «now» (``datetime.now``): on a copy whose data end earlier (the
stand: 30.06.2026) the first batch moves the forecast time of the whole copy to today
for every user, and only a database restore takes it back (rehearsal 29.09.2026, P1-3).
The SSH tunnel to the stand is a loopback address too, so the address check below does
not protect the stand.

Posts synthetic batches to ``POST /api/v1/observations`` with an integration token and
polls ``GET /api/v1/observations/{id}`` every 20 ms until the batch is final:
``published`` (``--until published``, default: the forecast is recomputed, B2) or
``imported`` (``--until imported``: rows loaded, without waiting for the recompute), or
``duplicate`` / ``failed``. ``sequential`` waits for each batch before the next one
(latency of one batch); ``burst`` posts all batches first (queueing in the single
worker). Prints one JSON line: POST time, POST -> final p50/p95/max, stages.

Only loopback API addresses are accepted (a local run; see the warning above about
the tunnel). Channels ``--channel-from`` .. ``+--channels`` must be in the channel reference;
records are synthetic, dated in the past (Moscow time) and strictly increasing across
batches, like a live stream: a record older than its channel's last one would make the
detector of B2 rebuild the channel from history (``late_channels``). The token comes
from the JSON of ``python -m infra_pulse_backend.admin token create --json`` saved to a
file outside the repository; it is never printed.

    uv run --locked python backend/scripts/probe_observation_api.py \
      --base-url http://127.0.0.1:8000 --token-file /secure/tmp/token.json \
      --batches 20 --records 1000 --mode sequential

This measures the local ingest path, not the OPS-01 load profile (D08).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx

MSK = timezone(timedelta(hours=3))
TERMINAL = {"published", "failed", "duplicate"}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
GAP_MS = 2000


def make_batch(
    batch_id: str, records: int, seq: int, first: int, channels: int, base: datetime
) -> bytes:
    """Synthetic records: half with date+time (MSK), half with event_at (UTC).

    Record ``index`` of batch ``seq`` is ``base + seq * (records + GAP_MS) + index`` ms:
    times grow within and across batches, and two records of one channel are more than
    a second apart, so whole seconds of ``date`` + ``time`` never reorder them.
    """
    items = []
    for index in range(records):
        at = base + timedelta(milliseconds=seq * (records + GAP_MS) + index)
        alarm = index % 97 == 0
        record = {
            "event_id": f"probe-{seq}-{index}",
            "channel_id": str(first + (seq * 7919 + index) % channels),
            "alarm": alarm,
            "value": "Неисправен" if alarm else str(index % 40),
        }
        if index % 2:
            record["event_at"] = at.astimezone(UTC).isoformat()
        else:
            record["date"], record["time"] = f"{at:%Y-%m-%d}", f"{at:%H:%M:%S}"
        items.append(record)
    return json.dumps({"batch_id": batch_id, "records": items}, ensure_ascii=False).encode()


def wait_final(
    client: httpx.Client, import_id: str, started: float, timeout: float, final: set[str]
) -> tuple:
    while True:
        response = client.get(f"/api/v1/observations/{import_id}")
        response.raise_for_status()
        item = response.json()
        if item["status"] in final:
            return time.perf_counter() - started, item
        if time.perf_counter() - started > timeout:
            raise TimeoutError(f"{import_id} not final after {timeout} s")
        time.sleep(0.02)


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(p * len(ordered)) - 1)], 3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--batches", type=int, default=20)
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--mode", choices=["sequential", "burst"], default="sequential")
    parser.add_argument("--channel-from", type=int, default=800000)
    parser.add_argument("--channels", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--until",
        choices=["published", "imported"],
        default="published",
        help="final status to wait for (imported: without the forecast recompute)",
    )
    args = parser.parse_args()
    final = TERMINAL | ({"imported"} if args.until == "imported" else set())
    if urlparse(args.base_url).hostname not in LOOPBACK:
        raise SystemExit("only a loopback API address is accepted (use the SSH tunnel)")
    if not 1 <= args.records <= 5000 or not 1 <= args.batches <= 1000:
        raise SystemExit("records must be 1-5000 and batches 1-1000")
    token = json.loads(args.token_file.read_text(encoding="utf-8"))["token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    prefix = f"probe-{uuid4().hex[:8]}"
    span = timedelta(milliseconds=args.batches * (args.records + GAP_MS))
    base = datetime.now(MSK) - timedelta(seconds=5) - span
    bodies = [
        make_batch(f"{prefix}-{seq:04d}", args.records, seq, args.channel_from, args.channels, base)
        for seq in range(args.batches)
    ]
    post_times, latencies, reports, pending = [], [], [], []
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=60) as client:
        for seq, data in enumerate(bodies):
            started = time.perf_counter()
            response = client.post("/api/v1/observations", content=data)
            post_times.append(time.perf_counter() - started)
            if response.status_code != 202:
                raise SystemExit(f"batch {seq}: HTTP {response.status_code} {response.text[:300]}")
            if args.mode == "sequential":
                latency, item = wait_final(
                    client, response.json()["id"], started, args.timeout, final
                )
                latencies.append(latency)
                reports.append(item)
            else:
                pending.append((response.json()["id"], started))
        for import_id, started in pending:
            latency, item = wait_final(client, import_id, started, args.timeout, final)
            latencies.append(latency)
            reports.append(item)
    stages: dict[str, list[float]] = {}
    for item in reports:
        for timing in item["timings"]:
            stages.setdefault(timing["stage"], []).append(timing["seconds"])
    print(
        json.dumps(
            {
                "mode": args.mode,
                "until": args.until,
                "batches": args.batches,
                "records_per_batch": args.records,
                "body_bytes_max": max(len(body) for body in bodies),
                "statuses": sorted({item["status"] for item in reports}),
                "rows_accepted": sum(item["rows_accepted"] or 0 for item in reports),
                "rows_quarantined": sum(item["rows_quarantined"] or 0 for item in reports),
                "post_seconds": {
                    "p50": round(statistics.median(post_times), 3),
                    "max": round(max(post_times), 3),
                },
                "post_to_final_seconds": {
                    "p50": percentile(latencies, 0.5),
                    "p95": percentile(latencies, 0.95),
                    "max": round(max(latencies), 3),
                },
                "stage_seconds_p50": {
                    stage: round(statistics.median(values), 4) for stage, values in stages.items()
                },
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
