"""Source/API parity checks for a bounded historical replay."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest


def _verifier():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_replay_api.py"
    spec = importlib.util.spec_from_file_location("verify_replay_api", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replay_verifier_reads_every_object_page():
    module = _verifier()

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        assert request.url.params["candidate_kind"] == "all"
        return httpx.Response(
            200,
            json={
                "mode": "replay",
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
        result = module.api_object_groups(client, as_of="2026-09-25T07:00:00+03:00")
    assert result["total"] == len(result["items"]) == 32
    assert result["items"][-1]["object_id"] == "synthetic-object-31"


def test_full_page_rejects_changed_source_text_with_unchanged_identity():
    module = _verifier()
    expected = module.source_message(
        (
            "synthetic-row",
            7,
            120578,
            42,
            "Газовый датчик",
            "Газовая охрана",
            "Обнаружен газ",
            None,
            True,
            datetime(2025, 11, 6, 8, 30),
            "synthetic.csv",
            "synthetic-hash",
            1,
            "synthetic-reference",
            False,
            False,
            False,
            2,
            "matched",
        )
    )
    actual = {
        **expected,
        "event_at": expected["event_at"].isoformat(),
        "available_at": expected["available_at"].isoformat(),
        "source_namespace": "synthetic-namespace",
        "snapshot_id": "synthetic-snapshot",
    }
    assert module.same_source_message(actual, expected)

    def client_for(message):
        return httpx.Client(
            base_url="http://example.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "view": "all",
                        "policy_version": module.POLICY_VERSION,
                        "total": 1,
                        "items": [{"message": message}],
                    },
                )
            ),
        )

    kwargs = {
        "as_of": datetime(2025, 11, 6, 8, 35, tzinfo=UTC).isoformat(),
        "view": "all",
        "alarm": None,
        "namespace": "synthetic-namespace",
        "snapshot_id": "synthetic-snapshot",
        "expected_messages": {"synthetic-row": expected},
    }
    with client_for(actual) as client:
        assert module.api_ids(client, **kwargs) == ({"synthetic-row"}, 1)
    with client_for({**actual, "value_raw": "Норма"}) as client:
        with pytest.raises(ValueError, match="content differs"):
            module.api_ids(client, **kwargs)


def test_source_parity_checks_alarm_time_channel_and_quality_flags():
    module = _verifier()
    expected = module.source_message(
        (
            "synthetic-row",
            1,
            2,
            None,
            "Состояние фазы",
            None,
            "Обесточен",
            None,
            False,
            datetime(2025, 11, 6, 8, 30),
            "synthetic.csv",
            "synthetic-hash",
            3,
            None,
            False,
            False,
            False,
            1,
            "unmatched",
        )
    )
    actual = {
        **expected,
        "event_at": expected["event_at"].isoformat(),
        "available_at": expected["available_at"].isoformat(),
    }
    for change in (
        {"alarm": True},
        {"channel_id": "other"},
        {"event_at": datetime(2025, 11, 6, 8, 31, tzinfo=UTC).isoformat()},
        {"quality_flags": []},
    ):
        assert not module.same_source_message({**actual, **change}, expected)
