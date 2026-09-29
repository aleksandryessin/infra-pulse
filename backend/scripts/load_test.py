"""Load test of the dispatcher path: N concurrent sessions over HTTP (ТЗ §9/§11, OPS-01).

Each virtual user is one session (``httpx.AsyncClient``) that keeps the forecast state
poll of the UI (``/forecast-state`` every ``--poll-seconds``; with ``--notifications``
also the bell ``/notifications`` for 24 h before ``data_as_of``; with
``--source-alarms`` also «Сейчас» of the «Прогноз» screen, ``/attention/source-alarms``
for 24 h before ``source_messages_as_of``, 6 latest messages) and,
between think pauses, does what a dispatcher does on a shift: opens the forecast list,
a card and its decision history, the object scheme and the forecast journal. Latency
is measured per route template; the report gives count, p50, p95, p99, max and errors
(5xx or no answer; 4xx are counted apart).

Authentication:

* ``--dev`` — the API runs with ``INFRA_AUTH_MODE=dev_stub`` (local only);
* ``--cookie NAME=VALUE`` — a directory session copied from the browser; repeat the
  option (or use ``--cookie-file``, one per line) to spread users over several
  sessions. Reads need no anti-CSRF header, so the test is read-only.
* ``--cacert FILE`` — the CA bundle of a local Caddy (internal CA) for ``https://``.

``--seed-synthetic`` first uploads a synthetic channel reference and ~13 months of a
synthetic phase journal through ``POST /api/v1/imports`` (the worker must be running)
and waits until the journal is published, so a fresh ``received`` stand has cards.
It writes nothing else. Example (local stand, see docs/OPERATIONS.md)::

    uv run --locked python backend/scripts/load_test.py --base-url http://127.0.0.1:8011 \
        --dev --seed-synthetic --users 20 --duration 300 --json var/load-test.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

MSK = timezone(timedelta(hours=3))
PHASE = "Состояние фазы"
JOURNAL_HEADER = '"ид_события","ид_канала_данных","дата","время","тревожное","значение_датчика"'
CHANNELS_HEADER = (
    '"ид_канала_данных","тип_инж_системы","тип_датчика","тег_инженерной_системы",'
    '"название_датчика","ид_объект"'
)
FEEDERS = ["ГРО{n} ПК{p}", "ФВ{n} ПК{p}-{q}", "ФАНС{n} ПК{p}", "РО{n} ПК{p}"]


@dataclass
class Stats:
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    statuses: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )
    errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record(self, route: str, seconds: float, status: int | None) -> None:
        self.latencies[route].append(seconds)
        key = str(status) if status is not None else "no_answer"
        self.statuses[route][key] += 1
        if status is None or status >= 500:
            self.errors[route] += 1


def percentile(values: list[float], share: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return ordered[index]


async def call(client: httpx.AsyncClient, stats: Stats, route: str, url: str, **params):
    started = time.perf_counter()
    try:
        response = await client.get(url, params=params or None)
    except httpx.HTTPError:
        stats.record(route, time.perf_counter() - started, None)
        return None
    stats.record(route, time.perf_counter() - started, response.status_code)
    return response if response.status_code == 200 else None


async def poller(
    client, stats, stop: float, every: float, notifications: bool, source_alarms: bool = False
) -> None:
    """The UI timer: forecast state, then the bell and «Сейчас» for 24 h before the data."""
    await asyncio.sleep(random.uniform(0, every))
    while time.monotonic() < stop:
        state = await call(client, stats, "GET /api/v1/forecast-state", "/api/v1/forecast-state")
        body = state.json() if state is not None else {}
        reference = body.get("data_as_of")
        if notifications and reference:
            since = datetime.fromisoformat(reference) - timedelta(hours=24)
            await call(
                client,
                stats,
                "GET /api/v1/notifications",
                "/api/v1/notifications",
                since=since.isoformat(),
            )
        source_at = body.get("source_messages_as_of")
        if source_alarms and source_at:
            # «Сейчас» (frontend/src/pages/forecast/ForecastPage.tsx, NowBlock): one request,
            # messages of 24 h before the source time, the NOW_LATEST = 6 latest; as_of only
            # in replay. The follow-up /attention?view=all of an empty window is not made.
            params = {
                "event_from": (datetime.fromisoformat(source_at) - timedelta(hours=24)).isoformat(),
                "limit": 6,
            }
            if body.get("mode") == "replay" and reference:
                params["as_of"] = reference
            await call(
                client,
                stats,
                "GET /api/v1/attention/source-alarms",
                "/api/v1/attention/source-alarms",
                **params,
            )
        await asyncio.sleep(every)


async def dispatcher(client, stats, stop: float, think: tuple[float, float]) -> None:
    """One shift of a dispatcher: list -> card -> history -> scheme -> journal."""
    card_ids: list[str] = []
    object_ids: list[str] = []
    await asyncio.sleep(random.uniform(0, think[1]))
    while time.monotonic() < stop:
        listed = await call(client, stats, "GET /api/v1/forecasts", "/api/v1/forecasts")
        if listed is not None:
            items = listed.json().get("items", [])
            card_ids = [item["card"]["id"] for item in items] or card_ids
            object_ids = [item["card"]["object_id"] for item in items] or object_ids
        await asyncio.sleep(random.uniform(*think))
        for _ in range(random.randint(1, 3)):
            if card_ids and time.monotonic() < stop:
                card = random.choice(card_ids)
                await call(
                    client,
                    stats,
                    "GET /api/v1/forecasts/{forecast_id}",
                    f"/api/v1/forecasts/{card}",
                )
                await call(
                    client,
                    stats,
                    "GET /api/v1/forecasts/{forecast_id}/decisions",
                    f"/api/v1/forecasts/{card}/decisions",
                )
                await asyncio.sleep(random.uniform(*think))
        if time.monotonic() >= stop:
            break
        await call(client, stats, "GET /api/v1/schemes", "/api/v1/schemes")
        if object_ids:
            obj = random.choice(object_ids)
            await call(client, stats, "GET /api/v1/schemes/{object_id}", f"/api/v1/schemes/{obj}")
        await asyncio.sleep(random.uniform(*think))
        if time.monotonic() >= stop:
            break
        await call(client, stats, "GET /api/v1/forecast-journal", "/api/v1/forecast-journal")
        await call(
            client,
            stats,
            "GET /api/v1/forecast-journal/summary",
            "/api/v1/forecast-journal/summary",
        )
        await asyncio.sleep(random.uniform(*think))


def synthetic_files(objects: int, days: int, seed: int = 17) -> tuple[bytes, bytes]:
    """A channel reference and a phase journal ending today (synthetic IDs 9xxxxx)."""
    rng = random.Random(seed)
    first = datetime.now(MSK).date() - timedelta(days=days - 1)
    object_ids = [str(930000 + index) for index in range(objects)]
    reference = [CHANNELS_HEADER]
    journal = [JOURNAL_HEADER]
    event_id = 0
    channels: dict[str, list[str]] = {}
    for number, object_id in enumerate(object_ids):
        ids = [f"{object_id}{index}" for index in range(len(FEEDERS))]
        channels[object_id] = ids
        for index, template in enumerate(FEEDERS):
            name = template.format(n=index + 1, p=10 * index + number, q=10 * index + 5)
            reference.append(
                f"{ids[index]},Электроснабжение,{PHASE},SYN-{index},{name},{object_id}"
            )

    def row(channel: str, day: date, clock: str, value: str) -> None:
        nonlocal event_id
        event_id += 1
        alarm = "true" if value == "Неисправен" else "false"
        journal.append(f'{event_id},{channel},"{day.isoformat()}","{clock}",{alarm},"{value}"')

    for offset in range(days):
        day = first + timedelta(days=offset)
        for number, object_id in enumerate(object_ids):
            chance = 0.01 + 0.08 * number / max(objects - 1, 1)
            for index, channel in enumerate(channels[object_id]):
                row(channel, day, "06:00:00", "Есть питание")
                if index < 3 and rng.random() < chance:
                    row(channel, day, f"10:{index * 3:02d}:00", "Неисправен")
                    row(channel, day, f"10:{index * 3 + 2:02d}:00", "Обесточен")
                    row(channel, day, f"12:{index * 3:02d}:00", "Норма")
    encode = lambda lines: ("\n".join(lines) + "\n").encode()  # noqa: E731
    return encode(reference), encode(journal)


async def seed(client: httpx.AsyncClient, objects: int, days: int, timeout: float) -> dict:
    reference, journal = synthetic_files(objects, days)
    result = {}
    for name, data, fmt in (
        ("synthetic-channels.csv", reference, "reference_channels_csv"),
        ("synthetic-phase.csv", journal, "journal_csv"),
    ):
        started = time.perf_counter()
        response = await client.post(
            "/api/v1/imports", files={"file": (name, data, "text/csv")}, data={"format": fmt}
        )
        response.raise_for_status()
        import_id = response.json()["id"]
        while True:
            report = (await client.get(f"/api/v1/imports/{import_id}")).json()
            if report["status"] in ("published", "duplicate", "failed"):
                break
            if time.perf_counter() - started > timeout:
                raise TimeoutError(f"{name} not processed in {timeout} s: {report['status']}")
            await asyncio.sleep(0.5)
        result[fmt] = {
            "status": report["status"],
            "rows_accepted": report.get("rows_accepted"),
            "seconds_to_final": round(time.perf_counter() - started, 2),
        }
        print(f"seed {name}: {result[fmt]}", file=sys.stderr)
    return result


def report(stats: Stats, seconds: float, users: int) -> dict:
    routes = {}
    total = errors = client_errors = 0
    for route in sorted(stats.latencies):
        values = stats.latencies[route]
        statuses = dict(stats.statuses[route])
        four = sum(n for code, n in statuses.items() if code.isdigit() and 400 <= int(code) < 500)
        total += len(values)
        errors += stats.errors[route]
        client_errors += four
        routes[route] = {
            "count": len(values),
            "p50_ms": round(statistics.median(values) * 1000, 1),
            "p95_ms": round(percentile(values, 0.95) * 1000, 1),
            "p99_ms": round(percentile(values, 0.99) * 1000, 1),
            "max_ms": round(max(values) * 1000, 1),
            "errors": stats.errors[route],
            "client_errors": four,
            "statuses": statuses,
        }
    everything = [value for values in stats.latencies.values() for value in values]
    return {
        "users": users,
        "seconds": round(seconds, 1),
        "requests": total,
        "requests_per_second": round(total / seconds, 2) if seconds else None,
        "errors": errors,
        "client_errors": client_errors,
        "p50_ms": round(statistics.median(everything) * 1000, 1) if everything else None,
        "p95_ms": round(percentile(everything, 0.95) * 1000, 1) if everything else None,
        "max_ms": round(max(everything) * 1000, 1) if everything else None,
        "routes": routes,
    }


def print_table(result: dict) -> None:
    print(
        f"users={result['users']} seconds={result['seconds']} requests={result['requests']} "
        f"rps={result['requests_per_second']} errors={result['errors']} "
        f"4xx={result['client_errors']} p50={result['p50_ms']} ms p95={result['p95_ms']} ms "
        f"max={result['max_ms']} ms"
    )
    columns = ("n", "p50", "p95", "p99", "max", "err", "4xx")
    widths = (6, 8, 8, 8, 8, 5, 5)
    print(
        f"{'route':48} "
        + " ".join(f"{name:>{width}}" for name, width in zip(columns, widths, strict=True))
    )
    for route, row in result["routes"].items():
        print(
            f"{route:48} {row['count']:>6} {row['p50_ms']:>8} {row['p95_ms']:>8} "
            f"{row['p99_ms']:>8} {row['max_ms']:>8} {row['errors']:>5} {row['client_errors']:>5}"
        )


async def main_async(args) -> int:
    cookies = list(args.cookie or [])
    if args.cookie_file:
        cookies += [line.strip() for line in args.cookie_file.read_text().splitlines() if line]
    if not args.dev and not cookies:
        print("give --dev (dev_stub API) or --cookie NAME=VALUE", file=sys.stderr)
        return 2
    timeout = httpx.Timeout(args.timeout)
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=4)

    def session(index: int) -> httpx.AsyncClient:
        headers = {"User-Agent": "infra-pulse-load-test/1"}
        if cookies:
            headers["Cookie"] = cookies[index % len(cookies)]
        return httpx.AsyncClient(
            base_url=args.base_url,
            headers=headers,
            timeout=timeout,
            limits=limits,
            verify=str(args.cacert) if args.cacert else True,
        )

    seeded = None
    if args.seed_synthetic:
        async with session(0) as client:
            seeded = await seed(client, args.seed_objects, args.seed_days, args.seed_timeout)
    stats = Stats()
    clients = [session(index) for index in range(args.users)]
    started = time.monotonic()
    stop = started + args.duration
    think = (args.think_min, args.think_max)
    try:
        tasks = []
        for client in clients:
            tasks.append(dispatcher(client, stats, stop, think))
            tasks.append(
                poller(
                    client,
                    stats,
                    stop,
                    args.poll_seconds,
                    args.notifications,
                    args.source_alarms,
                )
            )
        await asyncio.gather(*tasks)
    finally:
        for client in clients:
            await client.aclose()
    result = report(stats, time.monotonic() - started, args.users)
    result["base_url"] = args.base_url
    result["started_at"] = datetime.now(MSK).isoformat(timespec="seconds")
    result["scenario"] = {
        "poll_seconds": args.poll_seconds,
        "notifications": args.notifications,
        "source_alarms": args.source_alarms,
        "think_seconds": list(think),
        "auth": "dev_stub" if args.dev else f"cookie x{len(cookies)}",
    }
    if seeded is not None:
        result["seeded"] = seeded
    print_table(result)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 1 if result["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--duration", type=float, default=300.0, help="seconds")
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--notifications", action="store_true", help="also poll /notifications")
    parser.add_argument(
        "--source-alarms",
        action="store_true",
        help="also poll /attention/source-alarms («Сейчас» of the «Прогноз» screen)",
    )
    parser.add_argument("--think-min", type=float, default=2.0)
    parser.add_argument("--think-max", type=float, default=6.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--cacert", type=Path, help="CA bundle for https (a local Caddy CA)")
    parser.add_argument("--dev", action="store_true", help="API in dev_stub mode")
    parser.add_argument("--cookie", action="append", help="NAME=VALUE of a session cookie")
    parser.add_argument("--cookie-file", type=Path)
    parser.add_argument("--seed-synthetic", action="store_true")
    parser.add_argument("--seed-objects", type=int, default=19)
    parser.add_argument("--seed-days", type=int, default=400)
    parser.add_argument("--seed-timeout", type=float, default=600.0)
    parser.add_argument("--json", type=Path, help="write the report as JSON")
    args = parser.parse_args(argv)
    if args.users < 1 or args.duration <= 0 or args.think_min > args.think_max:
        parser.error("users >= 1, duration > 0, think-min <= think-max")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
