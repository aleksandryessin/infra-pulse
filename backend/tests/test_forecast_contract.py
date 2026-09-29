"""Forecast contract rules on the gate A examples (``legacy_*`` builders).

The served fixture scenario («обесточивание фидеров объекта», rolling 14-day list) is
tested in ``test_feeder_forecast_contract.py``.
"""

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from infra_pulse_backend.api import forecast_fixture
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_core.contracts import forecast as contract
from infra_pulse_core.contracts.forecast import (
    EVENT_LABEL,
    ForecastCard,
    ForecastCardView,
    ForecastDecisionSummary,
    ForecastJournalEntry,
    ForecastJournalList,
    ForecastList,
    ForecastOutcome,
    check_card_wording,
)

ROOT = Path(__file__).resolve().parents[2]
SCORED = "synthetic-forecast-24h-001"
OBSERVED = "synthetic-forecast-24h-002"
ABSTAINED = "synthetic-forecast-24h-004"
WEEKLY = "synthetic-forecast-168h-001"
REALIZED = "synthetic-forecast-24h-p01"
INTERVENED = "synthetic-forecast-24h-p02"
EVENT_NO_FORECAST = "synthetic-forecast-24h-p04"
NO_EVENT_NO_FORECAST = "synthetic-forecast-24h-p05"
TECHNICAL_VALUE = "sensor-failure/technical_value"
ENDPOINTS = ("/api/v1/forecasts", f"/api/v1/forecasts/{SCORED}", "/api/v1/forecast-journal")


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(mode="fixture", _env_file=None)))


def card(card_id: str) -> ForecastCard:
    return next(item for item in forecast_fixture.legacy_current_cards() if item.id == card_id)


def entry(card_id: str) -> ForecastJournalEntry:
    return next(
        item for item in forecast_fixture.legacy_journal_entries() if item.card.id == card_id
    )


def legacy_view(card_id: str) -> ForecastCardView:
    return next(view for view in forecast_fixture.legacy_current_views() if view.card.id == card_id)


@pytest.mark.parametrize("mode", ["scaffold", "replay", "received"])
def test_forecasts_exist_only_as_fixture(mode):
    client = TestClient(create_app(Settings(mode=mode, _env_file=None)))
    for path in ENDPOINTS:
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()["detail"] == "forecast_not_implemented"


def test_gate_a_examples_cover_risk_estimate_cases():
    views = forecast_fixture.legacy_current_views()
    cards = [view.card for view in views]
    assert len(cards) == 5
    ids = {
        value
        for item in cards
        for value in (
            item.id,
            item.object_id,
            item.versions.release_id,
            item.versions.run_id,
            *(channel.channel_id for channel in item.channels),
            *item.active_episode_channel_ids,
        )
    }
    assert all(value.startswith("synthetic-") for value in ids)
    assert all(item.event_label == EVENT_LABEL for item in cards)
    assert all(item.incident_type != "power_input_loss" for item in cards)
    assert all(item.score is None or item.score.kind == "risk_estimate" for item in cards)
    # Calibrated value is still shown as a risk estimate until ML-05 approves probability.
    assert all(item.versions.calibration_version for item in cards)

    scored = card(SCORED)
    assert scored.horizon == "24h" and scored.status == "scored"
    assert len(scored.channels) >= 2
    assert {channel.picket_form for channel in scored.channels} == {"point", "range"}
    assert scored.score.label == "оценка риска"
    bucket = scored.score.rank_bucket
    assert 0 <= scored.score.value <= 1
    assert scored.score.value != bucket.calibration.event_rate  # not required to be equal
    assert (bucket.bucket_starts, bucket.rank_from, bucket.rank_to) == ([1, 6, 11, 51], 1, 5)
    assert bucket.policy_id.endswith("connection_loss_1h-24h-v0")
    assert bucket.source_report == "synthetic-fixture-not-a-metric"
    assert scored.risk_level == bucket.level == "high"
    assert scored.window_end - scored.window_start == timedelta(hours=24)

    abstained = card(ABSTAINED)
    assert abstained.abstention_reason == "insufficient_history"
    assert (abstained.score, abstained.risk_level) == (None, "unknown")
    assert abstained.channels  # the channels it would have covered

    weekly = card(WEEKLY)
    weekly_bucket = weekly.score.rank_bucket
    assert weekly.window_end - weekly.window_start == timedelta(days=7)
    assert (weekly.score.rank, weekly.score.card_budget, weekly.risk_level) == (12, 50, "medium")
    assert (weekly_bucket.bucket_starts, weekly_bucket.rank_from, weekly_bucket.rank_to) == (
        [1, 11, 21, 51],
        11,
        20,
    )

    observed = next(view for view in views if view.already_observed)
    assert observed.card.id == OBSERVED
    overlay = observed.observed_overlay
    assert overlay.kind == "already_observed" and overlay.forecast_recomputed is False
    assert {(item.value_raw, item.alarm) for item in overlay.items} == {
        ("-127", False),
        ("Не определено", True),
    }
    assert "observed_overlay" not in ForecastCard.model_fields

    continuation = next(item for item in cards if item.continuation_of)
    assert continuation.continuation_of in {
        item.card.id for item in forecast_fixture.legacy_journal_entries()
    }


def test_fixture_text_has_no_diagnosis_or_probability_wording():
    text = json.dumps(forecast_fixture.forecast_fixture_document(), ensure_ascii=False).lower()
    for word in ("поломк", "вероятност", "выйдет из строя", "требуется замена", "авари"):
        assert word not in text


def test_journal_keeps_bare_issued_cards_and_outcomes():
    entries = forecast_fixture.legacy_journal_entries()
    journal = ForecastJournalList(
        mode="fixture",
        as_of=forecast_fixture.FIXTURE_AS_OF,
        journal_watermark=len(entries),
        items=list(reversed(entries)),
        total=len(entries),
        limit=100,
    )
    assert journal.total == journal.journal_watermark == 10
    assert [item.journal_position for item in journal.items] == list(range(10, 0, -1))
    assert {item.outcome.status for item in journal.items} == {
        "pending",
        "realized",
        "not_realized",
        "event_without_forecast",
        "no_event_without_forecast",
        "unknown",
    }
    assert all(item.snapshot_immutable for item in journal.items)
    assert {item.card.id for item in journal.items if item.outcome.status == "pending"} == {
        item.id for item in forecast_fixture.legacy_current_cards()
    }
    for item in journal.items:
        assert item.outcome.excluded_from_quality_metrics == (item.card.status == "abstained")
        if item.card.status == "abstained":
            assert item.outcome.status not in ("realized", "not_realized")
    decisions = [item.decision for item in journal.items if item.decision is not None]
    assert decisions and all(decision.simulated for decision in decisions)
    assert all(decision.draft_status == "not_sent" for decision in decisions if decision.draft_id)

    realized = next(item for item in journal.items if item.outcome.status == "realized")
    assert realized.outcome.basis == "automatic_registered_event"
    assert realized.outcome.lead_hours == pytest.approx(11.4)
    without = next(item for item in journal.items if item.card.id == EVENT_NO_FORECAST)
    assert without.outcome.status == "event_without_forecast"
    assert without.outcome.lead_hours is None
    intervened = next(item for item in journal.items if item.outcome.intervention_before_window_end)
    assert intervened.outcome.status == "not_realized"
    assert intervened.outcome.other_events_on_object_count == 1  # not a capture
    assert intervened.decision.decision_code == "R3"
    assert (intervened.decision.check_result_code, intervened.decision.check_result_simulated) == (
        "O3",
        True,
    )
    unknown = next(item for item in journal.items if item.outcome.status == "unknown")
    assert unknown.outcome.unknown_reason == "source_coverage"

    snapshot = next(item.card for item in journal.items if item.card.id == OBSERVED)
    assert snapshot == card(OBSERVED)  # the journal keeps the bare issued card
    with pytest.raises(ValidationError):
        snapshot.risk_level = "low"


def test_probability_needs_calibration_and_approval(monkeypatch):
    payload = card(SCORED).model_dump()
    payload["score"].update(kind="probability", label="вероятность")
    payload["versions"]["calibration_version"] = None
    with pytest.raises(ValidationError, match="recorded calibration"):
        ForecastCard.model_validate(payload)
    payload["versions"]["calibration_version"] = "synthetic-isotonic-v0"
    with pytest.raises(ValidationError, match="not approved"):
        ForecastCard.model_validate(payload)
    monkeypatch.setattr(contract, "PROBABILITY_DISPLAY_APPROVED", True)
    assert ForecastCard.model_validate(payload).score.kind == "probability"
    payload["score"]["label"] = "оценка риска"
    with pytest.raises(ValidationError, match="label must match"):
        ForecastCard.model_validate(payload)


def test_abstention_and_object_binding_rules():
    mutations = [
        (ABSTAINED, lambda p: p.update(score=card(SCORED).model_dump()["score"]), "no score"),
        (
            ABSTAINED,
            lambda p: p["freshness"].update(history_days_available=30),
            "shorter than the lookback",
        ),
        (
            ABSTAINED,
            lambda p: p.update(abstention_detail="Прогноз не выдан: причина — обрыв линии"),
            "forbidden card wording",
        ),
        (ABSTAINED, lambda p: p.update(abstention_reason="no_object_binding"), "object binding"),
        (SCORED, lambda p: p.update(score=None), "requires a score"),
        (SCORED, lambda p: p["channels"][1].update(reason_facts=[]), "why it was included"),
        (SCORED, lambda p: p.update(object_id=None), "object binding"),
    ]
    for card_id, mutate, message in mutations:
        payload = card(card_id).model_dump()
        mutate(payload)
        with pytest.raises(ValidationError, match=message):
            ForecastCard.model_validate(payload)
    payload = card(ABSTAINED).model_dump()
    payload.update(object_id=None, abstention_reason="no_object_binding")
    assert ForecastCard.model_validate(payload).object_id is None


def test_time_semantics_forbid_leakage_and_wrong_windows():
    base = card(SCORED).model_dump()
    cases = [
        (("facts", 0, "period_end"), base["issued_at"] + timedelta(hours=1), "after the cutoff"),
        (("window_end",), base["window_start"] + timedelta(hours=48), "differs from the horizon"),
        (("window_start",), base["issued_at"] + timedelta(hours=1), "starts at the cutoff"),
        (("published_at",), base["window_end"], "published after the cutoff"),
        (("freshness", "data_as_of"), base["issued_at"] + timedelta(seconds=1), "watermark"),
    ]
    for path, value, message in cases:
        payload = card(SCORED).model_dump()
        target = payload
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValidationError, match=message):
            ForecastCard.model_validate(payload)


def test_overlay_is_post_issue_and_matches_flag():
    base = legacy_view(OBSERVED).model_dump()
    issued_at = base["card"]["issued_at"]
    mutations = [
        (
            lambda p: p["observed_overlay"]["items"][0].update(
                event_at=issued_at - timedelta(minutes=1)
            ),
            "observed after issue",
        ),
        (lambda p: p.update(already_observed=False), "must match the overlay"),
        (lambda p: p["observed_overlay"]["items"][1].update(alarm=False), "original alarm=true"),
    ]
    for mutate, message in mutations:
        payload = legacy_view(OBSERVED).model_dump()
        mutate(payload)
        with pytest.raises(ValidationError, match=message):
            ForecastCardView.model_validate(payload)


def test_rank_bucket_levels_channels_and_pickets_are_consistent():
    mutations = [
        (SCORED, lambda p: p["score"].update(rank=6), "outside its bucket"),
        (SCORED, lambda p: p["score"]["rank_bucket"].update(rank_to=10), "differ from its policy"),
        (SCORED, lambda p: p["score"]["rank_bucket"].update(rank_from=2), "differ from its policy"),
        (SCORED, lambda p: p["score"]["rank_bucket"].update(bucket_starts=[2, 6]), "begin at 1"),
        (
            SCORED,
            lambda p: p["score"]["rank_bucket"].update(bucket_starts=[1, 11, 6, 51]),
            "increase",
        ),
        (
            SCORED,
            lambda p: p["score"]["rank_bucket"].update(bucket_levels=["high"]),
            "needs a risk level",
        ),
        (SCORED, lambda p: p.update(risk_level="medium"), "follow the rank bucket policy"),
        (
            WEEKLY,
            lambda p: p["score"]["rank_bucket"].update(bucket_levels=["high", "low", "low", "low"]),
            "within the budget cannot be low",
        ),
        (
            SCORED,
            lambda p: p["score"]["rank_bucket"]["calibration"].update(event_rate=0.5),
            "numerator and denominator",
        ),
        (
            SCORED,
            lambda p: p.update(active_episode_channel_ids=["synthetic-smoke-011"]),
            "not a candidate",
        ),
        (SCORED, lambda p: p["channels"][1].update(picket_to=5), "ordered end"),
        (SCORED, lambda p: p["channels"][1].update(rank_in_card=3), "contiguous"),
    ]
    for card_id, mutate, message in mutations:
        payload = card(card_id).model_dump()
        mutate(payload)
        with pytest.raises(ValidationError, match=message):
            ForecastCard.model_validate(payload)


@pytest.mark.parametrize(
    "text",
    [
        "Ожидается поломка датчика",
        "Причина — обрыв шлейфа",
        "Вероятность события 5%",
        "Датчик исправен",
        "Состояние: норма",
        "Риск пожар на объекте",
    ],
)
def test_facts_reject_diagnosis_and_health_wording(text):
    with pytest.raises(ValueError):
        check_card_wording(text)
    payload = card(SCORED).model_dump()
    payload["facts"][0]["text"] = text
    with pytest.raises(ValidationError, match="forbidden card wording"):
        ForecastCard.model_validate(payload)


def test_fact_wording_keeps_source_terms():
    for text in ("Пожарная охрана: 2 эпизода", "Последняя запись «Неисправен»"):
        assert check_card_wording(text) == text


def test_outcome_semantics_for_scored_cards():
    window_end = card(SCORED).window_end
    with pytest.raises(ValidationError, match="no resolution"):
        ForecastOutcome(status="pending", label_version="v", resolved_at=window_end)
    with pytest.raises(ValidationError, match="no resolution"):
        ForecastOutcome(status="pending", label_version="v", other_events_on_object_count=0)
    with pytest.raises(ValidationError, match="its reason"):
        ForecastOutcome(status="unknown", label_version="v", resolved_at=window_end)
    with pytest.raises(ValidationError):
        ForecastOutcome(
            status="unknown",
            label_version="v",
            resolved_at=window_end,
            unknown_reason="incomplete_coverage",
        )
    base = entry(REALIZED).model_dump()
    realized_end = base["card"]["window_end"]
    mutations = [
        ({"first_event_at": realized_end, "lead_hours": 24.0}, "outside the window"),
        ({"lead_hours": 5.0}, "lead differs"),
        ({"lead_hours": None}, "lead is measured only"),
        ({"resolved_at": realized_end - timedelta(hours=1)}, "after the window ends"),
        ({"label_version": "synthetic-other-labels"}, "label definition"),
        # An event on a non-candidate channel of the object is not a capture.
        ({"event_channel_ids": ["synthetic-smoke-013"]}, "candidate channels"),
        ({"excluded_from_quality_metrics": True}, "excluded from quality metrics"),
    ]
    for update, message in mutations:
        payload = entry(REALIZED).model_dump()
        payload["outcome"].update(update)
        with pytest.raises(ValidationError, match=message):
            ForecastJournalEntry.model_validate(payload)
    payload = entry(REALIZED).model_dump()
    payload["outcome"].update(status="event_without_forecast", lead_hours=None)
    payload["outcome"]["excluded_from_quality_metrics"] = True
    with pytest.raises(ValidationError, match="excluded from quality metrics"):
        ForecastJournalEntry.model_validate(payload)


def test_abstained_cards_never_realize():
    base = entry(EVENT_NO_FORECAST).model_dump()
    first_event_at = base["outcome"]["first_event_at"]
    lead = (first_event_at - base["card"]["issued_at"]).total_seconds() / 3600
    mutations = [
        ({"status": "realized", "lead_hours": lead}, "does not apply to abstained cards"),
        ({"lead_hours": lead}, "lead is measured only"),
        ({"excluded_from_quality_metrics": False}, "excluded from quality metrics"),
        ({"event_channel_ids": ["synthetic-smoke-999"]}, "candidate channels"),
    ]
    for update, message in mutations:
        payload = entry(EVENT_NO_FORECAST).model_dump()
        payload["outcome"].update(update)
        with pytest.raises(ValidationError, match=message):
            ForecastJournalEntry.model_validate(payload)
    payload = entry(NO_EVENT_NO_FORECAST).model_dump()
    payload["outcome"].update(status="not_realized")
    with pytest.raises(ValidationError, match="does not apply"):
        ForecastJournalEntry.model_validate(payload)


def test_decision_dictionary_rules_and_simulation_marks():
    base = entry(INTERVENED).model_dump()["decision"]
    with pytest.raises(ValidationError, match="work-order draft"):
        ForecastDecisionSummary.model_validate(base | {"decision_code": "R1"})
    with pytest.raises(ValidationError, match="work-order draft"):
        ForecastDecisionSummary.model_validate(base | {"draft_id": None, "draft_status": None})
    with pytest.raises(ValidationError, match="marked simulation"):
        ForecastDecisionSummary.model_validate(base | {"check_result_simulated": False})
    payload = entry(INTERVENED).model_dump()
    payload["decision"] = None
    with pytest.raises(ValidationError, match="intervention flag"):
        ForecastJournalEntry.model_validate(payload)
    journal = forecast_fixture.forecast_journal().model_dump()
    next(item for item in journal["items"] if item["decision"])["decision"]["simulated"] = False
    with pytest.raises(ValidationError, match="marked simulated"):
        ForecastJournalList.model_validate(journal)


def test_list_cards_belong_to_published_runs():
    base = forecast_fixture.forecast_list().model_dump()
    payload = forecast_fixture.forecast_list().model_dump()
    # A rolling run accepts cards of this or earlier cutoffs only.
    payload["runs"][0]["issued_at"] = payload["items"][0]["card"]["issued_at"] - timedelta(days=1)
    payload["runs"][0]["published_at"] = payload["runs"][0]["issued_at"]
    with pytest.raises(ValidationError, match="currently published run"):
        ForecastList.model_validate(payload)
    payload = forecast_fixture.forecast_list().model_dump()
    payload["runs"].append(base["runs"][0])
    with pytest.raises(ValidationError, match="one published run"):
        ForecastList.model_validate(payload)


def test_generated_forecast_fixture_matches_contract():
    published = json.loads((ROOT / "contracts/forecast.fixture.json").read_text(encoding="utf-8"))
    assert published == json.loads(json.dumps(forecast_fixture.forecast_fixture_document()))
    ForecastList.model_validate(published["forecasts"])
    ForecastJournalList.model_validate(published["journal"])


def test_forecast_fixture_api_does_not_import_research_or_ml():
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
    for path in ('/api/v1/forecasts', '/api/v1/forecast-journal'):
        assert client.get(path).status_code == 200, path
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
