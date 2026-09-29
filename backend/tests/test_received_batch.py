import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import psycopg
import pytest
from app_role import app_dsn
from fastapi.testclient import TestClient
from psycopg import sql
from pydantic import ValidationError

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_core.contracts.attention import ReceivedBatch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "load_received_batch.py"
SPEC = importlib.util.spec_from_file_location("load_received_batch", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
read_batch = module.read_batch
row_uid = module.row_uid
load = module.load
initialize_stream = module.initialize_stream
scan_directory = module.scan_directory
apply_migrations = module.apply_migrations


def sample_record(record_id: str = "r1") -> dict:
    return {
        "source_record_id": record_id,
        "channel_id": "channel-1",
        "sensor_type": "Газовый датчик",
        "value_raw": "Обнаружен газ",
        "alarm": False,
        "event_at": "2026-09-24T10:00:00+03:00",
    }


def test_received_batch_preserves_raw_text_and_requires_explicit_alarm(tmp_path):
    path = tmp_path / "batch.json"
    path.write_text(
        json.dumps({"batch_id": "b1", "records": [sample_record()]}, ensure_ascii=False),
        encoding="utf-8",
    )
    batch, raw, digest = read_batch(path)
    assert batch.records[0].value_raw == raw["records"][0]["value_raw"]
    assert batch.records[0].alarm is False
    assert batch.records[0].event_at == datetime(2026, 9, 24, 7, tzinfo=UTC)
    assert len(digest) == 64
    assert row_uid("ns", "stream", "b1", "r1") == row_uid("ns", "stream", "b1", "r1")
    assert row_uid("ns", "stream", "b1", "r1") != row_uid("ns", "stream", "b2", "r1")


@pytest.mark.parametrize(
    "record",
    [
        {**sample_record(), "alarm": "true"},
        {**sample_record(), "event_at": "2026-09-24T10:00:00"},
        {**sample_record(), "value_numeric": float("nan")},
    ],
)
def test_received_batch_rejects_ambiguous_or_invalid_fields(record):
    with pytest.raises(ValidationError):
        ReceivedBatch.model_validate({"batch_id": "b1", "records": [record]})


def test_received_batch_rejects_duplicate_source_record_id():
    with pytest.raises(ValidationError, match="duplicate source_record_id"):
        ReceivedBatch.model_validate(
            {"batch_id": "b1", "records": [sample_record(), sample_record()]}
        )


def test_received_import_time_follows_stream_lock(tmp_path, monkeypatch):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-order-{uuid4()}"
    initialize_stream(dsn, stream_id=stream, namespace_id="local-received")
    path = tmp_path / "delayed.json"
    path.write_text(
        json.dumps({"batch_id": "delayed", "records": [sample_record()]}), encoding="utf-8"
    )
    reached_scope_lock = Event()
    original_ensure_scope = module.ensure_scope

    def signaled_ensure_scope(*args, **kwargs):
        reached_scope_lock.set()
        return original_ensure_scope(*args, **kwargs)

    monkeypatch.setattr(module, "ensure_scope", signaled_ensure_scope)
    monkeypatch.setattr(module, "apply_migrations", lambda _connection: None)
    with psycopg.connect(dsn) as holding:
        holding.execute(
            """SELECT 1 FROM dispatch_replay_snapshots
               WHERE namespace_id = %s AND snapshot_id = %s FOR UPDATE""",
            ("local-received", stream),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(load, path, dsn=dsn, stream_id=stream)
            assert reached_scope_lock.wait(5)
            before_release = holding.execute("SELECT clock_timestamp()").fetchone()[0]
            holding.commit()
            report = future.result(timeout=5)
    assert datetime.fromisoformat(report["received_at"]) >= before_release


def test_new_text_candidate_is_visible_after_received_watermark(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-new-text-{uuid4()}"
    path = tmp_path / "candidate.json"
    path.write_text(
        json.dumps(
            {
                "batch_id": "baseline",
                "records": [{**sample_record("baseline"), "value_raw": "Норма"}],
            }
        ),
        encoding="utf-8",
    )
    assert load(path, dsn=dsn, stream_id=stream)["rows"] == 1
    client = TestClient(
        create_app(
            Settings(
                mode="received", db_dsn=app_dsn(dsn), received_stream_id=stream, _env_file=None
            )
        )
    )
    assert client.get("/api/v1/capabilities").json()["received_rows"] == 1
    normal_ids = [f"normal-{index}" for index in range(40)]
    normal_uids = [row_uid("local-received", stream, "new", item) for item in normal_ids]
    for index in range(1000):
        text_id = f"text-{index}"
        text_uid = row_uid("local-received", stream, "new", text_id)
        if sum(uid < text_uid for uid in normal_uids) >= 25:
            break
    else:
        raise AssertionError("could not place the text candidate beyond the first page")
    path.write_text(
        json.dumps(
            {
                "batch_id": "new",
                "records": [
                    *[
                        {**sample_record(item), "object_id": "normal-only", "value_raw": "Норма"}
                        for item in normal_ids
                    ],
                    {**sample_record(text_id), "object_id": "text-only"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load(path, dsn=dsn, stream_id=stream)["rows"] == 41
    updates = client.get("/api/v1/capabilities", params={"after_received_watermark": 1}).json()
    assert (
        updates["received_rows_after_watermark"],
        updates["received_source_alarms_after_watermark"],
        updates["received_candidates_after_watermark"],
    ) == (41, 0, 1)
    first_page = client.get(
        "/api/v1/attention",
        params={"view": "all", "after_received_watermark": 1, "limit": 25},
    ).json()
    assert first_page["total"] == 41
    assert all(item["message"]["row_uid"] != text_uid for item in first_page["items"])
    new_candidates = client.get(
        "/api/v1/attention", params={"view": "attention", "after_received_watermark": 1}
    ).json()
    assert new_candidates["total"] == new_candidates["watch_text_count"] == 1
    assert new_candidates["source_alarm_count"] == 0
    assert new_candidates["received_after_watermark"] == 1
    assert new_candidates["items"][0]["message"]["row_uid"] == text_uid
    assert new_candidates["items"][0]["message"]["alarm"] is False
    object_groups = {
        item["object_id"]: item for item in client.get("/api/v1/attention/objects").json()["items"]
    }
    assert object_groups["text-only"]["attention_band"] == "watch_text"
    assert object_groups["normal-only"]["attention_band"] == "chronological"
    assert (
        client.get("/api/v1/capabilities", params={"after_received_watermark": 42}).json()[
            "received_candidates_after_watermark"
        ]
        == 0
    )


def test_new_candidate_group_count_distinguishes_repeats_and_objects(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-new-groups-{uuid4()}"
    path = tmp_path / "groups.json"
    path.write_text(
        json.dumps(
            {
                "batch_id": "baseline",
                "records": [{**sample_record("baseline"), "value_raw": "Норма"}],
            }
        ),
        encoding="utf-8",
    )
    load(path, dsn=dsn, stream_id=stream)
    path.write_text(
        json.dumps(
            {
                "batch_id": "repeats-and-objects",
                "records": [
                    {
                        **sample_record(f"alarm-{index}"),
                        "object_id": "object-a"
                        if index < 4
                        else "object-b"
                        if index == 4
                        else None,
                        "alarm": True,
                        "value_raw": "Неисправен",
                    }
                    for index in range(6)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    load(path, dsn=dsn, stream_id=stream)
    client = TestClient(
        create_app(
            Settings(
                mode="received", db_dsn=app_dsn(dsn), received_stream_id=stream, _env_file=None
            )
        )
    )
    updates = client.get("/api/v1/capabilities", params={"after_received_watermark": 1}).json()
    assert (
        updates["received_rows_after_watermark"],
        updates["received_source_alarms_after_watermark"],
        updates["received_candidates_after_watermark"],
        updates["received_candidate_groups_after_watermark"],
    ) == (6, 6, 6, 3)
    current = client.get("/api/v1/capabilities", params={"after_received_watermark": 7}).json()
    assert current["received_candidate_groups_after_watermark"] == 0


def test_candidate_lanes_keep_received_watermark_after_late_commit(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-lanes-{uuid4()}"
    path = tmp_path / "lanes.json"
    path.write_text(
        json.dumps(
            {
                "batch_id": "first",
                "records": [
                    {
                        **sample_record("old-alarm"),
                        "sensor_type": "Состояние насоса",
                        "value_raw": "Неисправен",
                        "alarm": True,
                    },
                    {
                        **sample_record("old-text"),
                        "sensor_type": "Состояние вентилятора",
                        "value_raw": "Обесточен",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert load(path, dsn=dsn, stream_id=stream)["rows"] == 2
    client = TestClient(
        create_app(
            Settings(
                mode="received", db_dsn=app_dsn(dsn), received_stream_id=stream, _env_file=None
            )
        )
    )
    first = client.get("/api/v1/attention", params={"view": "attention"}).json()
    assert (first["total"], first["source_alarm_count"], first["watch_text_count"]) == (
        2,
        1,
        1,
    )
    assert first["received_watermark"] == 2
    as_of = first["as_of"]
    committed_before = datetime.fromisoformat(as_of) - timedelta(microseconds=1)

    # Commit after the first page, with an availability time inside its as_of.
    # The watermark, rather than the clock alone, must hold both overview lanes.
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """INSERT INTO dispatch_received_batches
               (namespace_id, snapshot_id, batch_id, source_sha256, source_name,
                received_at, row_count, alarm_count)
               VALUES (%s, %s, 'late', %s, 'late.json', %s, 2, 1)""",
            ("local-received", stream, "f" * 64, committed_before),
        )
        for position, (source_id, sensor_type, value_raw, alarm) in enumerate(
            (
                ("new-alarm", "Состояние насоса", "Неисправен", True),
                ("new-text", "Состояние вентилятора", "Обесточен", False),
            ),
            start=3,
        ):
            connection.execute(
                """INSERT INTO dispatch_observations
                   (namespace_id, snapshot_id, row_uid, channel_id, sensor_type,
                    value_raw, alarm, event_at, available_at, availability_basis,
                    source_file, source_sha256, record_ordinal, event_local_raw,
                    received_position)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                           'observed', 'late.json', %s, %s, %s, %s)""",
                (
                    "local-received",
                    stream,
                    row_uid("local-received", stream, "late", source_id),
                    source_id,
                    sensor_type,
                    value_raw,
                    alarm,
                    datetime(2026, 9, 24, tzinfo=UTC),
                    committed_before,
                    "f" * 64,
                    position - 2,
                    "2026-09-24T00:00:00+00:00",
                    position,
                ),
            )
        connection.execute(
            """UPDATE dispatch_replay_snapshots
               SET row_count = row_count + 2, alarm_count = alarm_count + 1,
                   last_received_at = %s
               WHERE namespace_id = %s AND snapshot_id = %s""",
            (committed_before, "local-received", stream),
        )

    pinned = {"view": "attention", "as_of": as_of, "received_watermark": 2, "limit": 1}
    unpinned = {"view": "attention", "as_of": as_of, "limit": 1}
    for alarm, old_id, new_id in (
        (True, "old-alarm", "new-alarm"),
        (False, "old-text", "new-text"),
    ):
        old_lane = client.get("/api/v1/attention", params={**pinned, "alarm": alarm}).json()
        new_lane = client.get("/api/v1/attention", params={**unpinned, "alarm": alarm}).json()
        assert (old_lane["total"], old_lane["received_watermark"]) == (1, 2)
        assert old_lane["items"][0]["message"]["row_uid"] == row_uid(
            "local-received", stream, "first", old_id
        )
        assert (new_lane["total"], new_lane["received_watermark"]) == (2, 4)
        assert new_lane["items"][0]["message"]["row_uid"] == row_uid(
            "local-received", stream, "late", new_id
        )


def test_object_groups_are_filtered_and_paged_before_response(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-object-pages-{uuid4()}"
    path = tmp_path / "object-pages.json"
    path.write_text(
        json.dumps(
            {
                "batch_id": "many-objects",
                "records": [
                    {
                        **sample_record(f"object-{index:02d}"),
                        "object_id": f"object-{index:02d}",
                        "channel_id": f"channel-{index:02d}",
                        "value_raw": "Обнаружен газ" if index in (2, 3) else "Норма",
                        "alarm": index in (0, 1),
                    }
                    for index in range(32)
                ]
                + [
                    {
                        **sample_record("object-00-text"),
                        "object_id": "object-00",
                        "channel_id": "channel-00",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert load(path, dsn=dsn, stream_id=stream)["rows"] == 33
    client = TestClient(
        create_app(
            Settings(
                mode="received", db_dsn=app_dsn(dsn), received_stream_id=stream, _env_file=None
            )
        )
    )
    first = client.get("/api/v1/attention/objects", params={"limit": 25}).json()
    second = client.get("/api/v1/attention/objects", params={"offset": 25, "limit": 25}).json()
    assert (first["total"], first["offset"], first["limit"], first["candidate_kind"]) == (
        32,
        0,
        25,
        "all",
    )
    assert second["total"] == 32
    assert (len(first["items"]), len(second["items"])) == (25, 7)
    assert len({item["object_id"] for page in (first, second) for item in page["items"]}) == 32
    for kind, expected in (("alarm", 2), ("text", 3), ("candidate", 4)):
        page = client.get("/api/v1/attention/objects", params={"candidate_kind": kind}).json()
        assert page["total"] == len(page["items"]) == expected
        assert page["candidate_kind"] == kind
        if kind == "text":
            assert "object-00" in {item["object_id"] for item in page["items"]}
    assert client.get("/api/v1/attention/objects", params={"limit": 101}).status_code == 422
    off_page = client.get("/api/v1/attention/channels", params={"object_id": "object-31"}).json()
    assert off_page["total"] == 1
    assert off_page["items"][0]["channel_id"] == "channel-31"


def test_object_and_channel_keep_candidate_times_separate_from_latest_record(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-candidate-times-{uuid4()}"
    path = tmp_path / "candidate-times.json"
    received_times = []
    for batch_id, value_raw, alarm in (
        ("alarm", "Норма", True),
        ("text", "Обнаружен газ", False),
        ("normal", "Норма", False),
    ):
        path.write_text(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "records": [
                        {
                            **sample_record(batch_id),
                            "object_id": "candidate-time-object",
                            "value_raw": value_raw,
                            "alarm": alarm,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        received_times.append(
            datetime.fromisoformat(load(path, dsn=dsn, stream_id=stream)["received_at"])
        )
    client = TestClient(
        create_app(
            Settings(
                mode="received", db_dsn=app_dsn(dsn), received_stream_id=stream, _env_file=None
            )
        )
    )
    objects = client.get("/api/v1/attention/objects").json()
    assert objects["total"] == 1
    item = objects["items"][0]
    assert (item["record_count"], item["candidate_count"], item["source_alarm_count"]) == (
        3,
        2,
        1,
    )
    assert item["attention_band"] == "source_alarm"
    assert datetime.fromisoformat(item["last_available_at"]) == received_times[2]
    assert datetime.fromisoformat(item["last_source_alarm_at"]) == received_times[0]
    assert datetime.fromisoformat(item["last_watch_text_at"]) == received_times[1]
    channels = client.get(
        "/api/v1/attention/channels", params={"object_id": "candidate-time-object"}
    ).json()
    assert channels["total"] == 1
    channel = channels["items"][0]
    assert (
        datetime.fromisoformat(channel["last_received_message"]["available_at"])
        == received_times[2]
    )
    assert datetime.fromisoformat(channel["last_source_alarm_at"]) == received_times[0]
    assert datetime.fromisoformat(channel["last_watch_text_at"]) == received_times[1]


def test_object_and_channel_order_uses_candidate_time_not_later_normal(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-candidate-order-{uuid4()}"
    path = tmp_path / "candidate-order.json"
    received_times = []
    for batch_id, object_id, channel_id, value_raw, alarm in (
        ("a-alarm", "object-a", "channel-a", "Норма", True),
        ("a-text", "object-a", "channel-b", "Обнаружен газ", False),
        ("b-text", "object-b", "channel-c", "Обнаружен газ", False),
        ("a-normal", "object-a", "channel-a", "Норма", False),
    ):
        path.write_text(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "records": [
                        {
                            **sample_record(batch_id),
                            "object_id": object_id,
                            "channel_id": channel_id,
                            "value_raw": value_raw,
                            "alarm": alarm,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        received_times.append(
            datetime.fromisoformat(load(path, dsn=dsn, stream_id=stream)["received_at"])
        )
    assert received_times == sorted(received_times)
    client = TestClient(
        create_app(
            Settings(
                mode="received", db_dsn=app_dsn(dsn), received_stream_id=stream, _env_file=None
            )
        )
    )
    objects = client.get("/api/v1/attention/objects").json()
    assert [item["object_id"] for item in objects["items"]] == ["object-b", "object-a"]
    assert datetime.fromisoformat(objects["items"][0]["last_watch_text_at"]) == received_times[2]
    assert datetime.fromisoformat(objects["items"][1]["last_available_at"]) == received_times[3]
    channels = client.get("/api/v1/attention/channels", params={"object_id": "object-a"}).json()
    assert [item["channel_id"] for item in channels["items"]] == ["channel-b", "channel-a"]
    assert (
        datetime.fromisoformat(channels["items"][1]["last_received_message"]["available_at"])
        == received_times[3]
    )


def test_channel_candidate_filter_runs_before_pagination(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-channel-filter-{uuid4()}"
    records = [
        {
            **sample_record(f"alarm-{index}"),
            "object_id": "busy-object",
            "channel_id": f"alarm-channel-{index:02d}",
            "value_raw": "Норма",
            "alarm": True,
        }
        for index in range(30)
    ]
    records.extend(
        [
            {
                **sample_record("text"),
                "object_id": "busy-object",
                "channel_id": "text-channel",
                "sensor_type": "Состояние фазы",
                "value_raw": "Обесточен",
                "alarm": False,
            },
            {
                **sample_record("normal"),
                "object_id": "busy-object",
                "channel_id": "normal-channel",
                "sensor_type": "Состояние фазы",
                "value_raw": "Норма",
                "alarm": False,
            },
        ]
    )
    path = tmp_path / "channel-filter.json"
    path.write_text(
        json.dumps({"batch_id": "channel-filter", "records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert load(path, dsn=dsn, stream_id=stream)["rows"] == 32
    client = TestClient(
        create_app(
            Settings(
                mode="received", db_dsn=app_dsn(dsn), received_stream_id=stream, _env_file=None
            )
        )
    )
    base = {"object_id": "busy-object", "limit": 25, "received_watermark": 32}
    all_channels = client.get("/api/v1/attention/channels", params=base).json()
    assert all_channels["total"] == 32
    assert "text-channel" not in {item["channel_id"] for item in all_channels["items"]}
    text = client.get(
        "/api/v1/attention/channels", params={**base, "candidate_kind": "text"}
    ).json()
    assert text["total"] == 1
    assert text["items"][0]["channel_id"] == "text-channel"
    assert (text["items"][0]["record_count"], text["items"][0]["candidate_count"]) == (1, 1)
    alarms = client.get(
        "/api/v1/attention/channels", params={**base, "candidate_kind": "alarm"}
    ).json()
    assert alarms["total"] == 30
    assert len(alarms["items"]) == 25
    candidates = client.get(
        "/api/v1/attention/channels", params={**base, "candidate_kind": "candidate"}
    ).json()
    assert candidates["total"] == 31
    assert (
        client.get(
            "/api/v1/attention/channels", params={**base, "candidate_kind": "text", "offset": 1}
        ).json()["total"]
        == 1
    )
    assert (
        client.get(
            "/api/v1/attention/channels", params={**base, "candidate_kind": "unknown"}
        ).status_code
        == 422
    )


def test_review_rejects_later_record_inside_displayed_received_prefix(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-prefix-{uuid4()}"
    path = tmp_path / "prefix.json"
    path.write_text(
        json.dumps(
            {"batch_id": "prefix", "records": [sample_record("first"), sample_record("second")]}
        ),
        encoding="utf-8",
    )
    assert load(path, dsn=dsn, stream_id=stream)["rows"] == 2
    client = TestClient(
        create_app(
            Settings(
                mode="received",
                db_dsn=app_dsn(dsn),
                received_stream_id=stream,
                enable_local_reviews=True,
                _env_file=None,
            )
        )
    )
    shown = client.get("/api/v1/attention", params={"view": "all"}).json()
    assert shown["received_watermark"] == 2
    with psycopg.connect(dsn) as connection:
        second_uid = connection.execute(
            """SELECT row_uid FROM dispatch_observations
               WHERE namespace_id = %s AND snapshot_id = %s AND received_position = 2""",
            ("local-received", stream),
        ).fetchone()[0]
        connection.execute(
            """UPDATE dispatch_observations SET available_at = %s
               WHERE namespace_id = %s AND snapshot_id = %s AND received_position = 1""",
            (
                datetime.fromisoformat(shown["as_of"]) + timedelta(minutes=1),
                "local-received",
                stream,
            ),
        )
    response = client.post(
        f"/api/v1/attention/{second_uid}/reviews",
        json={
            "idempotency_key": str(uuid4()),
            "expected_revision": 0,
            "view_as_of": shown["as_of"],
            "displayed_received_watermark": 2,
            "displayed_snapshot_id": stream,
            "displayed_policy_version": shown["policy_version"],
            "action_text": "Проверили запись",
            "result_text": "Физический исход неизвестен",
            "reason_text": "Синтетическая проверка времени внутри среза",
        },
    )
    assert response.status_code == 422
    assert "newer than the displayed time" in response.json()["detail"]
    assert client.get(f"/api/v1/attention/{second_uid}/reviews").json()["revision"] == 0


def test_local_received_import_to_queue_map_and_journal(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-{uuid4()}"
    before = datetime.now(UTC)
    path = tmp_path / "received.json"
    records = [
        {**sample_record("alarm"), "alarm": True, "object_id": "test-object"},
        {**sample_record("text"), "source_record_id": "text", "object_id": "test-object"},
        {
            **sample_record("plain"),
            "channel_id": "channel-2",
            "value_raw": "Норма",
        },
    ]
    path.write_text(json.dumps({"batch_id": "b1", "records": records}), encoding="utf-8")
    imported = load(path, dsn=dsn, stream_id=stream)
    assert imported["rows"] == 3
    assert imported["alarms"] == 1
    assert imported["repeated"] is False
    assert load(path, dsn=dsn, stream_id=stream)["repeated"] is True

    client = TestClient(
        create_app(
            Settings(
                mode="received",
                db_dsn=app_dsn(dsn),
                received_stream_id=stream,
                enable_local_reviews=True,
                _env_file=None,
            )
        )
    )
    assert client.get("/health/ready").status_code == 200
    capabilities = client.get("/api/v1/capabilities").json()
    assert capabilities["received_rows"] == 3
    assert capabilities["received_after_watermark"] is None
    assert capabilities["inference_ready"] is False
    initial_updates = client.get(
        "/api/v1/capabilities", params={"after_received_watermark": 3}
    ).json()
    assert (
        initial_updates["received_after_watermark"],
        initial_updates["received_rows_after_watermark"],
        initial_updates["received_source_alarms_after_watermark"],
        initial_updates["received_candidates_after_watermark"],
    ) == (3, 0, 0, 0)
    assert (
        client.get(
            "/api/v1/attention",
            params={"view": "all", "alarm": "true", "after_received_watermark": 3},
        ).json()["total"]
        == 0
    )
    assert (
        client.get("/api/v1/attention", params={"as_of": before.isoformat()}).json()["total"] == 0
    )
    queue = client.get("/api/v1/attention").json()
    assert queue["mode"] == "received"
    assert queue["total"] == 2
    assert queue["all_records_total"] == 3
    assert (
        queue["source_alarm_count"],
        queue["watch_text_count"],
        queue["chronological_count"],
    ) == (1, 1, 0)
    assert [item["attention_band"] for item in queue["items"]] == [
        "source_alarm",
        "watch_text",
    ]
    alarm_candidates = client.get("/api/v1/attention", params={"alarm": "true"}).json()
    text_candidates = client.get("/api/v1/attention", params={"alarm": "false"}).json()
    assert alarm_candidates["total"] == text_candidates["total"] == 1
    assert alarm_candidates["items"][0]["attention_band"] == "source_alarm"
    assert text_candidates["items"][0]["attention_band"] == "watch_text"
    assert text_candidates["all_records_total"] == 2  # includes the noncandidate normal record
    all_page = client.get(
        "/api/v1/attention", params={"view": "all", "offset": 1, "limit": 1}
    ).json()
    assert all_page["total"] == 3
    assert len(all_page["items"]) == 1
    assert (
        all_page["source_alarm_count"],
        all_page["watch_text_count"],
        all_page["chronological_count"],
    ) == (1, 1, 1)
    assert all(item["message"]["availability_basis"] == "observed" for item in queue["items"])
    uid = queue["items"][0]["message"]["row_uid"]
    source_alarms = client.get("/api/v1/attention", params={"view": "all", "alarm": "true"}).json()
    assert source_alarms["total"] == source_alarms["all_records_total"] == 1
    assert (
        source_alarms["source_alarm_count"],
        source_alarms["watch_text_count"],
        source_alarms["chronological_count"],
    ) == (1, 0, 0)
    assert source_alarms["items"][0]["message"]["row_uid"] == uid
    assert (
        client.get(
            "/api/v1/attention",
            params={"view": "all", "alarm": "true", "object_id": "test-object"},
        ).json()["total"]
        == 1
    )
    assert (
        client.get(
            "/api/v1/attention",
            params={"view": "all", "alarm": "true", "object_id": "__unknown__"},
        ).json()["total"]
        == 0
    )
    assert (
        client.get("/api/v1/attention", params={"view": "all", "alarm": "false"}).json()["total"]
        == 2
    )
    without_local_reviews = TestClient(
        create_app(
            Settings(
                mode="received", db_dsn=app_dsn(dsn), received_stream_id=stream, _env_file=None
            )
        )
    )
    assert (
        without_local_reviews.get(
            "/api/v1/attention", params={"view": "all", "alarm": "true"}
        ).json()["total"]
        == 1
    )
    assert without_local_reviews.get("/api/v1/review-journal").status_code == 503
    exact_source = client.get("/api/v1/attention", params={"view": "all", "row_uid": uid}).json()
    assert exact_source["total"] == exact_source["all_records_total"] == 1
    assert exact_source["items"][0]["message"]["row_uid"] == uid
    same_source_time = client.get(
        "/api/v1/attention",
        params={
            "view": "all",
            "object_id": "test-object",
            "channel_id": "channel-1",
            "event_at": records[0]["event_at"],
        },
    ).json()
    assert same_source_time["total"] == 2
    assert {item["message"]["row_uid"] for item in same_source_time["items"]} == {
        item["message"]["row_uid"] for item in queue["items"]
    }
    assert (
        client.get(
            "/api/v1/attention",
            params={"view": "all", "event_at": "2026-09-24T11:00:00+03:00"},
        ).json()["total"]
        == 0
    )
    assert (
        client.get("/api/v1/attention", params={"event_at": "2026-09-24T10:00:00"}).status_code
        == 422
    )
    assert (
        client.get("/api/v1/attention", params={"view": "all", "row_uid": "missing-row"}).json()[
            "total"
        ]
        == 0
    )
    assert (
        client.get(
            "/api/v1/attention",
            params={"view": "all", "row_uid": uid, "as_of": before.isoformat()},
        ).json()["total"]
        == 0
    )
    assert queue["items"][0]["message"]["available_at"] != records[0]["event_at"]
    objects = client.get("/api/v1/attention/objects").json()
    assert objects["mode"] == "received"
    assert objects["total"] == 2
    channel_page = client.get(
        "/api/v1/attention/channels", params={"object_id": "test-object"}
    ).json()
    assert channel_page["total"] == 1
    assert channel_page["items"][0]["channel_id"] == "channel-1"
    assert channel_page["items"][0]["record_count"] == 2
    assert channel_page["items"][0]["last_received_group_count"] == 2
    assert channel_page["items"][0]["source_alarm_count"] == 1
    selected_channel = client.get(
        "/api/v1/attention/channels",
        params={"object_id": "test-object", "channel_id": "channel-1"},
    ).json()
    assert selected_channel["total"] == 1
    assert selected_channel["items"][0]["channel_id"] == "channel-1"
    assert (
        client.get(
            "/api/v1/attention/channels",
            params={"object_id": "test-object", "channel_id": "channel-2"},
        ).json()["total"]
        == 0
    )
    unknown_page = client.get(
        "/api/v1/attention/channels", params={"object_id": "__unknown__"}
    ).json()
    assert unknown_page["total"] == 1
    assert unknown_page["items"][0]["last_received_message"]["value_raw"] == "Норма"
    channel_history = client.get(
        "/api/v1/attention",
        params={"object_id": "__unknown__", "channel_id": "channel-2", "view": "all"},
    ).json()
    assert channel_history["total"] == 1
    assert channel_history["items"][0]["message"]["value_raw"] == "Норма"

    payload = {
        "idempotency_key": str(uuid4()),
        "expected_revision": 0,
        "view_as_of": queue["as_of"],
        "displayed_received_watermark": queue["received_watermark"],
        "displayed_snapshot_id": stream,
        "displayed_policy_version": queue["policy_version"],
        "action_text": "Проверили канал",
        "result_text": "Причина не установлена",
        "reason_text": "Тестовая запись",
    }
    missing_watermark = {
        key: value for key, value in payload.items() if key != "displayed_received_watermark"
    }
    assert (
        client.post(f"/api/v1/attention/{uid}/reviews", json=missing_watermark).status_code == 422
    )
    assert (
        client.post(
            f"/api/v1/attention/{uid}/reviews",
            json={**payload, "displayed_received_watermark": 0},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/attention/{uid}/reviews",
            json={**payload, "displayed_received_watermark": 4},
        ).status_code
        == 422
    )
    saved = client.post(f"/api/v1/attention/{uid}/reviews", json=payload)
    assert saved.status_code == 201, saved.text
    assert saved.json()["actor_id"] == "local-operator"
    assert saved.json()["displayed_received_watermark"] == 3
    retried = client.post(f"/api/v1/attention/{uid}/reviews", json=payload)
    assert retried.status_code == 201
    assert retried.json() == saved.json()
    reused_key = client.post(
        f"/api/v1/attention/{uid}/reviews",
        json={**payload, "result_text": "Другой результат"},
    )
    assert reused_key.status_code == 409
    assert "idempotency key" in reused_key.json()["detail"]
    stale_revision = client.post(
        f"/api/v1/attention/{uid}/reviews",
        json={**payload, "idempotency_key": str(uuid4())},
    )
    assert stale_revision.status_code == 409
    assert "current 1" in stale_revision.json()["detail"]
    assert client.get(f"/api/v1/attention/{uid}/reviews").json()["revision"] == 1
    reviewed_queue = client.get("/api/v1/attention").json()
    assert (
        reviewed_queue["source_alarm_count"],
        reviewed_queue["watch_text_count"],
        reviewed_queue["chronological_count"],
    ) == (1, 1, 0)
    reviewed_entry = next(
        item for item in reviewed_queue["items"] if item["message"]["row_uid"] == uid
    )
    assert reviewed_entry["message"]["alarm"] is True
    assert reviewed_entry["local_review_revision"] == 1
    assert reviewed_entry["local_last_review_at"] is not None
    assert reviewed_entry["attention_band"] == "source_alarm"
    with_note = client.get("/api/v1/attention", params={"local_note": "present"}).json()
    without_note = client.get("/api/v1/attention", params={"local_note": "absent"}).json()
    assert with_note["total"] == without_note["total"] == 1
    assert with_note["all_records_total"] == without_note["all_records_total"] == 3
    assert (
        with_note["source_alarm_count"],
        with_note["watch_text_count"],
        with_note["chronological_count"],
    ) == (1, 0, 0)
    assert (
        without_note["source_alarm_count"],
        without_note["watch_text_count"],
        without_note["chronological_count"],
    ) == (0, 1, 0)
    assert [item["message"]["row_uid"] for item in with_note["items"]] == [uid]
    assert with_note["items"][0]["message"]["alarm"] is True
    assert without_note["items"][0]["attention_band"] == "watch_text"
    assert (
        client.get("/api/v1/attention", params={"view": "all", "local_note": "absent"}).json()[
            "total"
        ]
        == 2
    )
    assert (
        client.get(
            "/api/v1/attention", params={"view": "all", "local_note": "present", "row_uid": uid}
        ).json()["total"]
        == 1
    )
    assert (
        client.get(
            "/api/v1/attention", params={"view": "all", "local_note": "absent", "row_uid": uid}
        ).json()["total"]
        == 0
    )
    second_unnoted = client.get(
        "/api/v1/attention",
        params={"view": "all", "local_note": "absent", "limit": 1, "offset": 1},
    ).json()
    assert second_unnoted["total"] == 2
    assert len(second_unnoted["items"]) == 1
    assert second_unnoted["items"][0]["review_order"] == 2
    assert client.get("/api/v1/attention", params={"local_note": "invalid"}).status_code == 422
    journal = client.get("/api/v1/review-journal").json()
    assert journal["mode"] == "received"
    assert journal["total"] == 1
    assert journal["items"][0]["message"]["row_uid"] == uid
    selected_journal = client.get("/api/v1/review-journal", params={"row_uid": uid}).json()
    assert selected_journal["total"] == 1
    assert selected_journal["items"][0]["note"]["row_uid"] == uid
    assert selected_journal["items"][0]["note"]["displayed_received_watermark"] == 3
    assert (
        client.get("/api/v1/review-journal", params={"row_uid": "another-source-record"}).json()[
            "total"
        ]
        == 0
    )
    with psycopg.connect(dsn) as connection:
        audit = connection.execute(
            "SELECT count(*) FROM replay_review_audit WHERE namespace_id = %s AND snapshot_id = %s",
            ("local-received", stream),
        ).fetchone()[0]
    assert audit == 1

    fixed_all_page = client.get(
        "/api/v1/attention",
        params={
            "view": "all",
            "as_of": queue["as_of"],
            "received_watermark": queue["received_watermark"],
            "limit": 1,
            "offset": 1,
        },
    ).json()
    fixed_candidate_page = client.get(
        "/api/v1/attention",
        params={
            "as_of": queue["as_of"],
            "received_watermark": queue["received_watermark"],
            "limit": 1,
            "offset": 1,
        },
    ).json()

    path.write_text(
        json.dumps(
            {
                "batch_id": "b2",
                "records": [
                    {
                        **sample_record("next"),
                        "channel_id": "channel-2",
                        "event_at": "2026-09-24T09:00:00+03:00",
                        "alarm": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load(path, dsn=dsn, stream_id=stream)["rows"] == 1
    assert client.get("/api/v1/capabilities").json()["received_rows"] == 4
    updates = client.get("/api/v1/capabilities", params={"after_received_watermark": 3}).json()
    assert (
        updates["received_after_watermark"],
        updates["received_rows_after_watermark"],
        updates["received_source_alarms_after_watermark"],
        updates["received_candidates_after_watermark"],
    ) == (3, 1, 1, 1)
    assert (
        client.get("/api/v1/capabilities", params={"after_received_watermark": 4}).json()[
            "received_source_alarms_after_watermark"
        ]
        == 0
    )
    assert (
        client.get("/api/v1/capabilities", params={"after_received_watermark": 5}).status_code
        == 422
    )
    new_source_alarms = client.get(
        "/api/v1/attention",
        params={"view": "all", "alarm": "true", "after_received_watermark": 3},
    ).json()
    assert (
        new_source_alarms["received_after_watermark"],
        new_source_alarms["received_watermark"],
        new_source_alarms["total"],
        new_source_alarms["all_records_total"],
    ) == (3, 4, 1, 1)
    assert new_source_alarms["items"][0]["message"]["row_uid"] == row_uid(
        "local-received", stream, "b2", "next"
    )
    assert (
        client.get(
            "/api/v1/attention",
            params={
                "view": "all",
                "alarm": "true",
                "after_received_watermark": 3,
                "received_watermark": 3,
            },
        ).json()["total"]
        == 0
    )
    assert (
        client.get(
            "/api/v1/attention",
            params={"after_received_watermark": 4, "received_watermark": 3},
        ).status_code
        == 422
    )
    assert client.get("/api/v1/attention").json()["all_records_total"] == 4
    later_boundary = client.post(
        f"/api/v1/attention/{uid}/reviews",
        json={
            **payload,
            "idempotency_key": str(uuid4()),
            "expected_revision": 1,
            "displayed_received_watermark": 4,
        },
    )
    assert later_boundary.status_code == 422
    assert "newer than the displayed time" in later_boundary.json()["detail"]
    archived_note = client.get("/api/v1/review-journal", params={"row_uid": uid}).json()["items"][
        0
    ]["note"]
    archived_card = client.get(
        "/api/v1/attention",
        params={
            "view": "all",
            "row_uid": uid,
            "as_of": archived_note["view_as_of"],
            "received_watermark": archived_note["displayed_received_watermark"],
        },
    ).json()
    assert archived_card["total"] == 1
    assert archived_card["items"][0]["message"]["row_uid"] == uid
    after_all_page = client.get(
        "/api/v1/attention",
        params={
            "view": "all",
            "as_of": queue["as_of"],
            "received_watermark": queue["received_watermark"],
            "limit": 1,
            "offset": 1,
        },
    ).json()
    after_candidate_page = client.get(
        "/api/v1/attention",
        params={
            "as_of": queue["as_of"],
            "received_watermark": queue["received_watermark"],
            "limit": 1,
            "offset": 1,
        },
    ).json()
    assert after_all_page["total"] == fixed_all_page["total"] == 3
    assert after_candidate_page["total"] == fixed_candidate_page["total"] == 2
    assert after_all_page["items"] == fixed_all_page["items"]
    assert after_candidate_page["items"] == fixed_candidate_page["items"]
    live_candidate_page = client.get("/api/v1/attention", params={"limit": 1, "offset": 1}).json()
    assert (
        live_candidate_page["items"][0]["message"]["row_uid"]
        != fixed_candidate_page["items"][0]["message"]["row_uid"]
    )
    unknown_after = client.get(
        "/api/v1/attention/channels", params={"object_id": "__unknown__"}
    ).json()
    assert unknown_after["items"][0]["source_time_regressed"] is True
    assert unknown_after["items"][0]["last_received_message"]["alarm"] is True
    assert client.get("/api/v1/review-journal").json()["total"] == 1

    changed = {"batch_id": "b1", "records": [sample_record("changed")]}
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="different content"):
        load(path, dsn=dsn, stream_id=stream)

    path.write_text(
        json.dumps(
            {
                "batch_id": "b3",
                "records": [
                    {
                        **sample_record("late-normal"),
                        "channel_id": "channel-2",
                        "value_raw": "Норма",
                        "event_at": "2026-09-24T08:00:00+03:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load(path, dsn=dsn, stream_id=stream)["rows"] == 1
    all_channel_rows = client.get(
        "/api/v1/attention",
        params={"object_id": "__unknown__", "channel_id": "channel-2", "view": "all"},
    ).json()
    assert all_channel_rows["total"] == 3
    assert [item["message"]["value_raw"] for item in all_channel_rows["items"]] == [
        "Норма",
        "Обнаружен газ",
        "Норма",
    ]
    assert (
        all_channel_rows["items"][0]["message"]["event_at"]
        < all_channel_rows["items"][1]["message"]["event_at"]
    )
    candidates = client.get("/api/v1/attention").json()
    assert candidates["items"][0]["attention_band"] == "source_alarm"
    before_conflict = candidates["as_of"]
    assert (
        client.get(
            "/api/v1/attention/channels",
            params={
                "object_id": "test-object",
                "channel_id": "channel-1",
                "as_of": before_conflict,
            },
        ).json()["items"][0]["multiple_object_ids_seen"]
        is False
    )

    path.write_text(
        json.dumps(
            {
                "batch_id": "b4",
                "records": [
                    {**sample_record("other-object"), "object_id": "other-object"},
                    {
                        **sample_record("mapped-object"),
                        "channel_id": "channel-2",
                        "object_id": "mapped-object",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load(path, dsn=dsn, stream_id=stream)["rows"] == 2
    for object_id, channel_id in (
        ("test-object", "channel-1"),
        ("other-object", "channel-1"),
        ("__unknown__", "channel-2"),
        ("mapped-object", "channel-2"),
    ):
        page = client.get(
            "/api/v1/attention/channels",
            params={"object_id": object_id, "channel_id": channel_id},
        ).json()
        assert page["total"] == 1
        assert page["items"][0]["multiple_object_ids_seen"] is True
    assert (
        client.get(
            "/api/v1/attention/channels",
            params={
                "object_id": "test-object",
                "channel_id": "channel-1",
                "as_of": before_conflict,
            },
        ).json()["items"][0]["multiple_object_ids_seen"]
        is False
    )


def test_received_watermark_excludes_batch_committed_after_first_page(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-{uuid4()}"
    path = tmp_path / "first.json"
    records = [{**sample_record(f"first-{number}"), "alarm": True} for number in range(2)]
    path.write_text(json.dumps({"batch_id": "first", "records": records}), encoding="utf-8")
    assert load(path, dsn=dsn, stream_id=stream)["rows"] == 2
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """ALTER TABLE dispatch_observations
               DROP CONSTRAINT dispatch_observations_received_position_check"""
        )
        connection.execute(
            """UPDATE dispatch_observations SET received_position = NULL
               WHERE namespace_id = %s AND snapshot_id = %s""",
            ("local-received", stream),
        )
        apply_migrations(connection)
        positions = connection.execute(
            """SELECT received_position FROM dispatch_observations
               WHERE namespace_id = %s AND snapshot_id = %s
               ORDER BY received_position""",
            ("local-received", stream),
        ).fetchall()
    assert [position[0] for position in positions] == [1, 2]
    client = TestClient(
        create_app(
            Settings(
                mode="received", db_dsn=app_dsn(dsn), received_stream_id=stream, _env_file=None
            )
        )
    )
    first_page = client.get(
        "/api/v1/attention", params={"view": "all", "limit": 1, "offset": 1}
    ).json()
    assert first_page["total"] == first_page["received_watermark"] == 2
    map_params = {"as_of": first_page["as_of"]}
    objects_before = client.get("/api/v1/attention/objects", params=map_params).json()
    channels_before = client.get(
        "/api/v1/attention/channels", params={**map_params, "object_id": "__unknown__"}
    ).json()
    assert objects_before["received_watermark"] == channels_before["received_watermark"] == 2
    assert objects_before["items"][0]["record_count"] == 2
    assert channels_before["items"][0]["record_count"] == 2
    committed_before = datetime.fromisoformat(first_page["as_of"]) - timedelta(microseconds=1)
    pending_records = (
        ("late-channel", "channel-late", None),
        ("same-channel-a", "channel-1", None),
        ("same-channel-b", "channel-1", None),
        ("other-object", "channel-1", "other-object"),
    )
    with psycopg.connect(dsn) as pending:
        pending.execute(
            """INSERT INTO dispatch_received_batches
               (namespace_id, snapshot_id, batch_id, source_sha256, source_name,
                received_at, row_count, alarm_count)
               VALUES (%s, %s, 'pending', %s, 'pending.json', %s, 4, 4)""",
            ("local-received", stream, "f" * 64, committed_before),
        )
        for ordinal, (record_id, channel_id, object_id) in enumerate(pending_records, start=1):
            pending.execute(
                """INSERT INTO dispatch_observations
                   (namespace_id, snapshot_id, row_uid, channel_id, object_id,
                    value_raw, alarm, event_at, available_at, availability_basis,
                    source_file, source_sha256, record_ordinal, event_local_raw,
                    received_position)
                   VALUES (%s, %s, %s, %s, %s, 'Проверить', true,
                           %s, %s, 'observed', 'pending.json', %s, %s, %s, %s)""",
                (
                    "local-received",
                    stream,
                    row_uid("local-received", stream, "pending", record_id),
                    channel_id,
                    object_id,
                    datetime(2026, 9, 24, tzinfo=UTC),
                    committed_before,
                    "f" * 64,
                    ordinal,
                    "2026-09-24T00:00:00+00:00",
                    ordinal + 2,
                ),
            )
        pending.execute(
            """UPDATE dispatch_replay_snapshots
               SET row_count = row_count + 4, alarm_count = alarm_count + 4,
                   last_received_at = %s
               WHERE namespace_id = %s AND snapshot_id = %s""",
            (committed_before, "local-received", stream),
        )
        assert (
            client.get(
                "/api/v1/attention",
                params={"view": "all", "as_of": first_page["as_of"], "limit": 1, "offset": 1},
            ).json()["items"]
            == first_page["items"]
        )
        pending.commit()

    pinned = client.get(
        "/api/v1/attention",
        params={
            "view": "all",
            "as_of": first_page["as_of"],
            "received_watermark": first_page["received_watermark"],
            "limit": 1,
            "offset": 1,
        },
    ).json()
    as_of_only = client.get(
        "/api/v1/attention",
        params={"view": "all", "as_of": first_page["as_of"], "limit": 1, "offset": 1},
    ).json()
    assert pinned["total"] == 2
    assert pinned["items"] == first_page["items"]
    assert as_of_only["total"] == 6
    assert as_of_only["items"] != first_page["items"]
    assert client.get("/api/v1/attention", params={"received_watermark": 7}).status_code == 422
    pinned_params = {**map_params, "received_watermark": 2}
    assert client.get("/api/v1/attention/objects", params=pinned_params).json() == objects_before
    assert (
        client.get(
            "/api/v1/attention/channels", params={**pinned_params, "object_id": "__unknown__"}
        ).json()
        == channels_before
    )
    fresh_objects = client.get("/api/v1/attention/objects", params=map_params).json()
    fresh_channels = client.get(
        "/api/v1/attention/channels", params={**map_params, "object_id": "__unknown__"}
    ).json()
    assert fresh_objects["received_watermark"] == fresh_channels["received_watermark"] == 6
    assert fresh_objects["total"] == 2
    assert sum(item["record_count"] for item in fresh_objects["items"]) == 6
    assert fresh_channels["total"] == 2
    channel_one = next(
        item for item in fresh_channels["items"] if item["channel_id"] == "channel-1"
    )
    assert channel_one["record_count"] == 4
    assert channel_one["last_received_group_count"] == 2
    assert channel_one["multiple_object_ids_seen"] is True
    assert (
        client.get(
            "/api/v1/attention/objects", params={**map_params, "received_watermark": 7}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/attention/channels",
            params={**map_params, "object_id": "__unknown__", "received_watermark": 7},
        ).status_code
        == 422
    )


def test_review_journal_filters_object_and_channel_with_server_pagination(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-journal-filter-{uuid4()}"
    path = tmp_path / "received.json"
    records = [
        {**sample_record("a-1"), "object_id": "object-A", "channel_id": "channel-1"},
        {**sample_record("a-2"), "object_id": "object-A", "channel_id": "channel-2"},
        {**sample_record("b-1"), "object_id": "object-B", "channel_id": "channel-1"},
        {**sample_record("unknown"), "channel_id": "channel-3"},
    ]
    path.write_text(json.dumps({"batch_id": "b1", "records": records}), encoding="utf-8")
    assert load(path, dsn=dsn, stream_id=stream)["rows"] == 4
    client = TestClient(
        create_app(
            Settings(
                mode="received",
                db_dsn=app_dsn(dsn),
                received_stream_id=stream,
                enable_local_reviews=True,
                _env_file=None,
            )
        )
    )
    queue = client.get("/api/v1/attention", params={"view": "all"}).json()
    for entry in queue["items"]:
        uid = entry["message"]["row_uid"]
        response = client.post(
            f"/api/v1/attention/{uid}/reviews",
            json={
                "idempotency_key": str(uuid4()),
                "expected_revision": 0,
                "view_as_of": queue["as_of"],
                "displayed_received_watermark": queue["received_watermark"],
                "displayed_snapshot_id": stream,
                "displayed_policy_version": queue["policy_version"],
                "action_text": "Проверили запись",
                "result_text": "Результат не установлен",
                "reason_text": "Синтетическая проверка фильтра",
            },
        )
        assert response.status_code == 201, response.text

    object_a = client.get("/api/v1/review-journal", params={"object_id": "object-A"}).json()
    assert object_a["total"] == 2
    assert {item["message"]["channel_id"] for item in object_a["items"]} == {
        "channel-1",
        "channel-2",
    }
    assert (
        client.get(
            "/api/v1/review-journal",
            params={"object_id": "object-A", "channel_id": "channel-1"},
        ).json()["total"]
        == 1
    )
    assert (
        client.get(
            "/api/v1/review-journal",
            params={"object_id": "object-B", "channel_id": "channel-1"},
        ).json()["total"]
        == 1
    )
    unknown = client.get("/api/v1/review-journal", params={"object_id": "__unknown__"}).json()
    assert unknown["total"] == 1
    assert unknown["items"][0]["message"]["object_id"] is None
    assert (
        client.get("/api/v1/review-journal", params={"channel_id": "channel-1"}).json()["total"]
        == 2
    )
    assert (
        client.get(
            "/api/v1/review-journal", params={"object_id": "object-A", "limit": 1, "offset": 1}
        ).json()["total"]
        == 2
    )
    a_uid = next(
        item["message"]["row_uid"]
        for item in queue["items"]
        if item["message"]["object_id"] == "object-A"
    )
    assert (
        client.get(
            "/api/v1/review-journal", params={"row_uid": a_uid, "object_id": "object-B"}
        ).json()["total"]
        == 0
    )

    with psycopg.connect(dsn) as connection:
        connection.execute(
            "ALTER TABLE replay_review_notes DROP CONSTRAINT replay_review_notes_position_check"
        )
        connection.execute(
            """UPDATE replay_review_notes SET review_position = NULL
               WHERE namespace_id = %s AND snapshot_id = %s""",
            ("local-received", stream),
        )
        connection.execute(
            """UPDATE dispatch_replay_snapshots SET review_count = 0
               WHERE namespace_id = %s AND snapshot_id = %s""",
            ("local-received", stream),
        )
        apply_migrations(connection)
        restored = connection.execute(
            """SELECT review_position FROM replay_review_notes
               WHERE namespace_id = %s AND snapshot_id = %s
               ORDER BY review_position""",
            ("local-received", stream),
        ).fetchall()
        restored_count = connection.execute(
            """SELECT review_count FROM dispatch_replay_snapshots
               WHERE namespace_id = %s AND snapshot_id = %s""",
            ("local-received", stream),
        ).fetchone()[0]
    assert [row[0] for row in restored] == [1, 2, 3, 4]
    assert restored_count == 4

    pending_note_id = uuid4()
    with psycopg.connect(dsn) as pending:
        position = pending.execute(
            """UPDATE dispatch_replay_snapshots
               SET review_count = review_count + 1
               WHERE namespace_id = %s AND snapshot_id = %s
               RETURNING review_count""",
            ("local-received", stream),
        ).fetchone()[0]
        assert position == 5
        pending.execute(
            """INSERT INTO replay_review_notes
               (note_id, namespace_id, snapshot_id, row_uid, revision,
                idempotency_key, payload_sha256, actor_id, action_text,
                result_text, reason_text, review_position)
               VALUES (%s, %s, %s, %s, 2, %s, %s, 'local-operator',
                       'Повторная проверка', 'Пока неизвестно', 'Синтетическая заметка', %s)""",
            (
                pending_note_id,
                "local-received",
                stream,
                a_uid,
                uuid4(),
                "f" * 64,
                position,
            ),
        )
        pending.execute(
            """INSERT INTO replay_review_audit
               (audit_id, note_id, namespace_id, snapshot_id, row_uid,
                actor_id, action_kind, payload_sha256)
               VALUES (%s, %s, %s, %s, %s, 'local-operator',
                       'review_note_created', %s)""",
            (uuid4(), pending_note_id, "local-received", stream, a_uid, "f" * 64),
        )
        first_second_page = client.get(
            "/api/v1/review-journal", params={"limit": 1, "offset": 1}
        ).json()
        assert first_second_page["total"] == first_second_page["review_watermark"] == 4
        pending.commit()

    pinned_second_page = client.get(
        "/api/v1/review-journal",
        params={"limit": 1, "offset": 1, "review_watermark": first_second_page["review_watermark"]},
    ).json()
    live_second_page = client.get("/api/v1/review-journal", params={"limit": 1, "offset": 1}).json()
    assert pinned_second_page["total"] == 4
    assert pinned_second_page["items"] == first_second_page["items"]
    assert live_second_page["total"] == 5
    assert live_second_page["items"] != first_second_page["items"]
    assert client.get("/api/v1/review-journal", params={"review_watermark": 6}).status_code == 422


def test_review_and_audit_roll_back_together_on_audit_failure(tmp_path):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-audit-{uuid4()}"
    path = tmp_path / "received.json"
    path.write_text(json.dumps({"batch_id": "b1", "records": [sample_record()]}), encoding="utf-8")
    load(path, dsn=dsn, stream_id=stream)
    client = TestClient(
        create_app(
            Settings(
                mode="received",
                db_dsn=app_dsn(dsn),
                received_stream_id=stream,
                enable_local_reviews=True,
                _env_file=None,
            )
        )
    )
    queue = client.get("/api/v1/attention", params={"view": "all"}).json()
    uid = queue["items"][0]["message"]["row_uid"]
    payload = {
        "idempotency_key": str(uuid4()),
        "expected_revision": 0,
        "view_as_of": queue["as_of"],
        "displayed_received_watermark": queue["received_watermark"],
        "displayed_snapshot_id": stream,
        "displayed_policy_version": queue["policy_version"],
        "action_text": "Проверили канал",
        "result_text": "Причина не установлена",
        "reason_text": "Синтетическая проверка rollback",
    }
    trigger_name = f"fail_audit_{uuid4().hex}"
    function_name = f"fail_audit_{uuid4().hex}"
    with psycopg.connect(dsn) as connection:
        connection.execute(
            sql.SQL(
                "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'synthetic audit failure'; END $$"
            ).format(sql.Identifier(function_name))
        )
        connection.execute(
            sql.SQL(
                "CREATE TRIGGER {} BEFORE INSERT ON replay_review_audit "
                "FOR EACH ROW WHEN (NEW.snapshot_id = {}) EXECUTE FUNCTION {}()"
            ).format(
                sql.Identifier(trigger_name), sql.Literal(stream), sql.Identifier(function_name)
            )
        )
    try:
        failed = client.post(f"/api/v1/attention/{uid}/reviews", json=payload)
        assert failed.status_code == 503
        assert failed.json()["detail"] == "replay_storage_unavailable"
        assert client.get(f"/api/v1/attention/{uid}/reviews").json()["revision"] == 0
        failed_journal = client.get("/api/v1/review-journal").json()
        assert failed_journal["total"] == failed_journal["review_watermark"] == 0
        with psycopg.connect(dsn) as connection:
            notes = connection.execute(
                "SELECT count(*) FROM replay_review_notes WHERE namespace_id = %s "
                "AND snapshot_id = %s",
                ("local-received", stream),
            ).fetchone()[0]
            audit = connection.execute(
                "SELECT count(*) FROM replay_review_audit WHERE namespace_id = %s "
                "AND snapshot_id = %s",
                ("local-received", stream),
            ).fetchone()[0]
        assert (notes, audit) == (0, 0)
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(
                sql.SQL("DROP TRIGGER {} ON replay_review_audit").format(
                    sql.Identifier(trigger_name)
                )
            )
            connection.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(function_name)))
    saved = client.post(f"/api/v1/attention/{uid}/reviews", json=payload)
    assert saved.status_code == 201, saved.text
    reloaded = TestClient(
        create_app(
            Settings(
                mode="received",
                db_dsn=app_dsn(dsn),
                received_stream_id=stream,
                enable_local_reviews=True,
                _env_file=None,
            )
        )
    )
    assert reloaded.get(f"/api/v1/attention/{uid}/reviews").json()["revision"] == 1
    assert reloaded.get("/api/v1/review-journal").json()["total"] == 1


@pytest.mark.parametrize("same_idempotency_key", [False, True])
def test_concurrent_reviews_keep_one_revision_and_audit(tmp_path, same_idempotency_key):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-concurrent-review-{uuid4()}"
    path = tmp_path / "received.json"
    path.write_text(json.dumps({"batch_id": "b1", "records": [sample_record()]}), encoding="utf-8")
    load(path, dsn=dsn, stream_id=stream)
    settings = Settings(
        mode="received",
        db_dsn=app_dsn(dsn),
        received_stream_id=stream,
        enable_local_reviews=True,
        _env_file=None,
    )
    client = TestClient(create_app(settings))
    queue = client.get("/api/v1/attention").json()
    uid = queue["items"][0]["message"]["row_uid"]
    payload = {
        "idempotency_key": str(uuid4()),
        "expected_revision": 0,
        "view_as_of": queue["as_of"],
        "displayed_received_watermark": queue["received_watermark"],
        "displayed_snapshot_id": stream,
        "displayed_policy_version": queue["policy_version"],
        "action_text": "Проверили канал",
        "result_text": "Причина не установлена",
        "reason_text": "Синтетическая проверка конкурентной записи",
    }
    barrier = Barrier(2)

    def submit(key: str):
        concurrent_client = TestClient(create_app(settings))
        barrier.wait(timeout=5)
        return concurrent_client.post(
            f"/api/v1/attention/{uid}/reviews", json={**payload, "idempotency_key": key}
        )

    keys = [payload["idempotency_key"]]
    keys.append(keys[0] if same_idempotency_key else str(uuid4()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, key) for key in keys]
        responses = [future.result(timeout=10) for future in futures]

    statuses = sorted(response.status_code for response in responses)
    assert statuses == ([201, 201] if same_idempotency_key else [201, 409])
    if same_idempotency_key:
        assert responses[0].json() == responses[1].json()
    reloaded = TestClient(create_app(settings))
    history = reloaded.get(f"/api/v1/attention/{uid}/reviews").json()
    journal = reloaded.get("/api/v1/review-journal").json()
    assert history["revision"] == 1
    assert len(history["items"]) == journal["total"] == journal["review_watermark"] == 1
    with psycopg.connect(dsn) as connection:
        audit = connection.execute(
            "SELECT count(*) FROM replay_review_audit WHERE namespace_id = %s AND snapshot_id = %s",
            ("local-received", stream),
        ).fetchone()[0]
    assert audit == 1


def test_local_inbox_scan_reports_and_recovers_invalid_batches(tmp_path, monkeypatch):
    dsn = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL integration DSN not provided")
    stream = f"synthetic-inbox-{uuid4()}"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    initialize_stream(dsn, stream_id=stream, namespace_id="local-received")
    client = TestClient(
        create_app(
            Settings(
                mode="received", db_dsn=app_dsn(dsn), received_stream_id=stream, _env_file=None
            )
        )
    )
    assert client.get("/api/v1/capabilities").json()["received_rows"] == 0

    rejected = inbox / "b.json"
    rejected.write_text('{"batch_id":"bad","records":[{}]}', encoding="utf-8")
    seen = {}
    assert (
        scan_directory(inbox, dsn=dsn, stream_id=stream, namespace_id="local-received", seen=seen)[
            0
        ]["status"]
        == "invalid_batch"
    )
    capabilities = client.get("/api/v1/capabilities").json()
    assert capabilities["received_inbox_failure_count"] == 1
    assert capabilities["received_inbox_last_failure_at"] is not None
    assert capabilities["received_inbox_last_scan_at"] is not None
    checked_at = datetime.fromisoformat(capabilities["received_status_checked_at"])
    last_scan_at = datetime.fromisoformat(capabilities["received_inbox_last_scan_at"])
    assert checked_at >= last_scan_at
    assert client.get("/api/v1/attention").json()["all_records_total"] == 0

    first = inbox / "a.json"
    first.write_text(json.dumps({"batch_id": "b1", "records": [sample_record()]}), encoding="utf-8")
    outcome = scan_directory(
        inbox,
        dsn=dsn,
        stream_id=stream,
        namespace_id="local-received",
        seen=seen,
        max_files=1,
    )
    assert [item["status"] for item in outcome] == ["imported"]
    assert client.get("/api/v1/capabilities").json()["received_inbox_failure_count"] == 1
    assert client.get("/api/v1/attention").json()["all_records_total"] == 1

    replacement = inbox / "b.part"
    replacement.write_text(
        json.dumps({"batch_id": "b2", "records": [sample_record("r2")]}),
        encoding="utf-8",
    )
    replacement.replace(rejected)
    outcome = scan_directory(
        inbox, dsn=dsn, stream_id=stream, namespace_id="local-received", seen=seen
    )
    assert [item["status"] for item in outcome] == ["imported"]
    assert client.get("/api/v1/capabilities").json()["received_inbox_failure_count"] == 0
    assert client.get("/api/v1/attention").json()["all_records_total"] == 2
    assert (
        scan_directory(inbox, dsn=dsn, stream_id=stream, namespace_id="local-received", seen=seen)
        == []
    )
    assert client.get("/api/v1/capabilities").json()["received_rows"] == 2

    conflict = inbox / "c.json"
    conflict.write_text(
        json.dumps({"batch_id": "b1", "records": [sample_record("different")]}),
        encoding="utf-8",
    )
    outcome = scan_directory(
        inbox, dsn=dsn, stream_id=stream, namespace_id="local-received", seen=seen
    )
    assert [item["status"] for item in outcome] == ["batch_conflict"]
    assert client.get("/api/v1/capabilities").json()["received_inbox_failure_count"] == 1
    conflict.unlink()
    scan_directory(inbox, dsn=dsn, stream_id=stream, namespace_id="local-received", seen=seen)
    assert client.get("/api/v1/capabilities").json()["received_inbox_failure_count"] == 0

    unreadable = inbox / "d.json"
    unreadable.write_text(
        json.dumps({"batch_id": "b3", "records": [sample_record("r3")]}),
        encoding="utf-8",
    )
    original_load = module.load

    def fail_to_read(*args, **kwargs):
        raise OSError("synthetic unreadable file")

    monkeypatch.setattr(module, "load", fail_to_read)
    outcome = scan_directory(
        inbox, dsn=dsn, stream_id=stream, namespace_id="local-received", seen=seen
    )
    assert [item["status"] for item in outcome] == ["file_unreadable"]
    assert "d.json" not in seen
    monkeypatch.setattr(module, "load", original_load)
    outcome = scan_directory(
        inbox, dsn=dsn, stream_id=stream, namespace_id="local-received", seen=seen
    )
    assert [item["status"] for item in outcome] == ["imported"]
    assert client.get("/api/v1/capabilities").json()["received_inbox_failure_count"] == 0
