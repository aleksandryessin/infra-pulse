"""Dispatcher decisions on forecast cards (API-03, API-04, SEC-03). **Owned by package B3.**

Fixture mode validates the request and returns a simulated, unsaved decision.
replay/received store decision, draft («не отправлен» for R3/R4) and audit in one
transaction with the session author (``storage/decisions_pg.py``): a repeated
``idempotency_key`` returns the stored decision, a stale ``expected_revision`` is 409.
«Кому и когда сообщено» (``notified_to``/``notified_at``, C0.1) is stored with the
decision and its audit row; a notification later than the decision time is 422
``notified_after_decision``. The card must exist in the published forecast (B2
``forecast_db.forecast_card``): an unknown card is 404, a scope without a publication
(no DSN, scope or run) is 503 ``forecast_not_implemented``.

C0.4: R3 «Сообщено энергетику» needs recipient, time and ``awaiting_result_until``, R1
«Под наблюдением» ``watch_until`` (422 from the contract); a deadline not after the
decision is 422 ``deadline_before_decision``. ``POST …/check-result`` stores the check
result revision with audit (``storage/check_results_pg.py``); ``event_cause`` only for a
card whose event was registered (422 ``event_cause_requires_event``).
"""

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request

from infra_pulse_backend.api import forecast_db, forecast_fixture
from infra_pulse_backend.api.auth_deps import (
    acting_role,
    client_address,
    request_id,
    require_permission,
)
from infra_pulse_backend.config import Settings
from infra_pulse_backend.storage import check_results_pg, decisions_pg
from infra_pulse_core.contracts.auth import Me
from infra_pulse_core.contracts.forecast import (
    ForecastCardView,
    ForecastCheckResult,
    ForecastCheckResultCreate,
    ForecastCheckResultList,
    ForecastDecisionCreate,
    ForecastDecisionList,
    ForecastDecisionSummary,
)

MAX_FORECAST_ID = 256


def build_router(config: Settings) -> APIRouter:
    router = APIRouter()
    fixture = config.mode == "fixture"

    def storage_dsn() -> str:
        if config.db_dsn is None:
            raise HTTPException(status_code=503, detail="decisions_storage_not_configured")
        return config.db_dsn.get_secret_value()

    def ensure_card(forecast_id: str) -> ForecastCardView:
        if len(forecast_id) > MAX_FORECAST_ID:
            raise HTTPException(status_code=404, detail="forecast_not_found")
        try:
            card = forecast_db.forecast_card(config, forecast_id, as_of=None)
        except forecast_db.ForecastNotImplemented as error:
            raise HTTPException(status_code=503, detail="forecast_not_implemented") from error
        if card is None:
            raise HTTPException(status_code=404, detail="forecast_not_found")
        return card

    def check_event_cause(request: ForecastCheckResultCreate, view: ForecastCardView) -> None:
        if request.event_cause is not None and view.list_state != "released":
            raise HTTPException(status_code=422, detail="event_cause_requires_event")

    def check_reason(request: ForecastDecisionCreate) -> None:
        if not decisions_pg.reason_matches_code(request):
            raise HTTPException(status_code=422, detail="reason_code_mismatch")

    @router.get(
        "/api/v1/forecasts/{forecast_id}/decisions",
        response_model=ForecastDecisionList,
        dependencies=[Depends(require_permission("read"))],
    )
    def decisions(forecast_id: str) -> ForecastDecisionList:
        if fixture:
            found = forecast_fixture.decision_list(forecast_id)
            if found is None:
                raise HTTPException(status_code=404, detail="forecast_not_found")
            return found
        dsn = storage_dsn()
        ensure_card(forecast_id)
        try:
            return decisions_pg.read_decisions(dsn, forecast_id)
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="decisions_storage_unavailable") from error

    @router.post(
        "/api/v1/forecasts/{forecast_id}/decisions",
        response_model=ForecastDecisionSummary,
        status_code=201,
    )
    def decide(
        forecast_id: str,
        request: ForecastDecisionCreate,
        actor: Annotated[Me, Depends(require_permission("decide"))],
        http_request: Request,
    ) -> ForecastDecisionSummary:
        check_reason(request)
        role = acting_role(actor, "decide")
        if fixture:
            current = forecast_fixture.decision_list(forecast_id)
            if current is None:
                raise HTTPException(status_code=404, detail="forecast_not_found")
            revision = current.items[0].revision if current.items else 0
            if request.expected_revision != revision:
                raise HTTPException(status_code=409, detail="decision_revision_conflict")
            if (
                request.notified_at is not None
                and request.notified_at > forecast_fixture.FIXTURE_AS_OF
            ):
                raise HTTPException(status_code=422, detail="notified_after_decision")
            if any(
                deadline is not None and deadline <= forecast_fixture.FIXTURE_AS_OF
                for deadline in (request.awaiting_result_until, request.watch_until)
            ):
                raise HTTPException(status_code=422, detail="deadline_before_decision")
            draft = request.decision_code in decisions_pg.DRAFT_CODES
            return ForecastDecisionSummary(
                decision_code=request.decision_code,
                reason_code=request.reason_code,
                reason_text=request.reason_text,
                dictionary_version="synthetic-technologist-dictionary-v0",
                actor_id=actor.subject_id,
                actor_role=role,
                decided_at=forecast_fixture.FIXTURE_AS_OF,
                revision=revision + 1,
                simulated=True,
                verification_methods=request.verification_methods,
                notified_to=request.notified_to,
                notified_at=request.notified_at,
                awaiting_result_until=request.awaiting_result_until,
                watch_until=request.watch_until,
                draft_id=f"synthetic-draft-{forecast_id}" if draft else None,
                draft_status="not_sent" if draft else None,
            )
        dsn = storage_dsn()
        ensure_card(forecast_id)
        try:
            return decisions_pg.create_decision(
                dsn,
                forecast_id=forecast_id,
                request=request,
                actor=actor,
                actor_role=role,
                request_id=request_id(http_request),
                client_address=client_address(http_request),
            )
        except decisions_pg.DecisionRevisionConflict as error:
            raise HTTPException(status_code=409, detail="decision_revision_conflict") from error
        except decisions_pg.IdempotencyKeyReused as error:
            raise HTTPException(status_code=409, detail="idempotency_key_reused") from error
        except decisions_pg.NotifiedAfterDecision as error:
            raise HTTPException(status_code=422, detail="notified_after_decision") from error
        except decisions_pg.DeadlineBeforeDecision as error:
            raise HTTPException(status_code=422, detail="deadline_before_decision") from error
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="decisions_storage_unavailable") from error

    @router.get(
        "/api/v1/forecasts/{forecast_id}/check-results",
        response_model=ForecastCheckResultList,
        dependencies=[Depends(require_permission("read"))],
    )
    def check_results(forecast_id: str) -> ForecastCheckResultList:
        if fixture:
            found = forecast_fixture.check_result_list(forecast_id)
            if found is None:
                raise HTTPException(status_code=404, detail="forecast_not_found")
            return found
        dsn = storage_dsn()
        ensure_card(forecast_id)
        try:
            return check_results_pg.read_check_results(dsn, forecast_id)
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="decisions_storage_unavailable") from error

    @router.post(
        "/api/v1/forecasts/{forecast_id}/check-result",
        response_model=ForecastCheckResult,
        status_code=201,
    )
    def record_check_result(
        forecast_id: str,
        request: ForecastCheckResultCreate,
        actor: Annotated[Me, Depends(require_permission("decide"))],
        http_request: Request,
    ) -> ForecastCheckResult:
        role = acting_role(actor, "decide")
        if fixture:
            view = forecast_fixture.forecast_card(forecast_id)
            current = forecast_fixture.check_result_list(forecast_id)
            if view is None or current is None:
                raise HTTPException(status_code=404, detail="forecast_not_found")
            check_event_cause(request, view)
            revision = current.items[0].revision if current.items else 0
            if request.expected_revision != revision:
                raise HTTPException(status_code=409, detail="check_result_revision_conflict")
            if request.result_at > forecast_fixture.FIXTURE_AS_OF:
                raise HTTPException(status_code=422, detail="result_after_record")
            return ForecastCheckResult(
                forecast_id=forecast_id,
                revision=revision + 1,
                check_result=request.check_result,
                found=request.found,
                found_other_text=request.found_other_text,
                result_at=request.result_at,
                comment=request.comment,
                event_cause=request.event_cause,
                actor_id=actor.subject_id,
                actor_role=role,
                recorded_at=forecast_fixture.FIXTURE_AS_OF,
                simulated=True,
            )
        dsn = storage_dsn()
        check_event_cause(request, ensure_card(forecast_id))
        try:
            return check_results_pg.create_check_result(
                dsn,
                forecast_id=forecast_id,
                request=request,
                actor=actor,
                actor_role=role,
                request_id=request_id(http_request),
                client_address=client_address(http_request),
            )
        except check_results_pg.CheckResultRevisionConflict as error:
            raise HTTPException(status_code=409, detail="check_result_revision_conflict") from error
        except check_results_pg.IdempotencyKeyReused as error:
            raise HTTPException(status_code=409, detail="idempotency_key_reused") from error
        except check_results_pg.ResultAfterRecord as error:
            raise HTTPException(status_code=422, detail="result_after_record") from error
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="decisions_storage_unavailable") from error

    return router
