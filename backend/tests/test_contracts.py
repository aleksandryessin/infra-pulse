from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from infra_pulse_backend.api.app import create_app, fixture
from infra_pulse_backend.config import Settings
from infra_pulse_core.contracts.attention import Capabilities
from infra_pulse_core.contracts.risk import Risk


def test_default_api_does_not_return_fake_predictions():
    app = create_app(Settings(mode="scaffold", _env_file=None))
    client = TestClient(app)
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 503
    # The starter per-channel risks are not the product forecast: outside the public
    # schema, and outside fixture they point to /api/v1/forecasts instead of «not ready».
    risks = client.get("/api/v1/risks")
    assert (risks.status_code, risks.json()["detail"]) == (410, "risks_replaced_by_forecasts")
    assert "/api/v1/risks" not in app.openapi()["paths"]
    assert "/api/v1/forecasts" in app.openapi()["paths"]
    capabilities = Capabilities.model_validate(client.get("/api/v1/capabilities").json())
    assert (capabilities.stage, capabilities.inference_ready) == ("scaffold", False)


def test_fixture_is_explicit_and_has_no_probability():
    client = TestClient(create_app(Settings(mode="fixture", _env_file=None)))
    response = client.get("/api/v1/risks").json()
    risk = Risk.model_validate(response["items"][0])
    assert response["mode"] == "fixture"
    assert risk.source == "synthetic_fixture"
    assert risk.score is None
    # Synthetic cards are not a published forecast.
    capabilities = Capabilities.model_validate(client.get("/api/v1/capabilities").json())
    assert (capabilities.stage, capabilities.inference_ready) == ("fixture", False)


def test_capabilities_stage_matches_mode_and_inference():
    base = {
        "persistence_ready": True,
        "authentication_ready": False,
        "fixture_available": False,
        "observations_ready": True,
        "local_reviews_enabled": False,
    }
    published = {"stage": "forecast_published", "mode": "received", "inference_ready": True}
    assert Capabilities(**base, **published).inference_ready
    for wrong in (
        {"stage": "team_starter", "mode": "received", "inference_ready": False},
        {"stage": "observations_only", "mode": "received", "inference_ready": True},
        {"stage": "forecast_published", "mode": "received", "inference_ready": False},
        {"stage": "forecast_published", "mode": "fixture", "inference_ready": True},
        {"stage": "fixture", "mode": "replay", "inference_ready": False},
    ):
        with pytest.raises(ValidationError):
            Capabilities(**base, **wrong)


def test_abstention_cannot_look_healthy():
    payload = fixture().model_dump()
    payload.update(score=0.0, risk_level="low")
    with pytest.raises(ValidationError):
        Risk.model_validate(payload)


def test_future_data_cutoff_is_rejected():
    payload = fixture().model_dump()
    payload["data_cutoff"] = payload["as_of"] + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        Risk.model_validate(payload)


def test_timezone_and_forecast_semantics_are_enforced():
    payload = fixture().model_dump()
    payload["as_of"] = datetime(2026, 9, 15, 12)
    with pytest.raises(ValidationError):
        Risk.model_validate(payload)
    payload = fixture().model_dump()
    payload.update(
        assessment_kind="forecast",
        target_start=datetime(2026, 9, 16, 12, tzinfo=UTC),
        target_end=datetime(2026, 9, 17, 12, tzinfo=UTC),
    )
    with pytest.raises(ValidationError, match="not a forecast"):
        Risk.model_validate(payload)


@pytest.mark.parametrize("label", ["alarm_activation_proxy", "technical_fault_state_proxy"])
def test_valid_proxy_forecast_with_explicit_lead_time(label):
    payload = fixture().model_dump()
    payload.update(
        assessment_kind="forecast",
        label_type=label,
        target_start=payload["as_of"] + timedelta(hours=24),
        target_end=payload["as_of"] + timedelta(hours=48),
        status="scored",
        score_kind="probability",
        score=0.6,
        risk_level="medium",
        abstention_reason=None,
        calibration_version="synthetic-calibration",
    )
    assert Risk.model_validate(payload).label_type == label
