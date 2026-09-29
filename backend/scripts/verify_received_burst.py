"""Exercise a multi-object synthetic burst in a disposable received stream.

The shape matches one observed replay window's record counts, not its contents.
This is a local import/API integrity check, not a customer feed or SLA test.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psycopg
from load_received_batch import load, row_uid

NAMESPACE = "synthetic-burst"
SEED_ROWS = 3705
SEED_ALARMS = 1723
SEED_TEXT = 15
SEED_CHANNELS = 22
SEED_OBJECTS = 19
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def require_disposable(dsn: str, base_url: str, stream: str) -> None:
    if (
        urlparse(dsn).hostname not in LOOPBACK
        or urlparse(base_url).hostname not in LOOPBACK
        or not stream.startswith("synthetic-burst-")
    ):
        raise ValueError("probe requires loopback DB/API and a synthetic-burst stream")
    with psycopg.connect(dsn) as connection:
        present = connection.execute(
            "SELECT to_regclass('public.dispatch_replay_snapshots') IS NOT NULL"
        ).fetchone()[0]
        if present:
            existing = connection.execute(
                "SELECT count(*) FROM dispatch_replay_snapshots"
            ).fetchone()[0]
            if existing:
                raise ValueError("probe requires a disposable DB without existing streams")


def seed_records(event_at: str) -> list[dict]:
    result = []
    for index in range(SEED_ROWS):
        if index < SEED_ALARMS:
            channel_index = index % 8
            sensor_type, system_type, value_raw, alarm = (
                "Газовый датчик",
                "Газовая охрана",
                "Неисправен",
                True,
            )
        elif index < SEED_ALARMS + SEED_TEXT:
            channel_index = 8 + (index - SEED_ALARMS) % (SEED_CHANNELS - 8)
            sensor_type, system_type, value_raw, alarm = (
                "Состояние фазы",
                "Диспетчерский контроль",
                "Обесточен",
                False,
            )
        else:
            channel_index = index % SEED_CHANNELS
            sensor_type, system_type, value_raw, alarm = (
                "Состояние фазы",
                "Диспетчерский контроль",
                "Норма",
                False,
            )
        result.append(
            {
                "source_record_id": f"synthetic-row-{index:04d}",
                "channel_id": f"synthetic-channel-{channel_index:02d}",
                "object_id": f"synthetic-object-{channel_index % SEED_OBJECTS:02d}",
                "sensor_type": sensor_type,
                "system_type": system_type,
                "value_raw": value_raw,
                "alarm": alarm,
                "event_at": event_at,
            }
        )
    return result


def import_records(dsn: str, stream: str, batch_id: str, records: list[dict]) -> dict:
    with tempfile.TemporaryDirectory(prefix="infra-pulse-synthetic-burst-") as directory:
        path = Path(directory) / "synthetic-burst.json"
        path.write_text(
            json.dumps({"batch_id": batch_id, "records": records}, ensure_ascii=False),
            encoding="utf-8",
        )
        return load(path, dsn=dsn, stream_id=stream, namespace_id=NAMESPACE)


def expected_message(
    record: dict, *, stream: str, batch_id: str, ordinal: int, import_report: dict
) -> dict:
    event_at = datetime.fromisoformat(record["event_at"]).astimezone(UTC)
    available_at = datetime.fromisoformat(import_report["received_at"]).astimezone(UTC)
    flags = []
    if record.get("object_id") is not None:
        flags.append("object_mapping_from_input_unverified")
    if event_at > available_at:
        flags.append("source_clock_ahead_of_import")
    return {
        "row_uid": row_uid(NAMESPACE, stream, batch_id, record["source_record_id"]),
        "source_event_id": record.get("source_event_id"),
        "channel_id": record["channel_id"],
        "object_id": record.get("object_id"),
        "sensor_type": record.get("sensor_type"),
        "system_type": record.get("system_type"),
        "value_raw": record["value_raw"],
        "value_numeric": record.get("value_numeric"),
        "alarm": record["alarm"],
        "event_at": event_at,
        "available_at": available_at,
        "availability_basis": "observed",
        "source_namespace": NAMESPACE,
        "snapshot_id": stream,
        "source_file": "synthetic-burst.json",
        "source_sha256": import_report["source_sha256"],
        "record_ordinal": ordinal,
        "reference_version": record.get("reference_version"),
        "quality_flags": flags,
    }


def same_message(actual: dict, expected: dict) -> bool:
    return all(
        (
            datetime.fromisoformat(actual[key]).astimezone(UTC)
            if key in ("event_at", "available_at")
            else actual[key]
        )
        == value
        for key, value in expected.items()
    )


def paged_ids(
    client: httpx.Client,
    *,
    view: str,
    alarm: bool | None,
    as_of: str,
    watermark: int,
    stream: str,
    expected_messages: dict[str, dict] | None = None,
) -> tuple[set[str], int]:
    offset = 0
    seen: set[str] = set()
    total = None
    while True:
        params: dict[str, str | int | bool] = {
            "view": view,
            "as_of": as_of,
            "received_watermark": watermark,
            "offset": offset,
            "limit": 100,
        }
        if alarm is not None:
            params["alarm"] = alarm
        response = client.get("/api/v1/attention", params=params)
        response.raise_for_status()
        body = response.json()
        if body["mode"] != "received" or body["received_watermark"] != watermark:
            raise ValueError("API returned a different received scope")
        if total is None:
            total = body["total"]
        if total != body["total"]:
            raise ValueError("API total changed between pages")
        if any(
            item["message"]["source_namespace"] != NAMESPACE
            or item["message"]["snapshot_id"] != stream
            for item in body["items"]
        ):
            raise ValueError("API page contains a different received namespace or stream")
        if expected_messages is not None and any(
            item["message"]["row_uid"] not in expected_messages
            or not same_message(item["message"], expected_messages[item["message"]["row_uid"]])
            for item in body["items"]
        ):
            raise ValueError("received API content differs from imported JSON")
        page = {item["message"]["row_uid"] for item in body["items"]}
        if len(page) != len(body["items"]) or seen & page:
            raise ValueError("duplicate row UID in received API pages")
        seen.update(page)
        offset += len(body["items"])
        if offset >= total:
            break
        if not body["items"]:
            raise ValueError("empty page before received API total")
    if offset != total:
        raise ValueError("received API pages do not reconcile")
    return seen, total


def paged_objects(client: httpx.Client, *, as_of: str, watermark: int) -> dict:
    offset = 0
    total = None
    items: list[dict] = []
    while True:
        response = client.get(
            "/api/v1/attention/objects",
            params={
                "as_of": as_of,
                "received_watermark": watermark,
                "candidate_kind": "all",
                "offset": offset,
                "limit": 25,
            },
        )
        response.raise_for_status()
        page = response.json()
        if (
            page["mode"] != "received"
            or page["received_watermark"] != watermark
            or page["candidate_kind"] != "all"
            or page["offset"] != offset
        ):
            raise ValueError("received object page differs from verification scope")
        if total is None:
            total = page["total"]
        if page["total"] != total:
            raise ValueError("received object total changed between pages")
        items.extend(page["items"])
        offset += len(page["items"])
        if offset >= total:
            break
        if not page["items"]:
            raise ValueError("empty object page before received total")
    if offset != total:
        raise ValueError("received object pages do not reconcile")
    return {"total": total, "items": items}


def run(dsn: str, base_url: str, stream: str) -> dict:
    require_disposable(dsn, base_url, stream)
    now = datetime.now(UTC)
    event_at = (now - timedelta(minutes=5)).isoformat()
    seed = seed_records(event_at)
    if len(seed) != SEED_ROWS or len({row["channel_id"] for row in seed}) != SEED_CHANNELS:
        raise AssertionError("synthetic burst shape changed")
    if len({row["object_id"] for row in seed}) != SEED_OBJECTS:
        raise AssertionError("synthetic object count changed")
    report = import_records(dsn, stream, "synthetic-seed-v1", seed)
    if report["rows"] != SEED_ROWS or report["alarms"] != SEED_ALARMS or report["repeated"]:
        raise AssertionError("synthetic seed import did not reconcile")
    expected_all = {
        row_uid(NAMESPACE, stream, "synthetic-seed-v1", row["source_record_id"]) for row in seed
    }
    expected_alarms = {
        row_uid(NAMESPACE, stream, "synthetic-seed-v1", row["source_record_id"])
        for row in seed
        if row["alarm"]
    }
    expected_text = {
        row_uid(NAMESPACE, stream, "synthetic-seed-v1", row["source_record_id"])
        for row in seed
        if not row["alarm"] and row["value_raw"] == "Обесточен"
    }
    expected_messages = {
        row_uid(NAMESPACE, stream, "synthetic-seed-v1", row["source_record_id"]): expected_message(
            row,
            stream=stream,
            batch_id="synthetic-seed-v1",
            ordinal=ordinal,
            import_report=report,
        )
        for ordinal, row in enumerate(seed, start=1)
    }
    with httpx.Client(base_url=base_url, timeout=30) as client:
        first = client.get("/api/v1/attention", params={"view": "attention", "limit": 1})
        first.raise_for_status()
        as_of = first.json()["as_of"]
        watermark = first.json()["received_watermark"]
        if watermark != SEED_ROWS:
            raise ValueError("seed watermark differs from imported records")
        all_ids, all_total = paged_ids(
            client,
            view="all",
            alarm=None,
            as_of=as_of,
            watermark=watermark,
            stream=stream,
            expected_messages=expected_messages,
        )
        candidate_ids, candidate_total = paged_ids(
            client, view="attention", alarm=None, as_of=as_of, watermark=watermark, stream=stream
        )
        alarm_ids, alarm_total = paged_ids(
            client, view="attention", alarm=True, as_of=as_of, watermark=watermark, stream=stream
        )
        text_ids, text_total = paged_ids(
            client, view="attention", alarm=False, as_of=as_of, watermark=watermark, stream=stream
        )
        objects = paged_objects(client, as_of=as_of, watermark=watermark)
        if (
            all_ids != expected_all
            or alarm_ids != expected_alarms
            or text_ids != expected_text
            or candidate_ids != expected_alarms | expected_text
            or (all_total, candidate_total, alarm_total, text_total)
            != (SEED_ROWS, SEED_ALARMS + SEED_TEXT, SEED_ALARMS, SEED_TEXT)
            or objects["total"] != SEED_OBJECTS
            or sum(item["record_count"] for item in objects["items"]) != SEED_ROWS
            or sum(item["candidate_count"] for item in objects["items"]) != SEED_ALARMS + SEED_TEXT
        ):
            raise ValueError("synthetic source/API/object counts differ")

        followup = [
            {
                "source_record_id": "synthetic-followup-0001",
                "channel_id": "synthetic-new-channel",
                "object_id": "synthetic-new-object",
                "sensor_type": "Газовый датчик",
                "system_type": "Газовая охрана",
                "value_raw": "Обнаружен газ",
                "alarm": True,
                "event_at": (now - timedelta(minutes=10)).isoformat(),
            }
        ]
        later = import_records(dsn, stream, "synthetic-followup-v1", followup)
        if later["rows"] != 1 or later["alarms"] != 1 or later["repeated"]:
            raise AssertionError("synthetic followup import did not reconcile")
        updates_response = client.get(
            "/api/v1/capabilities", params={"after_received_watermark": watermark}
        )
        updates_response.raise_for_status()
        updates = updates_response.json()
        if (
            updates["received_rows"] != SEED_ROWS + 1
            or updates["received_rows_after_watermark"] != 1
            or updates["received_source_alarms_after_watermark"] != 1
            or updates["received_candidates_after_watermark"] != 1
        ):
            raise ValueError("new-record counters differ from followup import")
        pinned_ids, pinned_total = paged_ids(
            client, view="attention", alarm=None, as_of=as_of, watermark=watermark, stream=stream
        )
        if pinned_ids != candidate_ids or pinned_total != candidate_total:
            raise ValueError("pinned queue changed after a new batch")
        new_response = client.get(
            "/api/v1/attention",
            params={
                "view": "all",
                "alarm": True,
                "after_received_watermark": watermark,
                "limit": 25,
            },
        )
        new_response.raise_for_status()
        new = new_response.json()
        expected_new_uid = row_uid(
            NAMESPACE, stream, "synthetic-followup-v1", "synthetic-followup-0001"
        )
        if new["total"] != 1 or new["items"][0]["message"]["row_uid"] != expected_new_uid:
            raise ValueError("new source alarm is not reachable after pinned snapshot")
        expected_new_message = expected_message(
            followup[0],
            stream=stream,
            batch_id="synthetic-followup-v1",
            ordinal=1,
            import_report=later,
        )
        if not same_message(new["items"][0]["message"], expected_new_message):
            raise ValueError("new source alarm content differs from imported JSON")

    return {
        "status": "synthetic_received_burst_content_verified",
        "scope": "disposable_local_received_stream; not_customer_delivery_or_SLA",
        "counts": {
            "seed_rows": SEED_ROWS,
            "seed_alarms": SEED_ALARMS,
            "seed_watch_text_at_alarm_false": SEED_TEXT,
            "seed_candidates": SEED_ALARMS + SEED_TEXT,
            "seed_channels": SEED_CHANNELS,
            "seed_objects": SEED_OBJECTS,
            "followup_rows": 1,
            "pinned_candidates_after_followup": pinned_total,
            "new_alarm_after_watermark": new["total"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    dsn = os.environ.get("INFRA_RECEIVED_DSN", "")
    result = run(dsn, args.base_url, args.stream)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
