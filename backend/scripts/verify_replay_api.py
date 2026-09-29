"""Compare every API page and source message of one bounded accepted replay window.

Prints aggregate counts and hashes only. It does not infer incidents or severity.
Run against a disposable replay API loaded from the same accepted snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from infra_pulse_backend.operations.attention import POLICY_VERSION, WATCH_PAIR_SET


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def aware_utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("start and end must include a timezone")
    return result.astimezone(UTC)


def source_message(row: tuple) -> dict:
    (
        row_uid,
        event_id,
        channel_id,
        object_id,
        sensor_type,
        system_type,
        value_raw,
        value_numeric,
        alarm,
        event_at,
        source_file,
        source_sha256,
        record_ordinal,
        reference_version,
        epoch_placeholder,
        sentinel_candidate,
        rare_lexeme,
        timestamp_value_count,
        reference_status,
    ) = row
    flags = [
        label
        for present, label in (
            (epoch_placeholder, "epoch_placeholder"),
            (sentinel_candidate, "sentinel_candidate"),
            (rare_lexeme, "rare_lexeme"),
            ((timestamp_value_count or 0) > 1, "same_timestamp_conflict"),
            (reference_status != "matched", "reference_unmatched"),
            (value_numeric is not None and not math.isfinite(value_numeric), "nonfinite_numeric"),
        )
        if present
    ]
    if value_numeric is not None and not math.isfinite(value_numeric):
        value_numeric = None
    return {
        "row_uid": row_uid,
        "source_event_id": str(event_id) if event_id is not None else None,
        "channel_id": str(channel_id),
        "object_id": str(object_id) if object_id is not None else None,
        "sensor_type": sensor_type,
        "system_type": system_type,
        "value_raw": value_raw,
        "value_numeric": value_numeric,
        "alarm": alarm,
        "event_at": event_at.replace(tzinfo=UTC),
        "available_at": event_at.replace(tzinfo=UTC),
        "availability_basis": "simulated",
        "source_file": source_file,
        "source_sha256": source_sha256,
        "record_ordinal": record_ordinal,
        "reference_version": reference_version,
        "quality_flags": flags,
    }


def same_source_message(actual: dict, expected: dict) -> bool:
    return (
        all(
            (aware_utc(actual[key]) if key in ("event_at", "available_at") else actual[key])
            == value
            for key, value in expected.items()
            if key != "quality_flags"
        )
        and actual["quality_flags"] == expected["quality_flags"]
    )


def api_ids(
    client: httpx.Client,
    *,
    as_of: str,
    view: str,
    alarm: bool | None,
    namespace: str,
    snapshot_id: str,
    expected_messages: dict[str, dict] | None = None,
) -> tuple[set[str], int]:
    offset = 0
    expected_total = None
    seen: set[str] = set()
    while True:
        params: dict[str, str | int | bool] = {
            "as_of": as_of,
            "view": view,
            "offset": offset,
            "limit": 100,
        }
        if alarm is not None:
            params["alarm"] = alarm
        response = client.get("/api/v1/attention", params=params)
        response.raise_for_status()
        body = response.json()
        if body["view"] != view or body["policy_version"] != POLICY_VERSION:
            raise ValueError("API view or policy differs from verification scope")
        if expected_total is None:
            expected_total = body["total"]
        if body["total"] != expected_total:
            raise ValueError("API total changed between pages")
        if any(
            item["message"]["source_namespace"] != namespace
            or item["message"]["snapshot_id"] != snapshot_id
            for item in body["items"]
        ):
            raise ValueError("API page contains a different replay namespace or snapshot")
        if expected_messages is not None and any(
            item["message"]["row_uid"] not in expected_messages
            or not same_source_message(
                item["message"], expected_messages[item["message"]["row_uid"]]
            )
            for item in body["items"]
        ):
            raise ValueError("API message content differs from accepted Parquet")
        page = {item["message"]["row_uid"] for item in body["items"]}
        if len(page) != len(body["items"]) or seen & page:
            raise ValueError("duplicate row UID across API pages")
        seen.update(page)
        offset += len(body["items"])
        if offset >= expected_total:
            break
        if not body["items"]:
            raise ValueError("empty API page before reported total")
    if offset != expected_total:
        raise ValueError("API pages do not reconcile with total")
    return seen, expected_total


def api_object_groups(client: httpx.Client, *, as_of: str) -> dict:
    offset = 0
    total = None
    items: list[dict] = []
    while True:
        response = client.get(
            "/api/v1/attention/objects",
            params={"as_of": as_of, "candidate_kind": "all", "offset": offset, "limit": 25},
        )
        response.raise_for_status()
        page = response.json()
        if page["mode"] != "replay" or page["candidate_kind"] != "all" or page["offset"] != offset:
            raise ValueError("replay object page differs from verification scope")
        if total is None:
            total = page["total"]
        if page["total"] != total:
            raise ValueError("replay object total changed between pages")
        items.extend(page["items"])
        offset += len(page["items"])
        if offset >= total:
            break
        if not page["items"]:
            raise ValueError("empty object page before replay total")
    if offset != total:
        raise ValueError("replay object pages do not reconcile")
    return {"total": total, "items": items}


def api_channel_groups(
    client: httpx.Client, *, as_of: str, object_id: str | None, candidate_kind: str
) -> dict[tuple[str | None, str | None, str], tuple[int, int, int]]:
    offset = 0
    expected_total = None
    seen = {}
    while True:
        response = client.get(
            "/api/v1/attention/channels",
            params={
                "as_of": as_of,
                "object_id": object_id if object_id is not None else "__unknown__",
                "candidate_kind": candidate_kind,
                "offset": offset,
                "limit": 25,
            },
        )
        response.raise_for_status()
        body = response.json()
        if body["mode"] != "replay" or body["object_id"] != object_id:
            raise ValueError("API channel page differs from verification scope")
        if expected_total is None:
            expected_total = body["total"]
        if body["total"] != expected_total:
            raise ValueError("API channel total changed between pages")
        for item in body["items"]:
            key = (item["object_id"], item["system_type"], item["channel_id"])
            if key in seen:
                raise ValueError("duplicate channel group across API pages")
            seen[key] = (
                item["record_count"],
                item["candidate_count"],
                item["source_alarm_count"],
            )
        offset += len(body["items"])
        if offset >= expected_total:
            break
        if not body["items"]:
            raise ValueError("empty channel page before reported total")
    if offset != expected_total:
        raise ValueError("API channel pages do not reconcile with total")
    return seen


def verify(
    snapshot: Path, month: str, start: datetime, end: datetime, base_url: str, namespace: str
) -> dict:
    if not start < end or end - start > timedelta(hours=1):
        raise ValueError("verification window must be positive and at most one hour")
    manifest_path = snapshot / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("publication_status") != "accepted":
        raise ValueError("curated snapshot is not accepted")
    relative = f"curated/month={month}/events.parquet"
    source_path = snapshot / relative
    source_hash = manifest.get("output_sha256", {}).get(relative)
    if not source_hash or not source_path.is_file() or sha256(source_path) != source_hash:
        raise ValueError("curated month missing or differs from accepted manifest")
    if not namespace:
        raise ValueError("namespace is required")
    snapshot_key = f"{sha256(manifest_path)}|{month}|{start.isoformat()}|{end.isoformat()}"
    expected_snapshot_id = hashlib.sha256(snapshot_key.encode()).hexdigest()

    # Only the full Parquet comparison needs the optional data dependency.
    # API parity helpers are also tested in the minimal HTTP environment.
    import duckdb

    connection = duckdb.connect()
    try:
        rows = connection.execute(
            """SELECT row_uid, event_id, channel_id, object_id,
                      sensor_type, system_type, value_raw, value_numeric,
                      alarm, event_ts_utc, source_file, source_sha256,
                      record_ordinal, reference_version, is_epoch_placeholder,
                      sentinel_candidate, rare_lexeme, ts_group_distinct_values,
                      reference_status
               FROM read_parquet(?) WHERE event_ts_utc >= ? AND event_ts_utc < ?""",
            (str(source_path), start.replace(tzinfo=None), end.replace(tzinfo=None)),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) > 100_000:
        raise ValueError("verification window exceeds 100000 source records")
    all_ids = {row[0] for row in rows}
    alarm_ids = {row[0] for row in rows if row[8]}
    text_ids = {row[0] for row in rows if not row[8] and (row[4], row[6]) in WATCH_PAIR_SET}
    if len(all_ids) != len(rows):
        raise ValueError("duplicate row UID in source window")
    source_groups: dict[tuple[str | None, str | None, str], list[int]] = {}
    source_messages = {row[0]: source_message(row) for row in rows}
    for row in rows:
        _, _, channel_id, object_id, sensor_type, system_type, value_raw, _, alarm, *_ = row
        key = (str(object_id) if object_id is not None else None, system_type, str(channel_id))
        counts = source_groups.setdefault(key, [0, 0, 0])
        counts[0] += 1
        if alarm:
            counts[1] += 1
            counts[2] += 1
        elif (sensor_type, value_raw) in WATCH_PAIR_SET:
            counts[1] += 1

    with httpx.Client(base_url=base_url, timeout=30) as client:
        capabilities_response = client.get("/api/v1/capabilities")
        capabilities_response.raise_for_status()
        capabilities = capabilities_response.json()
        if capabilities["mode"] != "replay" or not capabilities["observations_ready"]:
            raise ValueError("API is not ready in replay mode")
        if (
            aware_utc(capabilities["replay_window_start"]) != start
            or aware_utc(capabilities["replay_window_end"]) != end
            or capabilities["replay_rows"] != len(rows)
        ):
            raise ValueError("API replay window differs from accepted source window")

        as_of = end.isoformat()
        scope = {"namespace": namespace, "snapshot_id": expected_snapshot_id}
        all_api, all_total = api_ids(
            client,
            as_of=as_of,
            view="all",
            alarm=None,
            expected_messages=source_messages,
            **scope,
        )
        candidates_api, candidate_total = api_ids(
            client, as_of=as_of, view="attention", alarm=None, **scope
        )
        alarms_api, alarm_total = api_ids(
            client, as_of=as_of, view="attention", alarm=True, **scope
        )
        texts_api, text_total = api_ids(client, as_of=as_of, view="attention", alarm=False, **scope)
        objects = api_object_groups(client, as_of=as_of)
        expected_by_object: dict[str | None, dict] = {}
        for key, counts in source_groups.items():
            object_counts = expected_by_object.setdefault(
                key[0], {"records": 0, "candidates": 0, "alarms": 0, "groups": {}}
            )
            object_counts["records"] += counts[0]
            object_counts["candidates"] += counts[1]
            object_counts["alarms"] += counts[2]
            object_counts["groups"][key] = tuple(counts)
        if objects["total"] != len(expected_by_object):
            raise ValueError("API object group count differs from source")
        seen_objects = set()
        channel_filter_counts = {"all": 0, "alarm": 0, "text": 0, "candidate": 0}
        for item in objects["items"]:
            object_id = item["object_id"]
            if object_id in seen_objects or object_id not in expected_by_object:
                raise ValueError("API object group identity differs from source")
            seen_objects.add(object_id)
            expected = expected_by_object[object_id]
            if (item["record_count"], item["candidate_count"], item["source_alarm_count"]) != (
                expected["records"],
                expected["candidates"],
                expected["alarms"],
            ):
                raise ValueError("API object group counts differ from source")
            for candidate_kind in channel_filter_counts:
                expected_groups = {
                    key: counts
                    for key, counts in expected["groups"].items()
                    if candidate_kind == "all"
                    or (candidate_kind == "alarm" and counts[2] > 0)
                    or (candidate_kind == "text" and counts[1] > counts[2])
                    or (candidate_kind == "candidate" and counts[1] > 0)
                }
                actual_groups = api_channel_groups(
                    client, as_of=as_of, object_id=object_id, candidate_kind=candidate_kind
                )
                if actual_groups != expected_groups:
                    raise ValueError("API channel filter or group counts differ from source")
                channel_filter_counts[candidate_kind] += len(actual_groups)

    if (
        all_api != all_ids
        or alarms_api != alarm_ids
        or texts_api != text_ids
        or candidates_api != alarm_ids | text_ids
        or (all_total, candidate_total, alarm_total, text_total)
        != (len(all_ids), len(alarm_ids | text_ids), len(alarm_ids), len(text_ids))
    ):
        raise ValueError("source and paginated API row identities differ")
    if (
        sum(item["record_count"] for item in objects["items"]) != len(all_ids)
        or sum(item["candidate_count"] for item in objects["items"]) != len(candidates_api)
        or sum(item["source_alarm_count"] for item in objects["items"]) != len(alarm_ids)
    ):
        raise ValueError("object groups do not reconcile with source/API counts")
    return {
        "status": "source_api_content_match",
        "scope": {
            "namespace": namespace,
            "snapshot_id": expected_snapshot_id,
            "snapshot_manifest_sha256": sha256(manifest_path),
            "curated_month_sha256": source_hash,
            "policy_version": POLICY_VERSION,
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat(),
            "availability_basis": "simulated_from_source_event_time",
        },
        "counts": {
            "source_and_api_rows": len(all_ids),
            "source_alarm": len(alarm_ids),
            "watch_text_at_alarm_false": len(text_ids),
            "candidates": len(candidates_api),
            "object_groups": objects["total"],
            "channel_groups_by_filter": channel_filter_counts,
        },
        "interpretation_limit": (
            "Bounded historical replay only. No observed customer delivery, "
            "operator workload, incident labels or accepted severity policy."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--namespace", default="local-replay")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    result = verify(
        args.snapshot,
        args.month,
        aware_utc(args.start),
        aware_utc(args.end),
        args.base_url,
        args.namespace,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
