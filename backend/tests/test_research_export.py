"""«Исследование» outside fixture mode (G1, M8): the exported v9 summary and its route."""

import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_core.contracts.research import ResearchSummary

ROOT = Path(__file__).resolve().parents[2]
SUMMARY = ROOT / "backend/research/research-summary.json"
SCRIPT = ROOT / "backend/scripts/export_research_summary.py"


def exporter():
    spec = importlib.util.spec_from_file_location("export_research_summary", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def metric(row, name):
    return next(item for item in row.metrics if item.name == name)


def test_checked_in_summary_is_fresh_and_valid():
    assert exporter().main(["--check", "--output", str(SUMMARY)]) == 0
    summary = ResearchSummary.model_validate(json.loads(SUMMARY.read_text(encoding="utf-8")))
    assert summary.synthetic is False and summary.source_reports


def test_numbers_come_from_the_v9_report():
    summary = ResearchSummary.model_validate_json(SUMMARY.read_text(encoding="utf-8"))
    scopes = {scope.scope_id: scope for scope in summary.scopes}
    prod = scopes["phase_feeders"]
    assert (prod.status, prod.horizon_days, prod.budget_k, prod.period_use_count) == (
        "in_product",
        14,
        10,
        5,
    )
    ladder = {row.step: row for row in prod.ladder}
    precision = metric(ladder["static_list"], "precision")
    recall = metric(ladder["static_list"], "recall_incident")
    assert (precision.value, precision.ci_low, precision.ci_high) == (0.773, 0.728, 0.815)
    assert (recall.value, recall.ci_low, recall.ci_high) == (0.646, 0.604, 0.689)
    assert metric(ladder["static_list"], "precision_by_object").ci_low == 0.662
    assert metric(ladder["random_list"], "precision").value == 0.541
    assert metric(ladder["random_list"], "recall_incident").value == 0.389
    research_only = {"without_rare", "tz_sensors", "fire_layer", "fire_detector_failure"}
    assert {key for key, scope in scopes.items() if scope.status == "research_only"} == (
        research_only
    )
    assert {key for key, scope in scopes.items() if scope.status == "needs_more_data"} == {
        "gas",
        "temperature",
        "security",
    }
    # Holdout numbers of the detector target are withheld until the technologist checks them.
    assert scopes["fire_detector_failure"].ladder == []


def test_route_serves_the_file_outside_fixture_mode():
    app = create_app(Settings(mode="received", research_summary_path=SUMMARY, _env_file=None))
    response = TestClient(app).get("/api/v1/research-summary")
    assert response.status_code == 200, response.text
    assert response.json()["version"] == "research-summary-v9-2026-09-27"
    missing = create_app(Settings(mode="received", _env_file=None))
    assert TestClient(missing).get("/api/v1/research-summary").status_code == 503
