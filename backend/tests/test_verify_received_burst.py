"""A received API page must preserve the imported JSON message content."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest


def _verifier(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "verify_received_burst", directory / "verify_received_burst.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_received_verifier_reads_every_object_page(monkeypatch):
    module = _verifier(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        assert request.url.params["candidate_kind"] == "all"
        return httpx.Response(
            200,
            json={
                "mode": "received",
                "received_watermark": 32,
                "candidate_kind": "all",
                "offset": offset,
                "total": 32,
                "items": [
                    {"object_id": f"synthetic-object-{index:02d}"}
                    for index in range(offset, min(offset + 25, 32))
                ],
            },
        )

    with httpx.Client(
        base_url="http://example.test", transport=httpx.MockTransport(handler)
    ) as client:
        result = module.paged_objects(client, as_of="2026-09-25T07:00:00+03:00", watermark=32)
    assert result["total"] == len(result["items"]) == 32
    assert result["items"][-1]["object_id"] == "synthetic-object-31"


def test_received_page_rejects_changed_raw_with_same_row_uid(monkeypatch):
    module = _verifier(monkeypatch)
    received_at = datetime(2026, 9, 25, 7, tzinfo=UTC)
    record = {
        "source_record_id": "synthetic-row-1",
        "channel_id": "synthetic-channel-1",
        "object_id": "synthetic-object-1",
        "sensor_type": "Газовый датчик",
        "system_type": "Газовая охрана",
        "value_raw": "Обнаружен газ",
        "alarm": True,
        "event_at": (received_at - timedelta(minutes=5)).isoformat(),
    }
    expected = module.expected_message(
        record,
        stream="synthetic-burst-test",
        batch_id="synthetic-seed-v1",
        ordinal=1,
        import_report={"received_at": received_at.isoformat(), "source_sha256": "synthetic-hash"},
    )
    actual = {
        **expected,
        "event_at": expected["event_at"].isoformat(),
        "available_at": expected["available_at"].isoformat(),
    }
    assert module.same_message(actual, expected)

    def client_for(message):
        return httpx.Client(
            base_url="http://example.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "mode": "received",
                        "received_watermark": 1,
                        "total": 1,
                        "items": [{"message": message}],
                    },
                )
            ),
        )

    kwargs = {
        "view": "all",
        "alarm": None,
        "as_of": received_at.isoformat(),
        "watermark": 1,
        "stream": "synthetic-burst-test",
        "expected_messages": {expected["row_uid"]: expected},
    }
    with client_for(actual) as client:
        assert module.paged_ids(client, **kwargs) == ({expected["row_uid"]}, 1)
    with client_for({**actual, "value_raw": "Норма"}) as client:
        with pytest.raises(ValueError, match="content differs"):
            module.paged_ids(client, **kwargs)


def test_received_parity_checks_alarm_and_both_times(monkeypatch):
    module = _verifier(monkeypatch)
    received_at = datetime(2026, 9, 25, 7, tzinfo=UTC)
    expected = module.expected_message(
        {
            "source_record_id": "synthetic-row-2",
            "channel_id": "synthetic-channel-2",
            "value_raw": "Обесточен",
            "alarm": False,
            "event_at": (received_at - timedelta(minutes=5)).isoformat(),
        },
        stream="synthetic-burst-test",
        batch_id="synthetic-seed-v1",
        ordinal=2,
        import_report={"received_at": received_at.isoformat(), "source_sha256": "synthetic-hash"},
    )
    actual = {
        **expected,
        "event_at": expected["event_at"].isoformat(),
        "available_at": expected["available_at"].isoformat(),
    }
    for change in (
        {"alarm": True},
        {"event_at": received_at.isoformat()},
        {"available_at": (received_at + timedelta(minutes=1)).isoformat()},
        {"quality_flags": ["object_mapping_from_input_unverified"]},
    ):
        assert not module.same_message({**actual, **change}, expected)
