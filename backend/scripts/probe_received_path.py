"""Bounded synthetic received import/read probe for a disposable local database.

Measures new source-alarm visibility behind a full page under parallel reads,
not receive-to-UI or OPS-01.
Only aggregate timings and process counters are printed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import psutil

NAMESPACE = "synthetic-probe"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
LOADER = Path(__file__).with_name("load_received_batch.py")


def percentile_ms(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(p * len(ordered)) - 1)] * 1000, 1)


def process_tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
        return [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


def process_counters(pid: int) -> dict[str, float | None]:
    rss = cpu_seconds = reads = writes = 0.0
    io_supported = True
    for process in process_tree(pid):
        try:
            rss += process.memory_info().rss
            cpu_time = process.cpu_times()
            cpu_seconds += cpu_time.user + cpu_time.system
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        try:
            io = process.io_counters()
            reads += io.read_bytes
            writes += io.write_bytes
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError, NotImplementedError):
            io_supported = False
    return {
        "rss_mb": rss / 1024**2,
        "cpu_seconds": cpu_seconds,
        "read_bytes": reads if io_supported else None,
        "write_bytes": writes if io_supported else None,
    }


async def sample_processes(pids: dict[str, int], stop: asyncio.Event) -> dict:
    if not pids:
        return {}
    observations = defaultdict(list)
    while not stop.is_set():
        for name, pid in pids.items():
            if not process_tree(pid):
                raise RuntimeError("profile process exited during probe")
            observations[name].append((time.perf_counter(), process_counters(pid)))
        await asyncio.sleep(0.25)
    summaries = {}
    for name, samples in observations.items():
        if not samples:
            continue
        cpu_intervals = [
            max(0, (later["cpu_seconds"] - earlier["cpu_seconds"]) / (later_at - at) * 100)
            for (at, earlier), (later_at, later) in zip(samples, samples[1:], strict=False)
            if later_at > at
        ]
        first = samples[0][1]
        last = samples[-1][1]
        summaries[name] = {
            "peak_sum_rss_mb": round(max(item["rss_mb"] for _, item in samples), 1),
            "peak_cpu_pct": round(max(cpu_intervals), 1) if cpu_intervals else None,
            "read_bytes_delta": max(0, last["read_bytes"] - first["read_bytes"])
            if first["read_bytes"] is not None and last["read_bytes"] is not None
            else None,
            "write_bytes_delta": max(0, last["write_bytes"] - first["write_bytes"])
            if first["write_bytes"] is not None and last["write_bytes"] is not None
            else None,
        }
    return summaries


async def import_synthetic(
    dsn: str,
    stream: str,
    batch_id: str,
    source_record_ids: list[str],
    *,
    alarm_record_id: str | None,
) -> dict:
    event_at = datetime.now(UTC).isoformat()
    payload = {
        "batch_id": batch_id,
        "records": [
            {
                "source_record_id": source_record_id,
                "channel_id": "synthetic-probe-channel",
                "object_id": "synthetic-probe-object",
                "sensor_type": "Состояние насоса",
                "system_type": "Водоотведение",
                "value_raw": "Неисправен"
                if source_record_id == alarm_record_id
                else "Работают все насосы в АНС",
                "alarm": source_record_id == alarm_record_id,
                "event_at": event_at,
            }
            for source_record_id in source_record_ids
        ],
    }
    with tempfile.TemporaryDirectory(prefix="infra-pulse-received-probe-") as directory:
        path = Path(directory) / "synthetic.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        environment = {**os.environ, "INFRA_RECEIVED_DSN": dsn}
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(LOADER),
            "--input",
            str(path),
            "--stream",
            stream,
            "--namespace",
            NAMESPACE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        stdout, _ = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"synthetic import failed with exit code {process.returncode}")
    report = json.loads(stdout)
    if (
        report["repeated"]
        or report["rows"] != len(source_record_ids)
        or report["alarms"] != int(alarm_record_id is not None)
    ):
        raise RuntimeError("unexpected synthetic import accounting")
    return report


def synthetic_uid(stream: str, batch_id: str, source_record_id: str) -> str:
    identity = "\x00".join((NAMESPACE, stream, batch_id, source_record_id))
    return hashlib.sha256(identity.encode()).hexdigest()


async def detect_new_alarm(
    client: httpx.AsyncClient,
    row_uid: str,
    lower_watermark: int,
    upper_watermark: int,
    stop: asyncio.Event,
) -> tuple[float | None, int]:
    errors = 0
    while not stop.is_set():
        try:
            response = await client.get(
                "/api/v1/attention",
                params={
                    "view": "all",
                    "alarm": "true",
                    "after_received_watermark": lower_watermark,
                    "limit": 25,
                },
            )
            response.raise_for_status()
            body = response.json()
            if (
                body["total"] == 1
                and body["items"][0]["message"]["row_uid"] == row_uid
                and body["items"][0]["message"]["alarm"]
                and body["received_after_watermark"] == lower_watermark
                and body["received_watermark"] == upper_watermark
            ):
                return time.time(), errors
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            errors += 1
        await asyncio.sleep(0.1)
    return None, errors


async def run(args: argparse.Namespace) -> int:
    dsn = os.environ["INFRA_RECEIVED_DSN"]
    unique = uuid4().hex
    limits = httpx.Limits(
        max_connections=args.users + 10, max_keepalive_connections=args.users + 10
    )
    async with httpx.AsyncClient(base_url=args.base_url, timeout=20, limits=limits) as client:
        initial_response = await client.get("/api/v1/capabilities")
        initial_response.raise_for_status()
        initial = initial_response.json()
        if initial["mode"] != "received" or initial["received_rows"] not in (None, 0):
            raise RuntimeError("probe requires an empty disposable received stream")
        baseline_id = f"baseline-{unique}"
        await import_synthetic(dsn, args.stream, baseline_id, [baseline_id], alarm_record_id=None)
        capability_response = await client.get("/api/v1/capabilities")
        capability_response.raise_for_status()
        capability = capability_response.json()
        if capability["mode"] != "received" or not capability["observations_ready"]:
            raise RuntimeError("received API is not ready")
        if capability["received_rows"] != 1:
            raise RuntimeError("probe requires an empty disposable stream")
        lower_watermark = capability["received_rows"]

        batch_id = f"alarm-{unique}"
        normal_ids = [f"normal-{unique}-{index}" for index in range(args.normal_rows)]
        normal_uids = [synthetic_uid(args.stream, batch_id, item) for item in normal_ids]
        # All rows share one import time. Choose an alarm UID that sorts after
        # at least 25 ordinary rows, so the first unfiltered page cannot contain it.
        for index in range(1000):
            alarm_record_id = f"alarm-{unique}-{index}"
            alarm_uid = synthetic_uid(args.stream, batch_id, alarm_record_id)
            normal_before_alarm = sum(uid < alarm_uid for uid in normal_uids)
            if normal_before_alarm >= 25:
                break
        else:
            raise RuntimeError("could not construct the synthetic full-page case")
        upper_watermark = lower_watermark + len(normal_ids) + 1

        requests = [
            ("queue", "/api/v1/attention", {"view": "attention", "limit": 25}),
            (
                "new_source_alarms",
                "/api/v1/attention",
                {
                    "view": "all",
                    "alarm": "true",
                    "after_received_watermark": lower_watermark,
                    "limit": 25,
                },
            ),
            ("objects", "/api/v1/attention/objects", {}),
            (
                "channels",
                "/api/v1/attention/channels",
                {"object_id": "synthetic-probe-object", "limit": 25},
            ),
        ]
        samples: dict[str, list[float]] = defaultdict(list)
        errors: Counter[str] = Counter()
        stop = asyncio.Event()
        pids = {name: pid for name, pid in (("api", args.api_pid), ("db", args.db_pid)) if pid}
        if any(not process_tree(pid) for pid in pids.values()):
            raise RuntimeError("profile process missing")
        resource_task = asyncio.create_task(sample_processes(pids, stop))

        async def reader() -> None:
            while not stop.is_set():
                for name, path, params in requests:
                    started = time.perf_counter()
                    try:
                        response = await client.get(path, params=params)
                        response.raise_for_status()
                        if response.json().get("mode") != "received":
                            raise ValueError("unexpected mode")
                        samples[name].append(time.perf_counter() - started)
                    except (httpx.HTTPError, ValueError) as error:
                        errors[f"{name}:{type(error).__name__}"] += 1
                await asyncio.sleep(args.reader_pause)

        readers = [asyncio.create_task(reader()) for _ in range(args.users)]
        await asyncio.sleep(1)
        detector = asyncio.create_task(
            detect_new_alarm(client, alarm_uid, lower_watermark, upper_watermark, stop)
        )
        started = time.perf_counter()
        report = await import_synthetic(
            dsn,
            args.stream,
            batch_id,
            [*normal_ids, alarm_record_id],
            alarm_record_id=alarm_record_id,
        )
        import_returned = time.perf_counter()
        updates_response = await client.get(
            "/api/v1/capabilities", params={"after_received_watermark": lower_watermark}
        )
        updates_response.raise_for_status()
        updates = updates_response.json()
        if (
            updates["received_rows"] != upper_watermark
            or updates["received_rows_after_watermark"] != len(normal_ids) + 1
            or updates["received_source_alarms_after_watermark"] != 1
        ):
            raise RuntimeError("new-record and source-alarm counters disagree with import")
        full_page_response = await client.get(
            "/api/v1/attention",
            params={"view": "all", "after_received_watermark": lower_watermark, "limit": 25},
        )
        full_page_response.raise_for_status()
        full_page = full_page_response.json()
        if (
            full_page["total"] != len(normal_ids) + 1
            or len(full_page["items"]) != 25
            or any(item["message"]["row_uid"] == alarm_uid for item in full_page["items"])
        ):
            raise RuntimeError("alarm was not hidden behind the first full page")
        await asyncio.sleep(args.duration)
        stop.set()
        seen_at, detector_errors = await detector
        await asyncio.gather(*readers)
        resources = await resource_task
        elapsed = time.perf_counter() - started

    received_at = datetime.fromisoformat(report["received_at"]).timestamp()
    received_to_api_ms = round((seen_at - received_at) * 1000, 1) if seen_at else None
    output = {
        "kind": "synthetic_local_received_probe",
        "platform": platform.platform(),
        "users": args.users,
        "duration_after_import_s": args.duration,
        "baseline_rows": lower_watermark,
        "ordinary_rows": len(normal_ids),
        "ordinary_rows_before_alarm": normal_before_alarm,
        "new_rows": updates["received_rows_after_watermark"],
        "new_source_alarms": updates["received_source_alarms_after_watermark"],
        "alarm_outside_first_full_page": True,
        "import_to_api_ms": received_to_api_ms,
        "import_call_ms": round((import_returned - started) * 1000, 1),
        "elapsed_s": round(elapsed, 2),
        "errors": dict(errors),
        "detector_errors": detector_errors,
        "endpoints": {
            name: {
                "count": len(values),
                "p50_ms": percentile_ms(values, 0.5),
                "p95_ms": percentile_ms(values, 0.95),
                "max_ms": round(max(values) * 1000, 1),
            }
            for name, values in samples.items()
            if values
        },
        "processes": resources,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return int(bool(errors) or detector_errors > 0 or seen_at is None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--stream", required=True)
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--normal-rows", type=int, default=40)
    parser.add_argument("--reader-pause", type=float, default=0.5)
    parser.add_argument("--api-pid", type=int, required=True)
    parser.add_argument("--db-pid", type=int, required=True)
    args = parser.parse_args()
    dsn = os.environ.get("INFRA_RECEIVED_DSN", "")
    if (
        urlparse(dsn).hostname not in LOOPBACK
        or urlparse(args.base_url).hostname not in LOOPBACK
        or not args.stream.startswith("synthetic-probe-")
    ):
        parser.error("local loopback DSN/API and synthetic-probe-* stream required")
    if not 1 <= args.users <= 50 or not 5 <= args.duration <= 60:
        parser.error("users must be 1..50 and duration 5..60 seconds")
    if not 25 <= args.normal_rows <= 4999:
        parser.error("normal-rows must be 25..4999 to stay within a 5000-row batch")
    if not 0.1 <= args.reader_pause <= 5 or any(
        pid is not None and pid <= 0 for pid in (args.api_pid, args.db_pid)
    ):
        parser.error("reader-pause must be 0.1..5 seconds and PIDs must be positive")
    raise SystemExit(asyncio.run(run(args)))
