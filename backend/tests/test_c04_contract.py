"""C0.4 contract and fixture routes: decision labels and deadlines, check results,
journal filters and fields, attention object names, two data slices."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from infra_pulse_backend.api import forecast_fixture
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_core.contracts.forecast import (
    CHECK_RESULT_LABELS,
    DECISION_LABELS,
    EVENT_CAUSE_LABELS,
    FOUND_LABELS,
    ForecastCheckResult,
    ForecastCheckResultCreate,
    ForecastCheckResultList,
    ForecastDecisionCreate,
    ForecastDecisionSummary,
    ForecastJournalEntry,
    ForecastJournalList,
    ForecastOutcome,
    ForecastState,
    ForecastWindowEvent,
)

MSK = timezone(timedelta(hours=3))
OPEN = "synthetic-feeders-14d-011"
RELEASED = forecast_fixture.RELEASED_ID
BASE = {
    "reason_text": "основание",
    "verification_methods": ["source_records"],
    "idempotency_key": "synthetic-key-c04-1",
    "expected_revision": 0,
}
NOTIFIED = {
    "decision_code": "R3",
    "reason_code": "R3.1",
    "notified_to": "дежурный энергетик (синтетика)",
    "notified_at": "2026-09-24T09:00:00+03:00",
    "awaiting_result_until": "2026-09-26T17:00:00+03:00",
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(mode="fixture", _env_file=None)))


def test_labels_keep_codes_and_rename_r3():
    assert DECISION_LABELS["R3"] == "Сообщено энергетику"
    assert (DECISION_LABELS["R1"], DECISION_LABELS["R7"]) == ("Под наблюдением", "Нет оснований")
    assert set(DECISION_LABELS) == {f"R{n}" for n in range(1, 8)}
    assert set(CHECK_RESULT_LABELS) == {"awaiting", "fixed", "no_violation", "not_done"}
    assert FOUND_LABELS["comm_module"] == "модуль связи"
    assert EVENT_CAUSE_LABELS["smvu_channel"] == "канал связи СМВУ"


def test_decision_rules_by_code():
    assert ForecastDecisionCreate.model_validate(BASE | NOTIFIED).awaiting_result_until
    cases = [
        (BASE | NOTIFIED | {"awaiting_result_until": None}, "result deadline"),
        (
            BASE | {k: v for k, v in NOTIFIED.items() if k not in ("notified_to", "notified_at")},
            "result deadline",
        ),
        (BASE | NOTIFIED | {"awaiting_result_until": "2026-09-24T08:00:00+03:00"}, "later than"),
        (BASE | {"decision_code": "R1", "reason_code": "R1.1"}, "watch deadline"),
        (
            BASE
            | {
                "decision_code": "R2",
                "reason_code": "R2.1",
                "watch_until": "2026-09-30T00:00:00+03:00",
            },
            "only «Под наблюдением»",
        ),
        (
            BASE | {"decision_code": "R7", "reason_code": "R7.1", "verification_methods": []},
            "at least 1",
        ),
    ]
    for payload, message in cases:
        with pytest.raises(ValidationError, match=message):
            ForecastDecisionCreate.model_validate(payload)
    watch = ForecastDecisionCreate.model_validate(
        BASE
        | {"decision_code": "R1", "reason_code": "R1.1", "watch_until": "2026-09-30T00:00:00+03:00"}
    )
    assert watch.watch_until is not None
    stored = forecast_fixture.decision_list(OPEN).items[0].model_dump()
    with pytest.raises(ValidationError, match="later than the decision"):
        ForecastDecisionSummary.model_validate(
            stored | {"awaiting_result_until": stored["decided_at"]}
        )


def test_decision_route_deadlines(client):
    ok = client.post(
        f"/api/v1/forecasts/{OPEN}/decisions", json=BASE | NOTIFIED | {"expected_revision": 1}
    )
    assert ok.status_code == 201, ok.json()
    assert ok.json()["awaiting_result_until"] is not None
    early = client.post(
        f"/api/v1/forecasts/{OPEN}/decisions",
        json=BASE
        | {
            "decision_code": "R1",
            "reason_code": "R1.1",
            "watch_until": "2026-09-24T09:59:00+03:00",
            "expected_revision": 1,
        },
    )
    assert (early.status_code, early.json()["detail"]) == (422, "deadline_before_decision")
    no_methods = client.post(
        f"/api/v1/forecasts/{OPEN}/decisions",
        json=BASE | {"decision_code": "R7", "reason_code": "R7.1", "verification_methods": []},
    )
    assert no_methods.status_code == 422


def test_check_result_rules_and_routes(client):
    listed = ForecastCheckResultList.model_validate(
        client.get(f"/api/v1/forecasts/{RELEASED}/check-results").json()
    )
    latest = listed.items[0]
    assert (latest.check_result, latest.found, latest.event_cause) == (
        "fixed",
        ["breaker"],
        "protection_trip",
    )
    assert latest.found_confirmed_by_customer is False
    body = {
        "check_result": "fixed",
        "found": ["other", "cable"],
        "found_other_text": "клеммник",
        "result_at": "2026-09-24T09:40:00+03:00",
        "event_cause": "unknown",
        "idempotency_key": "synthetic-key-c04-check",
        "expected_revision": 1,
    }
    created = client.post(f"/api/v1/forecasts/{RELEASED}/check-result", json=body)
    assert created.status_code == 201, created.json()
    assert ForecastCheckResult.model_validate(created.json()).revision == 2
    not_released = client.post(
        f"/api/v1/forecasts/{OPEN}/check-result", json=body | {"expected_revision": 1}
    )
    assert (not_released.status_code, not_released.json()["detail"]) == (
        422,
        "event_cause_requires_event",
    )
    stale = client.post(
        f"/api/v1/forecasts/{RELEASED}/check-result", json=body | {"expected_revision": 0}
    )
    assert (stale.status_code, stale.json()["detail"]) == (409, "check_result_revision_conflict")
    future = client.post(
        f"/api/v1/forecasts/{RELEASED}/check-result",
        json=body | {"result_at": "2026-09-25T00:00:00+03:00"},
    )
    assert (future.status_code, future.json()["detail"]) == (422, "result_after_record")
    assert client.get("/api/v1/forecasts/missing/check-results").status_code == 404
    for payload, message in (
        (body | {"check_result": "no_violation"}, "only for a fixed result"),
        (body | {"found_other_text": None}, "другое"),
        (body | {"found": ["cable", "cable"], "found_other_text": None}, "duplicate"),
    ):
        with pytest.raises(ValidationError, match=message):
            ForecastCheckResultCreate.model_validate(payload)


def test_journal_filters_and_fields(client):
    none = ForecastJournalList.model_validate(
        client.get(
            "/api/v1/forecast-journal", params={"decision_state": "none", "limit": 100}
        ).json()
    )
    assert none.items and all(item.decision is None for item in none.items)
    decided = client.get(
        "/api/v1/forecast-journal", params={"decision_state": "any", "limit": 100}
    ).json()
    assert decided["total"] == 3 and none.total + decided["total"] == 14
    unknown = client.get("/api/v1/forecast-journal", params={"outcome": "unknown"}).json()
    assert [item["card"]["id"] for item in unknown["items"]] == [forecast_fixture.UNKNOWN_ID]
    assert (
        client.get("/api/v1/forecast-journal", params={"decision_state": "some"}).status_code == 422
    )
    entry = next(
        item
        for item in none.items + ForecastJournalList.model_validate(decided).items
        if item.card.id == RELEASED
    )
    assert entry.check_result is not None and entry.outcome.power_off_at is not None
    assert entry.outcome.power_off_at - entry.outcome.first_event_at == timedelta(minutes=10)
    assert entry.events[0].power_off_at is not None
    payload = entry.model_dump()
    payload["check_result"]["event_cause"] = "external_grid"
    payload["outcome"] = ForecastOutcome(
        status="pending", label_version=entry.outcome.label_version
    ).model_dump()
    payload.update(list_state="open", released_at=None, events=[])
    with pytest.raises(ValidationError, match="event cause needs"):
        ForecastJournalEntry.model_validate(payload)
    with pytest.raises(ValidationError, match="precedes the event start"):
        ForecastWindowEvent(
            event_id="e",
            started_at=datetime(2026, 9, 24, 10, tzinfo=MSK),
            channel_ids=["c"],
            cluster_size=1,
            while_open=True,
            power_off_at=datetime(2026, 9, 24, 9, tzinfo=MSK),
        )


def test_two_data_slices_and_object_names(client):
    state = ForecastState.model_validate(client.get("/api/v1/forecast-state").json())
    assert state.forecast_data_as_of == state.data_as_of
    assert state.source_messages_as_of > state.forecast_data_as_of
    payload = state.model_dump() | {"forecast_data_as_of": state.published_at}
    with pytest.raises(ValidationError, match="equals data_as_of"):
        ForecastState.model_validate(payload)
    attention = client.get("/api/v1/attention").json()
    named = [item["message"] for item in attention["items"] if item["message"]["object_id"]]
    assert named and all(message["object_name"] for message in named)
    schemes = client.get("/api/v1/schemes").json()
    assert all(item["object_name"] for item in schemes["items"])


def test_days_without_data_before_the_latest_cutoff_are_a_period(client):
    """P1-2 (rehearsal 29.09.2026): the state names the days without data, or nothing."""
    state = ForecastState.model_validate(client.get("/api/v1/forecast-state").json())
    assert (state.no_data_from, state.no_data_to) == (None, None)
    payload = state.model_dump()
    first, last = date(2026, 7, 1), date(2026, 7, 31)
    named = ForecastState.model_validate(payload | {"no_data_from": first, "no_data_to": last})
    assert (named.no_data_from, named.no_data_to) == (first, last)
    with pytest.raises(ValidationError, match="closed period"):
        ForecastState.model_validate(payload | {"no_data_from": first})
    with pytest.raises(ValidationError, match="reversed"):
        ForecastState.model_validate(payload | {"no_data_from": last, "no_data_to": first})
