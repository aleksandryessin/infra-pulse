"""Served fixture scenario (C0.1): «обесточивание фидеров объекта», 14-day rolling list.

Covers the contract of 27.09.2026: per-object cards with top feeders, release at the
event, «k из n», recommendation policy (v5: main text and details) with «кому и когда
сообщено», dispatcher journal counters, research-only quality, picket-line scheme, imports,
auth stub and the research summary (analysts and administrators only).
"""

import json
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from infra_pulse_backend.api import auth_deps, forecast_fixture, product_fixture
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_core.contracts.attention import ObservedMessage
from infra_pulse_core.contracts.auth import Me
from infra_pulse_core.contracts.forecast import (
    FEEDER_EVENT_LABEL,
    ForecastCardView,
    ForecastDecisionCreate,
    ForecastDecisionList,
    ForecastDecisionSummary,
    ForecastJournalCounts,
    ForecastJournalCountsRow,
    ForecastJournalEntry,
    ForecastJournalList,
    ForecastList,
    ForecastQualitySummary,
    ForecastState,
    FrequencyBucket,
    RatioMetric,
    RecurringPlaceList,
)
from infra_pulse_core.contracts.imports import ImportFile, ImportList
from infra_pulse_core.contracts.research import ResearchSummary
from infra_pulse_core.contracts.scheme import ObjectScheme, ObjectSchemeList, SchemePicket
from infra_pulse_core.features import incident_list as il

ROOT = Path(__file__).resolve().parents[2]
RELEASED = forecast_fixture.RELEASED_ID
EXPIRED = forecast_fixture.EXPIRED_ID
UNKNOWN = forecast_fixture.UNKNOWN_ID
ABSTAINED = forecast_fixture.ABSTAINED_ID
OPEN = "synthetic-feeders-14d-011"
READ_PATHS = (
    "/api/v1/forecasts",
    f"/api/v1/forecasts/{OPEN}",
    "/api/v1/forecast-journal",
    "/api/v1/forecast-journal/summary",
    "/api/v1/forecast-journal/quality",
    "/api/v1/forecast-state",
    "/api/v1/registries/recurring",
    "/api/v1/schemes",
    "/api/v1/schemes/synthetic-object-19",
)
FORBIDDEN = (
    "вероятност",
    "поломк",
    r"\bнорма\b",
    r"\bисправ(ен|на|но|ны)\b",
    "критичн",
    "питания ввода",
    "power[-_]input",
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(mode="fixture", _env_file=None)))


def journal_entry(card_id: str) -> ForecastJournalEntry:
    return next(item for item in forecast_fixture.journal_entries() if item.card.id == card_id)


@pytest.mark.parametrize("mode", ["scaffold", "replay", "received"])
def test_database_routes_answer_503_until_packages_land(mode):
    client = TestClient(create_app(Settings(mode=mode, _env_file=None)))
    for path in READ_PATHS:
        response = client.get(path)
        assert (response.status_code, response.json()["detail"]) == (
            503,
            "forecast_not_implemented",
        ), path
    decisions = client.get(f"/api/v1/forecasts/{OPEN}/decisions")
    assert decisions.json()["detail"] == "decisions_storage_not_configured"
    # B1: outside fixture, imports need INFRA_DB_DSN (none configured here).
    imports = client.get("/api/v1/imports")
    assert (imports.status_code, imports.json()["detail"]) == (503, "imports_not_configured")
    research = client.get("/api/v1/research-summary")
    assert (research.status_code, research.json()["detail"]) == (503, "research_summary_missing")


def test_rolling_list_shows_ten_object_cards(client):
    page = ForecastList.model_validate(client.get("/api/v1/forecasts").json())
    assert [
        (run.horizon, run.list_policy.max_open, run.list_policy.window_days) for run in page.runs
    ] == [("336h", 10, 14)]
    assert page.total == 10
    objects = [view.card.object_id for view in page.items]
    assert len(objects) == len(set(objects))  # one card per object
    for view in page.items:
        card = view.card
        assert card.incident_type == "feeder_power_loss"
        assert card.target_label == "обесточивание электрооборудования объекта"
        assert card.event_label == FEEDER_EVENT_LABEL
        assert card.sensor_type == "Состояние фазы" and card.horizon == "336h"
        assert card.versions.scorer == "static_list"
        assert card.score.kind == "frequency_share" and card.score.label == "доля k из n"
        assert card.score.value == card.score.frequency.share
        assert card.risk_level == card.score.frequency.level
        assert card.recurrence in {"chronic", "fresh"}
        assert 3 <= len(card.channels) <= 5 and card.channels_total >= len(card.channels)
        assert all(channel.feeder_kind is not None for channel in card.channels)
        assert all(
            {fact.kind for fact in channel.reason_facts}
            == {"events_365d", "last_connection_loss_at"}
            for channel in card.channels
        )
        assert {fact.kind for fact in card.facts} == {"events_365d", "power_off_followup"}
        assert view.list_state == "open" and 1 <= view.day_index <= view.days_total == 14
        recommendation = view.recommendation
        # Policy v5 over the synthetic lines (all five kinds): pumps and ventilation first.
        assert recommendation.text == il.RECOMMENDATION_TEXT
        assert recommendation.policy_version == forecast_fixture.RECOMMENDATION_POLICY
        assert recommendation.rule_ids == ["group_pumps", "group_ventilation"]
        assert recommendation.details == [
            il.RECOMMENDATION_GROUP_TEXT["pumps"],
            il.RECOMMENDATION_GROUP_TEXT["ventilation"],
        ]
        assert recommendation.decision_code is None and recommendation.regulation_confirmed is False
    text = json.dumps(page.model_dump(mode="json"), ensure_ascii=False).lower()
    for pattern in FORBIDDEN:
        assert re.search(pattern, text) is None, pattern
    for word in ("фаза a", "фазы a", "ввод пк"):
        assert word not in text


def test_released_expired_and_abstained_cards(client):
    released = client.get("/api/v1/forecasts", params={"list_state": "released"}).json()
    assert [item["card"]["id"] for item in released["items"]] == [RELEASED]
    since = client.get(
        "/api/v1/forecasts",
        params={"list_state": "released", "released_since": "2026-09-24T00:00:00+03:00"},
    ).json()
    assert since["total"] == 0
    naive = client.get(
        "/api/v1/forecasts", params={"list_state": "released", "released_since": "2026-09-24T00:00"}
    )
    assert (naive.status_code, naive.json()["detail"]) == (422, "aware_as_of_required")
    expired = client.get("/api/v1/forecasts", params={"list_state": "expired"}).json()
    assert {item["card"]["id"] for item in expired["items"]} == {EXPIRED, UNKNOWN}
    abstained = ForecastCardView.model_validate(client.get(f"/api/v1/forecasts/{ABSTAINED}").json())
    assert abstained.card.status == "abstained" and abstained.list_state is None
    assert abstained.card.abstention_reason == "insufficient_history"
    missing = client.get("/api/v1/forecasts/synthetic-feeders-missing")
    assert (missing.status_code, missing.json()["detail"]) == (404, "forecast_not_found")


def test_journal_entries_counts_and_filters(client):
    journal = ForecastJournalList.model_validate(client.get("/api/v1/forecast-journal").json())
    assert journal.total == journal.journal_watermark == 14
    released = next(item for item in journal.items if item.card.id == RELEASED)
    assert (released.list_state, released.outcome.status) == ("released", "realized")
    assert released.outcome.resolved_at < released.card.window_end
    assert [event.while_open for event in released.events] == [True, False]
    assert released.decision.notified_to and released.decision.notified_at
    counts = ForecastJournalCounts.model_validate(
        client.get("/api/v1/forecast-journal/summary").json()
    )
    totals = {
        field: sum(getattr(row, field) for row in counts.rows)
        for field in (
            "cards_issued",
            "cards_released",
            "cards_no_event",
            "cards_unknown",
            "cards_open",
        )
    }
    assert totals == {
        "cards_issued": 13,
        "cards_released": 1,
        "cards_no_event": 1,
        "cards_unknown": 1,
        "cards_open": 10,
    }
    assert "card_share" not in json.dumps(counts.model_dump(mode="json"))

    def ids(**params) -> set[str]:
        response = client.get("/api/v1/forecast-journal", params=params)
        assert response.status_code == 200, response.json()
        return {item["card"]["id"] for item in response.json()["items"]}

    assert ids(list_state="released") == {RELEASED}
    assert ids(issued_from="2026-09-24") == {
        "synthetic-feeders-14d-019",
        "synthetic-feeders-14d-020",
        ABSTAINED,
    }
    reversed_range = client.get(
        "/api/v1/forecast-journal", params={"issued_from": "2026-09-24", "issued_to": "2026-09-10"}
    )
    assert (reversed_range.status_code, reversed_range.json()["detail"]) == (
        422,
        "issued_range_reversed",
    )
    assert client.get("/api/v1/forecast-journal", params={"limit": 4}).json()["next_cursor"] == (
        "w14.4"
    )


def test_publication_cursor_goes_stale(client, monkeypatch):
    first = client.get("/api/v1/forecasts", params={"limit": 2}).json()
    monkeypatch.setitem(
        forecast_fixture.RUN_GENERATIONS, (forecast_fixture.FEEDERS, forecast_fixture.HORIZON), 10
    )
    stale = client.get("/api/v1/forecasts", params={"cursor": first["next_cursor"]})
    assert (stale.status_code, stale.json()["detail"]) == (409, "forecast_cursor_stale")
    bad = client.get("/api/v1/forecasts", params={"cursor": "pt-xyz.1"})
    assert (bad.status_code, bad.json()["detail"]) == (422, "invalid_forecast_cursor")


def test_state_quality_and_registry(client):
    state = ForecastState.model_validate(client.get("/api/v1/forecast-state").json())
    assert [
        (h.horizon, h.open_cards, h.new_cards, h.released_since_previous) for h in state.horizons
    ] == [("336h", 10, 2, 1)]
    quality = ForecastQualitySummary.model_validate(
        client.get("/api/v1/forecast-journal/quality").json()
    )
    assert quality.evidence_scope == "fixture"
    registry = RecurringPlaceList.model_validate(client.get("/api/v1/registries/recurring").json())
    assert registry.total == 3 and {item.incident_type for item in registry.items} == {
        "feeder_power_loss"
    }


def test_picket_line_scheme(client):
    overview = ObjectSchemeList.model_validate(client.get("/api/v1/schemes").json())
    assert overview.total == 14
    assert "не план" in overview.convention
    row = next(item for item in overview.items if item.object_id == "synthetic-object-19")
    assert (row.open_cards, row.current_alarms, row.feeders_without_picket) == (1, 2, 1)
    scheme = ObjectScheme.model_validate(client.get("/api/v1/schemes/synthetic-object-19").json())
    assert {landmark.kind for landmark in scheme.landmarks} == {"input", "ats"}
    assert {feeder.picket.form for feeder in scheme.feeders} == {"range", "point", "unknown"}
    assert any(feeder.current_alarms for feeder in scheme.feeders)
    # F6 B-3: one alarm record of the day is still in force, one was cleared by «Норма».
    alarms = [alarm for feeder in scheme.feeders for alarm in feeder.current_alarms]
    assert sorted(alarm.cleared_value_raw or "" for alarm in alarms) == ["", "Норма"]
    assert all(feeder.alarm_records_24h == len(feeder.current_alarms) for feeder in scheme.feeders)
    released = ObjectScheme.model_validate(client.get("/api/v1/schemes/synthetic-object-21").json())
    assert [item.forecast_id for item in released.released_7d] == [RELEASED]
    assert client.get("/api/v1/schemes/synthetic-object-99").status_code == 404
    with pytest.raises(ValidationError, match="basis"):
        SchemePicket(form="point", picket_from=3)
    with pytest.raises(ValidationError, match="carries no picket"):
        SchemePicket(form="unknown", picket_from=3)


def test_attention_messages_carry_optional_pickets():
    base = {
        "row_uid": "synthetic-row",
        "channel_id": "synthetic-channel",
        "value_raw": "Неисправен",
        "alarm": True,
        "event_at": "2026-09-24T08:00:00+03:00",
        "available_at": "2026-09-24T08:00:01+03:00",
        "availability_basis": "simulated",
        "source_namespace": "synthetic",
        "snapshot_id": "synthetic",
    }
    assert ObservedMessage.model_validate(base).picket_form == "unknown"
    parsed = ObservedMessage.model_validate(
        base
        | {
            "picket_form": "range",
            "picket_from": 12,
            "picket_to": 14,
            "picket_basis": "channel_name",
        }
    )
    assert parsed.picket_basis == "channel_name"
    with pytest.raises(ValidationError, match="ordered end"):
        ObservedMessage.model_validate(
            base
            | {
                "picket_form": "range",
                "picket_from": 14,
                "picket_to": 12,
                "picket_basis": "channel_name",
            }
        )


def test_rolling_list_rules_reject_inconsistent_pages():
    base = json.loads(
        json.dumps(forecast_fixture.forecast_list(limit=100).model_dump(), default=str)
    )
    payload = json.loads(json.dumps(base))
    payload["items"][0]["list_state"] = None
    payload["items"][0]["day_index"] = payload["items"][0]["days_total"] = None
    with pytest.raises(ValidationError, match="states whether it is open"):
        ForecastList.model_validate(payload)
    payload = json.loads(json.dumps(base))
    payload["runs"][0]["list_policy"]["max_open"] = 9
    with pytest.raises(ValidationError, match="more open cards"):
        ForecastList.model_validate(payload)
    payload = json.loads(json.dumps(base))
    payload["runs"][0]["list_policy"]["window_days"] = 7
    with pytest.raises(ValidationError, match="rolling window differs"):
        ForecastList.model_validate(payload)
    view = forecast_fixture.forecast_card(OPEN).model_dump()
    view["day_index"] = 15
    with pytest.raises(ValidationError, match="exceeds days total"):
        ForecastCardView.model_validate(view)
    view = forecast_fixture.forecast_card(OPEN).model_dump()
    view["card"]["channels_total"] = 2
    with pytest.raises(ValidationError, match="channels total"):
        ForecastCardView.model_validate(view)


def test_release_and_event_rules_in_journal():
    mutations = [
        (
            lambda p: p["outcome"].update(
                status="not_realized",
                first_event_at=None,
                lead_hours=None,
                event_channel_ids=[],
                event_cluster_size=None,
                power_off_at=None,
            ),
            "released card is realized",
        ),
        (lambda p: p.update(released_at=p["card"]["window_end"]), "outside the card window"),
        (
            lambda p: p["outcome"].update(resolved_at=p["released_at"] - timedelta(minutes=5)),
            "resolves before the release",
        ),
        (lambda p: p["events"][1].update(while_open=True), "after the release is not while open"),
        (lambda p: p["events"].reverse(), "ordered by start"),
    ]
    for mutate, message in mutations:
        payload = journal_entry(RELEASED).model_dump()
        mutate(payload)
        with pytest.raises(ValidationError, match=message):
            ForecastJournalEntry.model_validate(payload)
    payload = journal_entry(EXPIRED).model_dump()
    payload["outcome"]["resolved_at"] = payload["card"]["window_end"] - timedelta(hours=1)
    with pytest.raises(ValidationError, match="after the window ends"):
        ForecastJournalEntry.model_validate(payload)
    with pytest.raises(ValidationError, match="do not add up"):
        ForecastJournalCountsRow(
            period_start="2026-09-21",
            period_end="2026-09-28",
            target_spec_id=forecast_fixture.FEEDERS,
            horizon="336h",
            cards_issued=3,
            cards_released=1,
            cards_no_event=0,
            cards_unknown=0,
            cards_open=1,
        )


def test_frequency_share_labels_and_wording():
    card = json.loads(
        json.dumps(forecast_fixture.forecast_card(OPEN).card.model_dump(), default=str)
    )
    bucket = card["score"]["frequency"]
    with pytest.raises(ValidationError, match="numerator and denominator"):
        FrequencyBucket.model_validate(bucket | {"share": 0.5})
    with pytest.raises(ValidationError, match="Wilson interval"):
        FrequencyBucket.model_validate(bucket | {"wilson_high": bucket["share"] - 0.01})
    cases = [
        (lambda p: p["score"].update(value=0.1), "differs from its bucket share"),
        (lambda p: p["score"].update(label="оценка риска"), "label must match"),
        (lambda p: p.update(incident_type="sensor_connection_loss"), "incident type differs"),
        (lambda p: p["versions"].update(recurrence_rule_version=None), "recurrence requires"),
        (lambda p: p["facts"][0].update(text="Фидер вышел из строя"), "forbidden card wording"),
    ]
    for mutate, message in cases:
        payload = json.loads(json.dumps(card))
        mutate(payload)
        with pytest.raises(ValidationError, match=message):
            ForecastCardView.model_validate({"card": payload})


def test_metric_decision_and_import_rules():
    with pytest.raises(ValidationError, match="numerator and denominator"):
        RatioMetric(
            name="recall_any", definition_version="v", numerator=1, denominator=2, value=0.4
        )
    assert RatioMetric(name="x", definition_version="v", numerator=0, denominator=0).value is None
    request = {
        "decision_code": "R2",
        "reason_code": "R2.3",
        "reason_text": "событие объекта на нескольких каналах",
        "verification_methods": ["source_records"],
        "idempotency_key": "synthetic-key-0001",
        "expected_revision": 0,
    }
    with pytest.raises(ValidationError, match="draft note"):
        ForecastDecisionCreate.model_validate(request | {"draft_note": "выезд"})
    with pytest.raises(ValidationError, match="recipient and time"):
        ForecastDecisionCreate.model_validate(request | {"notified_to": "энергетик"})
    summary = journal_entry(RELEASED).decision.model_dump()
    with pytest.raises(ValidationError, match="later than the decision"):
        ForecastDecisionSummary.model_validate(
            summary | {"notified_at": summary["decided_at"] + timedelta(minutes=1)}
        )
    published = product_fixture.imports()[0].model_dump()
    with pytest.raises(ValidationError, match="quarantine reasons do not add up"):
        ImportFile.model_validate(published | {"quarantine_reasons": {"bad_date": 1}})
    with pytest.raises(ValidationError, match="only a published import"):
        ImportFile.model_validate(
            published | {"status": "imported", "finished_at": None, "forecast_generation": None}
        )
    with pytest.raises(ValidationError, match="expiry"):
        Me(subject_id="x", display_name="x", roles=["analyst"], auth_source="ldap")


def test_imports_fixture_routes(client):
    listed = ImportList.model_validate(client.get("/api/v1/imports").json())
    assert {item.status for item in listed.items} == {"published", "duplicate", "failed"}
    upload = client.post(
        "/api/v1/imports",
        files={"file": ("synthetic.csv", b'"id"\n1\n', "text/csv")},
        data={"format": "journal_csv"},
    )
    assert upload.status_code == 202
    queued = ImportFile.model_validate(upload.json())
    assert (queued.status, queued.simulated, queued.size_bytes) == ("queued", True, 7)
    assert client.get("/api/v1/imports/missing").status_code == 404


def test_decision_fixture_routes(client):
    listed = ForecastDecisionList.model_validate(
        client.get(f"/api/v1/forecasts/{OPEN}/decisions").json()
    )
    assert [item.revision for item in listed.items] == [1]
    body = {
        "decision_code": "R3",
        "reason_code": "R3.1",
        "reason_text": "устойчивая или повторная потеря связи",
        "verification_methods": ["source_records"],
        "idempotency_key": "synthetic-key-0002",
        "expected_revision": 1,
        "draft_note": "осмотр фидеров",
        "notified_to": "дежурный энергетик (синтетика)",
        "notified_at": "2026-09-24T09:30:00+03:00",
        "awaiting_result_until": "2026-09-26T17:00:00+03:00",
    }
    created = client.post(f"/api/v1/forecasts/{OPEN}/decisions", json=body)
    assert created.status_code == 201, created.json()
    decision = ForecastDecisionSummary.model_validate(created.json())
    assert (decision.revision, decision.draft_status, decision.notified_to) == (
        2,
        "not_sent",
        body["notified_to"],
    )
    late = client.post(
        f"/api/v1/forecasts/{OPEN}/decisions",
        json=body | {"notified_at": "2026-09-25T09:30:00+03:00"},
    )
    assert (late.status_code, late.json()["detail"]) == (422, "notified_after_decision")
    stale = client.post(f"/api/v1/forecasts/{OPEN}/decisions", json=body | {"expected_revision": 0})
    assert (stale.status_code, stale.json()["detail"]) == (409, "decision_revision_conflict")


def test_research_is_for_analysts_and_admins_only(client, monkeypatch):
    summary = ResearchSummary.model_validate(client.get("/api/v1/research-summary").json())
    assert summary.synthetic and summary.blocks
    assert any(row.step == "random_list" for scope in summary.scopes for row in scope.ladder)
    dispatcher = Me(
        subject_id="synthetic-dispatcher",
        display_name="Синтетический диспетчер",
        roles=["dispatcher"],
        auth_source="dev_stub",
    )
    monkeypatch.setattr(auth_deps, "LOCAL_OPERATOR", dispatcher)
    app = create_app(Settings(mode="fixture", _env_file=None))
    app.dependency_overrides[auth_deps.current_actor] = lambda: dispatcher
    as_dispatcher = TestClient(app)
    for path in ("/api/v1/research-summary", "/api/v1/forecast-journal/quality"):
        response = as_dispatcher.get(path)
        assert (response.status_code, response.json()["detail"]) == (403, "forbidden_role"), path
    for path in ("/api/v1/forecasts", "/api/v1/forecast-journal/summary", "/api/v1/schemes"):
        assert as_dispatcher.get(path).status_code == 200, path


def test_research_summary_from_file(tmp_path):
    summary = product_fixture.research_summary()
    path = tmp_path / "research.json"
    real = summary.model_dump(mode="json") | {"synthetic": False, "source_reports": ["report.json"]}
    path.write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")
    replay = TestClient(
        create_app(Settings(mode="replay", research_summary_path=path, _env_file=None))
    )
    assert replay.get("/api/v1/research-summary").json()["source_reports"] == ["report.json"]
    path.write_text(json.dumps(real | {"source_reports": []}), encoding="utf-8")
    invalid = replay.get("/api/v1/research-summary")
    assert (invalid.status_code, invalid.json()["detail"]) == (503, "research_summary_invalid")


def test_auth_stub_and_ldap_mode_gate():
    client = TestClient(create_app(Settings(mode="fixture", _env_file=None)))
    me = Me.model_validate(client.get("/api/v1/auth/me").json())
    assert me.auth_source == "dev_stub" and set(me.roles) == {"dispatcher", "analyst", "admin"}
    # B3: without a session every data route is 401; full matrix in test_rbac_matrix.py.
    ldap = TestClient(create_app(Settings(mode="fixture", auth_mode="ldap", _env_file=None)))
    for path in ("/api/v1/auth/me", "/api/v1/forecasts", "/api/v1/research-summary"):
        response = ldap.get(path)
        assert (response.status_code, response.json()["detail"]) == (401, "not_authenticated")
    assert ldap.get("/health/live").status_code == 200


def test_generated_fixtures_match_contract():
    published = json.loads((ROOT / "contracts/product.fixture.json").read_text(encoding="utf-8"))
    assert published == json.loads(json.dumps(product_fixture.product_fixture_document()))
    forecast = json.loads((ROOT / "contracts/forecast.fixture.json").read_text(encoding="utf-8"))
    assert forecast == json.loads(json.dumps(forecast_fixture.forecast_fixture_document()))
    ForecastJournalCounts.model_validate(forecast["journal_counts"])
    ObjectSchemeList.model_validate(forecast["schemes"])
    ObjectScheme.model_validate(forecast["scheme_example"])


def test_new_routes_do_not_import_research_or_ml():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
class BlockResearch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {
            'infra_pulse_research', 'mlflow', 'numpy', 'pandas',
            'catboost', 'sklearn', 'duckdb', 'pyarrow'
        }:
            raise AssertionError('HTTP imported ' + fullname)
sys.meta_path.insert(0, BlockResearch())
from fastapi.testclient import TestClient
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
with TestClient(create_app(Settings(mode='fixture', _env_file=None))) as client:
    for path in ('/api/v1/forecast-state', '/api/v1/forecast-journal/summary',
                 '/api/v1/schemes', '/api/v1/imports', '/api/v1/auth/me',
                 '/api/v1/research-summary', '/api/v1/registries/recurring'):
        assert client.get(path).status_code == 200, path
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
