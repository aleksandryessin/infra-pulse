from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from infra_pulse_backend.api.app import attention_fixture, create_app
from infra_pulse_backend.config import Settings
from infra_pulse_backend.operations.attention import (
    explain_attention,
    order_for_review,
    summarize_source_alarms,
)
from infra_pulse_backend.storage.review_notes_pg import payload_hash
from infra_pulse_core.contracts.attention import (
    AttentionList,
    ObservedMessage,
    ReviewJournalEntry,
    ReviewJournalList,
    ReviewNote,
    ReviewNoteCreate,
    SourceAlarmWindow,
)

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def message(uid: str, *, alarm: bool, available_at: datetime = NOW) -> ObservedMessage:
    return ObservedMessage(
        row_uid=uid,
        channel_id="synthetic-channel",
        value_raw="Обнаружен газ",
        alarm=alarm,
        event_at=NOW - timedelta(minutes=5),
        available_at=available_at,
        availability_basis="simulated",
        source_namespace="synthetic",
        snapshot_id="synthetic-snapshot",
    )


def test_fixture_queue_exposes_original_alarm_and_raw_text_without_diagnosis():
    client = TestClient(create_app(Settings(mode="fixture", _env_file=None)))
    assert (
        client.get("/api/v1/capabilities", params={"after_received_watermark": 0}).status_code
        == 422
    )
    assert (
        client.get("/api/v1/attention", params={"after_received_watermark": 0}).status_code == 422
    )
    result = client.get("/api/v1/attention")
    assert result.status_code == 200
    queue = AttentionList.model_validate(result.json())
    assert queue.mode == "fixture"
    assert queue.policy_status == "provisional"
    assert queue.total == 3
    assert (
        queue.source_alarm_count,
        queue.watch_text_count,
        queue.chronological_count,
    ) == (1, 2, 0)
    assert queue.items[0].message.alarm is True
    assert queue.items[0].attention_band == "source_alarm"
    assert any(
        item.message.value_raw == "Обнаружен газ" and item.message.alarm is False
        for item in queue.items
    )
    assert any(item.message.object_id is None for item in queue.items)
    assert all(item.message.row_uid.startswith("synthetic-") for item in queue.items)
    page = client.get("/api/v1/attention?offset=1&limit=1").json()
    assert page["total"] == 3
    assert page["offset"] == 1
    assert len(page["items"]) == 1
    assert (page["source_alarm_count"], page["watch_text_count"], page["chronological_count"]) == (
        1,
        2,
        0,
    )
    assert page["items"][0]["review_order"] == 2
    beyond = client.get("/api/v1/attention?offset=100&limit=1").json()
    assert beyond["total"] == 3
    assert beyond["items"] == []
    exact = client.get(
        "/api/v1/attention", params={"view": "all", "row_uid": "synthetic-row-001"}
    ).json()
    assert exact["total"] == 1
    assert exact["items"][0]["message"]["value_raw"] == "Обнаружен газ"
    assert (
        client.get("/api/v1/attention", params={"view": "all", "row_uid": "missing-row"}).json()[
            "total"
        ]
        == 0
    )
    without_note = client.get("/api/v1/attention", params={"local_note": "absent"}).json()
    with_note = client.get("/api/v1/attention", params={"local_note": "present"}).json()
    assert without_note["total"] == without_note["all_records_total"] == 3
    assert with_note["total"] == 0
    assert with_note["all_records_total"] == 3
    only_source_alarms = client.get(
        "/api/v1/attention", params={"view": "all", "alarm": "true"}
    ).json()
    assert only_source_alarms["total"] == only_source_alarms["all_records_total"] == 1
    assert only_source_alarms["items"][0]["message"]["alarm"] is True
    assert client.get("/api/v1/attention", params={"alarm": "invalid"}).status_code == 422


def test_scaffold_queue_does_not_serve_synthetic_data():
    client = TestClient(create_app(Settings(mode="scaffold", _env_file=None)))
    assert client.get("/api/v1/attention").status_code == 503


def test_local_review_writes_are_disabled_by_default():
    client = TestClient(
        create_app(
            Settings(
                mode="replay",
                db_dsn="postgresql://invalid.example/db",
                replay_snapshot_id="synthetic-snapshot",
                _env_file=None,
            )
        )
    )
    assert client.get("/api/v1/attention/synthetic-row/reviews").status_code == 503
    assert client.get("/api/v1/review-journal").status_code == 503
    assert (
        client.post(
            "/api/v1/attention/synthetic-row/reviews",
            json={
                "idempotency_key": str(uuid4()),
                "expected_revision": 0,
                "view_as_of": NOW.isoformat(),
                "displayed_snapshot_id": "synthetic-snapshot",
                "displayed_policy_version": "provisional-exact-text-received-v2",
                "action_text": "Проверить",
                "result_text": "Ничего не обнаружено",
                "reason_text": "Синтетический тест",
            },
        ).status_code
        == 503
    )


def test_review_journal_keeps_source_identity_and_validates_page():
    source = message("source-1", alarm=True)
    note = ReviewNote(
        note_id=uuid4(),
        row_uid="source-1",
        revision=1,
        actor_id="local-replay-operator",
        action_text="Проверили",
        result_text="Причина не установлена",
        reason_text="Локальная проверка",
        created_at=NOW,
    )
    entry = ReviewJournalEntry(note=note, message=source)
    page = ReviewJournalList(mode="replay", items=[entry], offset=0, total=1, review_watermark=1)
    assert page.items[0].message.alarm is True
    assert page.items[0].message.value_raw == "Обнаружен газ"
    with pytest.raises(ValidationError, match="differ"):
        ReviewJournalEntry(note=note, message=source.model_copy(update={"row_uid": "other"}))
    with pytest.raises(ValidationError, match="journal total"):
        ReviewJournalList(mode="replay", items=[entry], offset=1, total=1, review_watermark=1)


def test_review_payload_needs_result_and_hash_binds_revision():
    payload = ReviewNoteCreate(
        idempotency_key=uuid4(),
        expected_revision=0,
        view_as_of=NOW,
        displayed_snapshot_id="synthetic-snapshot",
        displayed_policy_version="provisional-exact-text-received-v2",
        action_text="Проверить",
        result_text="Ничего не обнаружено",
        reason_text="Синтетический тест",
    )
    assert payload_hash(payload) == payload_hash(
        payload.model_copy(update={"idempotency_key": uuid4()})
    )
    assert payload_hash(payload) != payload_hash(
        payload.model_copy(update={"expected_revision": 1})
    )
    assert payload_hash(payload) != payload_hash(
        payload.model_copy(update={"view_as_of": NOW + timedelta(seconds=1)})
    )
    assert payload_hash(payload) != payload_hash(
        payload.model_copy(update={"displayed_received_watermark": 3})
    )
    assert payload_hash(payload) != payload_hash(
        payload.model_copy(update={"displayed_policy_version": "changed"})
    )
    with pytest.raises(ValidationError):
        ReviewNoteCreate.model_validate({**payload.model_dump(), "result_text": ""})
    with pytest.raises(ValidationError):
        ReviewNoteCreate.model_validate(
            {**payload.model_dump(), "view_as_of": "2026-09-24T12:00:00"}
        )


def test_saved_basis_uses_same_explanation_as_queue():
    examples = [
        (True, "Состояние насоса", "Работают все насосы в АНС", "source_alarm"),
        (False, "Газовый датчик", "Обнаружен газ", "watch_text"),
        (False, "Газовый датчик", "0.03", "chronological"),
    ]
    for alarm, sensor_type, value_raw, expected_band in examples:
        band, reasons = explain_attention(alarm=alarm, sensor_type=sensor_type, value_raw=value_raw)
        assert band == expected_band
        assert reasons[-1] == "received_order"


def test_order_keeps_all_rows_and_filters_future_availability():
    rows = [
        message("a", alarm=False, available_at=NOW),
        message("b", alarm=True, available_at=NOW - timedelta(minutes=1)),
        message("c", alarm=False, available_at=NOW + timedelta(seconds=1)),
    ]
    result = order_for_review(rows, as_of=NOW, mode="replay", view="all")
    assert [entry.message.row_uid for entry in result.items] == ["a", "b"]
    assert [entry.review_order for entry in result.items] == [1, 2]
    candidates = order_for_review(rows, as_of=NOW, mode="replay", view="attention")
    assert [entry.message.row_uid for entry in candidates.items] == ["b"]
    assert len(rows) == 3


def test_equal_time_is_deterministic_and_duplicate_operational_id_rejected():
    rows = [message("z", alarm=False), message("a", alarm=False)]
    assert [
        item.message.row_uid
        for item in order_for_review(rows, as_of=NOW, mode="replay", view="all").items
    ] == [
        "a",
        "z",
    ]
    with pytest.raises(ValueError, match="duplicate operational identity"):
        order_for_review([rows[0], rows[0]], as_of=NOW, mode="replay", view="all")


def test_pagination_keeps_global_order_and_total():
    rows = [message("z", alarm=False), message("a", alarm=True), message("b", alarm=True)]
    page = order_for_review(rows, as_of=NOW, mode="replay", view="all", offset=1, limit=1)
    assert page.total == 3
    assert page.offset == 1
    assert [item.review_order for item in page.items] == [2]
    assert page.items[0].message.row_uid == "b"


def test_attention_view_keeps_alarm_false_text_and_numeric_in_full_journal():
    gas_text = message("gas-text", alarm=False).model_copy(
        update={"sensor_type": "Газовый датчик", "value_raw": "Обнаружен газ"}
    )
    gas_number = message("gas-number", alarm=False).model_copy(
        update={"sensor_type": "Газовый датчик", "value_raw": "0.03"}
    )
    queue = order_for_review([gas_text, gas_number], as_of=NOW, mode="replay")
    assert queue.total == 1
    assert queue.all_records_total == 2
    assert queue.items[0].attention_band == "watch_text"
    assert queue.items[0].message.alarm is False
    full = order_for_review([gas_text, gas_number], as_of=NOW, mode="replay", view="all")
    assert full.total == 2
    assert {item.message.row_uid for item in full.items} == {"gas-text", "gas-number"}


def test_contract_rejects_future_or_naive_as_of():
    queue = attention_fixture().model_dump()
    queue["as_of"] = queue["items"][0]["message"]["available_at"] - timedelta(seconds=1)
    with pytest.raises(ValidationError, match="unavailable"):
        AttentionList.model_validate(queue)
    with pytest.raises(ValueError, match="timezone-aware"):
        order_for_review([message("a", alarm=False)], as_of=datetime(2026, 9, 24), mode="replay")


def test_source_alarm_window_orders_by_event_time_not_receipt():
    """«Сейчас» (F-04, rehearsal 29.09): a file received later with older events does not
    push the newest events aside; counts cover the window, objects are grouped."""

    def alarm(uid, object_id, channel, minutes, received_hours, flag=True):
        return message(uid, alarm=flag).model_copy(
            update={
                "object_id": object_id,
                "channel_id": channel,
                "event_at": NOW - timedelta(minutes=minutes),
                "available_at": NOW - timedelta(hours=received_hours),
            }
        )

    seeded = [alarm(f"zeta-{index}", "zeta", f"line-{index % 3}", index, 30) for index in range(6)]
    later = [alarm(f"smoke-{index}", "gamma", "smoke-1", 300 + index, 1) for index in range(8)]
    rest = [
        alarm("orphan", None, "unknown", 10, 1),
        alarm("old", "zeta", "line-0", 60 * 25, 40),  # before the window
        alarm("norm", "zeta", "line-0", 5, 1, flag=False),
        alarm("unflagged", "zeta", "line-0", 5, 1, flag=None),
        alarm("future", "omega", "line-9", 1, -1),  # received after as_of
    ]
    window = summarize_source_alarms(
        [*later, *seeded, *rest],
        as_of=NOW,
        event_from=NOW - timedelta(hours=24),
        mode="replay",
        latest_limit=3,
        object_limit=1,
        scheme_lines=frozenset({("zeta", "line-0"), ("zeta", "line-1")}),
    )
    assert (window.record_count, window.object_count, window.without_object_count) == (15, 2, 1)
    assert [item.row_uid for item in window.latest] == ["zeta-0", "zeta-1", "zeta-2"]
    (zeta,) = window.objects
    assert (zeta.object_id, zeta.record_count, zeta.channel_count) == ("zeta", 6, 3)
    assert zeta.scheme_record_count == 4
    assert zeta.last_message.row_uid == "zeta-0"
    assert zeta.first_event_at == NOW - timedelta(minutes=5)
    everything = summarize_source_alarms(
        [*later, *seeded, *rest], as_of=NOW, event_from=NOW - timedelta(hours=24), mode="replay"
    )
    assert [item.object_id for item in everything.objects] == ["zeta", "gamma"]
    assert sum(item.record_count for item in everything.objects) == 14
    # The contract refuses a window ordered by receipt or with records outside it.
    document = everything.model_dump()
    document["latest"] = list(reversed(document["latest"]))
    with pytest.raises(ValidationError, match="ordered by event time"):
        SourceAlarmWindow.model_validate(document)
    document = everything.model_dump()
    document["object_count"] = 1
    with pytest.raises(ValidationError, match="more objects"):
        SourceAlarmWindow.model_validate(document)
    document = everything.model_dump()
    document["event_from"] = NOW
    with pytest.raises(ValidationError, match="outside its bounds"):
        SourceAlarmWindow.model_validate(document)


def test_fixture_source_alarm_window_needs_an_aware_start():
    client = TestClient(create_app(Settings(mode="fixture", _env_file=None)))
    response = client.get(
        "/api/v1/attention/source-alarms", params={"event_from": "2026-09-23T09:02:00Z"}
    )
    assert response.status_code == 200, response.text
    window = SourceAlarmWindow.model_validate(response.json())
    assert window.mode == "fixture" and window.record_count == 1
    assert window.latest[0].alarm is True and window.objects[0].record_count == 1
    assert client.get("/api/v1/attention/source-alarms").status_code == 422
    naive = client.get(
        "/api/v1/attention/source-alarms", params={"event_from": "2026-09-23T09:02:00"}
    )
    assert (naive.status_code, naive.json()["detail"]) == (422, "aware_event_from_required")
    assert (
        client.get(
            "/api/v1/attention/source-alarms",
            params={"event_from": "2026-09-23T09:02:00Z", "received_watermark": 1},
        ).status_code
        == 422
    )
    scaffold = TestClient(create_app(Settings(mode="scaffold", _env_file=None)))
    assert (
        scaffold.get(
            "/api/v1/attention/source-alarms", params={"event_from": "2026-09-23T09:02:00Z"}
        ).status_code
        == 503
    )
