"""C0.2 contract: streaming batches, integration role, notifications, monthly report and
the fire detector pilot target. Fixture routes only; packages B1x, N1, R1, B4 implement
the other modes.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from infra_pulse_backend.api import forecast_fixture, product_fixture
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_core.contracts.auth import PERMISSIONS, Me
from infra_pulse_core.contracts.forecast import FIRE_PILOT_TARGET, ForecastCardView
from infra_pulse_core.contracts.imports import (
    MAX_OBSERVATION_RECORDS,
    ImportFile,
    ObservationBatch,
    ObservationRecord,
)
from infra_pulse_core.contracts.notifications import NotificationItem, NotificationSummary
from infra_pulse_core.contracts.reports import MonthlyReport, ReportCardCounts

ROOT = Path(__file__).resolve().parents[2]
MSK = timezone(timedelta(hours=3))
RECORD_RU = {
    "ид_события": 4524243389,
    "ид_канала_данных": "120473",
    "дата": "2026-08-01",
    "время": "03:09:27",
    "тревожное": False,
    "значение_датчика": "28",
}
RECORD_EN = {
    "event_id": "4524243390",
    "channel_id": "120474",
    "event_at": "2026-08-01T03:10:00+03:00",
    "alarm": True,
    "value": "Неисправен",
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(mode="fixture", _env_file=None)))


def test_observation_records_accept_csv_and_english_keys():
    ru = ObservationRecord.model_validate(RECORD_RU)
    assert (ru.event_id, ru.channel_id, ru.value, ru.event_at) == (
        "4524243389",
        "120473",
        "28",
        None,
    )
    en = ObservationRecord.model_validate(RECORD_EN)
    assert en.event_at is not None and en.date is None and en.value == "Неисправен"
    cases = [
        (RECORD_RU | {"event_at": "2026-08-01T03:09:27+03:00"}, "either date and time"),
        ({k: v for k, v in RECORD_RU.items() if k != "время"}, "go together"),
        ({k: v for k, v in RECORD_EN.items() if k != "event_at"}, "either date and time"),
        (RECORD_EN | {"value": 28}, "string"),
        (RECORD_EN | {"alarm": "t"}, "boolean"),
        (RECORD_EN | {"unexpected": 1}, "Extra inputs"),
        (RECORD_EN | {"event_at": "2026-08-01T03:10:00"}, "timezone"),
    ]
    for payload, message in cases:
        with pytest.raises(ValidationError, match=message):
            ObservationRecord.model_validate(payload)
    batch = ObservationBatch(batch_id="scada-2026-08-01T03:10", records=[RECORD_RU, RECORD_EN])
    assert len(batch.records) == 2
    with pytest.raises(ValidationError):
        ObservationBatch(batch_id="bad id", records=[RECORD_EN])
    with pytest.raises(ValidationError):
        ObservationBatch(batch_id="big", records=[RECORD_EN] * (MAX_OBSERVATION_RECORDS + 1))


def test_integration_role_only_ingests():
    assert PERMISSIONS["ingest"] == {"integration", "admin"}
    assert PERMISSIONS["report"] == {"analyst", "admin"}
    for permission in ("read", "decide", "import", "research", "report"):
        assert "integration" not in PERMISSIONS[permission], permission
    token = Me(
        subject_id="integration:synthetic-scada",
        display_name="Синтетическая интеграция",
        roles=["integration"],
        auth_source="token",
    )
    assert token.can("ingest") and not token.can("read")


def test_observations_fixture_routes(client):
    accepted = client.post(
        "/api/v1/observations", json={"batch_id": "synthetic-1", "records": [RECORD_RU, RECORD_EN]}
    )
    assert accepted.status_code == 202, accepted.json()
    report = ImportFile.model_validate(accepted.json())
    assert (report.format, report.status, report.simulated, report.file_name) == (
        "journal_json",
        "queued",
        True,
        "synthetic-1",
    )
    bad = client.post("/api/v1/observations", json={"batch_id": "x", "records": []})
    assert bad.status_code == 422
    too_large = client.post(
        "/api/v1/observations",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": str(6 * 1024 * 1024)},
    )
    assert too_large.status_code in (413, 422)
    status = ImportFile.model_validate(
        client.get("/api/v1/observations/synthetic-batch-001").json()
    )
    assert status.format == "journal_json" and status.status == "published"
    assert client.get("/api/v1/observations/missing").status_code == 404


def test_notifications_fixture(client):
    summary = NotificationSummary.model_validate(
        client.get("/api/v1/notifications", params={"since": "2026-09-24T00:00:00+03:00"}).json()
    )
    assert (summary.new_cards, summary.critical_alarms) == (2, 3)
    assert summary.policy_confirmed is False and summary.maintenance_check == "not_applied"
    later = client.get(
        "/api/v1/notifications", params={"since": "2026-09-24T08:00:00+03:00"}
    ).json()
    assert (later["new_cards"], later["critical_alarms"]) == (0, 1)  # the 09:00 test series
    for since, detail in (
        ("2026-09-24T00:00:00", "aware_since_required"),
        ("2026-09-10T00:00:00+03:00", "since_window_too_long"),
        ("2026-09-25T00:00:00+03:00", "since_after_as_of"),
    ):
        response = client.get("/api/v1/notifications", params={"since": since})
        assert (response.status_code, response.json()["detail"]) == (422, detail)
    with pytest.raises(ValidationError, match="names its rule"):
        NotificationItem(
            kind="critical_alarm",
            ref_id="r",
            title="Исходная тревога",
            at=datetime(2026, 9, 24, tzinfo=MSK),
        )
    payload = summary.model_dump()
    payload["truncated"] = True
    with pytest.raises(ValidationError, match="truncated"):
        NotificationSummary.model_validate(payload)


def test_monthly_report_fixture(client):
    report = MonthlyReport.model_validate(
        client.get("/api/v1/reports/monthly", params={"month": "2026-09"}).json()
    )
    counts = report.cards[0]
    assert (counts.issued, counts.released, counts.no_event, counts.unknown, counts.open) == (
        13,
        1,
        1,
        1,
        10,
    )
    assert {row.decision_code for row in report.decisions} == {"R1", "R3"}
    assert report.top_objects and len(report.top_objects) <= 10
    empty = client.get("/api/v1/reports/monthly", params={"month": "2026-01"}).json()
    assert empty["cards"] == [] and empty["decisions"] == []
    assert client.get("/api/v1/reports/monthly", params={"month": "2026-13"}).status_code == 422
    # R1: the XLSX renders the same report (tests/test_reports.py compares the numbers).
    xlsx = client.get("/api/v1/reports/monthly.xlsx", params={"month": "2026-09"})
    assert xlsx.status_code == 200 and xlsx.content.startswith(b"PK")
    journal = client.get(
        "/api/v1/reports/journal.xlsx",
        params={"issued_from": "2026-09-24", "issued_to": "2026-09-01"},
    )
    assert (journal.status_code, journal.json()["detail"]) == (422, "issued_range_reversed")
    with pytest.raises(ValidationError, match="do not add up"):
        ReportCardCounts(
            target_spec_id=forecast_fixture.FEEDERS,
            issued=3,
            released=1,
            no_event=0,
            unknown=0,
            open=1,
        )
    with pytest.raises(ValidationError, match="first day"):
        MonthlyReport.model_validate(
            report.model_dump() | {"period_start": report.period_start + timedelta(days=1)}
        )


@pytest.mark.parametrize(
    ("mode", "notifications", "reports"),
    [
        ("scaffold", "notifications_not_implemented", "reports_not_implemented"),
        # N1/R1 read the published forecast (B2): without a DSN and scope it answers first.
        ("replay", "forecast_not_implemented", "forecast_not_implemented"),
        ("received", "forecast_not_implemented", "forecast_not_implemented"),
    ],
)
def test_new_routes_answer_503_outside_fixture(mode, notifications, reports):
    client = TestClient(create_app(Settings(mode=mode, _env_file=None)))
    cases = [
        # B1x: the observation API is implemented; without INFRA_DB_DSN it has no storage.
        (
            client.post("/api/v1/observations", json={"batch_id": "x", "records": [RECORD_EN]}),
            "imports_not_configured",
        ),
        (client.get("/api/v1/observations/x"), "imports_not_configured"),
        (
            client.get("/api/v1/notifications", params={"since": "2026-09-24T00:00:00+03:00"}),
            notifications,
        ),
        (client.get("/api/v1/reports/monthly", params={"month": "2026-09"}), reports),
    ]
    for response, detail in cases:
        assert (response.status_code, response.json()["detail"]) == (503, detail)


def test_fire_pilot_target_is_in_the_contract():
    card = forecast_fixture.forecast_card("synthetic-feeders-14d-011").card.model_dump()
    for key in ("incident_type", "event_label"):
        card.pop(key)
    card.update(
        target_spec_id=FIRE_PILOT_TARGET,
        target_label="отказ пожарных извещателей (пилот)",
        sensor_type="Датчик дыма",
        system_type="Пожарная охрана",
    )
    view = ForecastCardView.model_validate({"card": card})
    assert view.card.incident_type == "fire_detector_failure"
    assert "вне сессии ТО" in view.card.event_label


def test_generated_product_fixture_has_c02_documents():
    published = json.loads((ROOT / "contracts/product.fixture.json").read_text(encoding="utf-8"))
    ImportFile.model_validate(published["observation_batch"])
    NotificationSummary.model_validate(published["notifications"])
    MonthlyReport.model_validate(published["monthly_report"])
    assert published == json.loads(json.dumps(product_fixture.product_fixture_document()))
