"""Bounded local read-path probe, not an OPS-01 SLA or production load test.

The probe prints aggregate timings only; it never prints object/channel IDs or raw values.
"""

import argparse
import asyncio
import json
import math
import time
from collections import Counter, defaultdict

import httpx


def percentile(values, p):
    ordered = sorted(values)
    rank = max(0, math.ceil(p * len(ordered)) - 1)
    return round(ordered[rank] * 1000, 1)


async def run(base_url, users, iterations, scenario="core"):
    pool_size = users * 3 + 5 if scenario == "queue-overview" else users + 5
    limits = httpx.Limits(max_connections=pool_size, max_keepalive_connections=pool_size)
    async with httpx.AsyncClient(base_url=base_url, timeout=20, limits=limits) as client:
        capability_response = await client.get("/api/v1/capabilities")
        capability_response.raise_for_status()
        capability = capability_response.json()
        if capability["mode"] != "replay" or not capability["observations_ready"]:
            raise RuntimeError("replay API is not ready")
        as_of = capability["replay_window_end"]
        object_id = None
        if scenario == "core":
            object_response = await client.get("/api/v1/attention/objects", params={"as_of": as_of})
            object_response.raise_for_status()
            objects = object_response.json()
            candidates = [
                item for item in objects["items"] if item["object_id"] and item["channel_count"]
            ]
            if not candidates:
                raise RuntimeError("no object with channels in replay window")
            object_id = candidates[0]["object_id"]
        queue_request = (
            "queue",
            "/api/v1/attention",
            {"as_of": as_of, "view": "attention", "offset": 0, "limit": 25},
        )
        lane_requests = [
            (
                "alarm_lane",
                "/api/v1/attention",
                {"as_of": as_of, "view": "attention", "offset": 0, "limit": 1, "alarm": "true"},
            ),
            (
                "text_lane",
                "/api/v1/attention",
                {"as_of": as_of, "view": "attention", "offset": 0, "limit": 1, "alarm": "false"},
            ),
            (
                "object_overview",
                "/api/v1/attention/objects",
                {"as_of": as_of, "candidate_kind": "candidate", "offset": 0, "limit": 5},
            ),
        ]
        requests = (
            [queue_request, *lane_requests]
            if scenario == "queue-overview"
            else [
                queue_request,
                ("objects", "/api/v1/attention/objects", {"as_of": as_of}),
                (
                    "channels",
                    "/api/v1/attention/channels",
                    {"as_of": as_of, "object_id": object_id, "offset": 0, "limit": 25},
                ),
            ]
        )
        for _, path, params in requests:
            response = await client.get(path, params=params)
            response.raise_for_status()
        samples = defaultdict(list)
        errors = Counter()
        cycles = []

        async def measured_get(name, path, params):
            started = time.perf_counter()
            try:
                response = await client.get(path, params=params)
                response.raise_for_status()
                body = response.json()
                if body.get("mode") != "replay":
                    raise ValueError("unexpected mode")
                samples[name].append(time.perf_counter() - started)
                return body
            except (httpx.HTTPError, ValueError) as error:
                errors[f"{name}:{type(error).__name__}"] += 1
                return None

        async def worker():
            for _ in range(iterations):
                if scenario == "queue-overview":
                    cycle_started = time.perf_counter()
                    queue = await measured_get(*requests[0])
                    if queue is None:
                        continue
                    alarm, text, objects = await asyncio.gather(
                        *(measured_get(*request) for request in requests[1:])
                    )
                    if alarm is None or text is None or objects is None:
                        continue
                    if (
                        alarm.get("total") != queue.get("source_alarm_count")
                        or text.get("total") != queue.get("watch_text_count")
                        or alarm.get("total", 0) + text.get("total", 0) != queue.get("total")
                        or (
                            objects.get("total", 0) <= 5
                            and sum(item["candidate_count"] for item in objects["items"])
                            != queue.get("total")
                        )
                    ):
                        errors["queue_overview:count_mismatch"] += 1
                        continue
                    cycles.append(time.perf_counter() - cycle_started)
                else:
                    for request in requests:
                        await measured_get(*request)

        started = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(users)))
        elapsed = time.perf_counter() - started
    print(
        json.dumps(
            {
                "scenario": scenario,
                "users": users,
                "iterations_per_user": iterations,
                "replay_rows": capability.get("replay_rows"),
                "requests_expected": users * iterations * len(requests),
                "elapsed_s": round(elapsed, 2),
                "errors": dict(errors),
                "complete_cycles": len(cycles) if scenario == "queue-overview" else None,
                "cycle_p50_ms": percentile(cycles, 0.5) if cycles else None,
                "cycle_p95_ms": percentile(cycles, 0.95) if cycles else None,
                "cycle_max_ms": round(max(cycles) * 1000, 1) if cycles else None,
                "endpoints": {
                    name: {
                        "count": len(values),
                        "p50_ms": percentile(values, 0.5),
                        "p95_ms": percentile(values, 0.95),
                        "max_ms": round(max(values) * 1000, 1),
                    }
                    for name, values in samples.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return int(bool(errors))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--scenario", choices=("core", "queue-overview"), default="core")
    args = parser.parse_args()
    if not 1 <= args.users <= 50 or not 1 <= args.iterations <= 100:
        parser.error("--users must be 1..50 and --iterations must be 1..100")
    raise SystemExit(asyncio.run(run(args.base_url, args.users, args.iterations, args.scenario)))
