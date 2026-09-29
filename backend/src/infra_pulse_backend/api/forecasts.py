"""Forecast read routes. Fixture mode serves ``forecast_fixture``; replay/received call
``forecast_db`` (B2). The routes and their parameters are the contract; data sources
change behind them.
"""

from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from infra_pulse_backend.api import forecast_db, forecast_fixture
from infra_pulse_backend.api.auth_deps import require_permission
from infra_pulse_backend.config import Settings
from infra_pulse_core.contracts.forecast import (
    ForecastCardView,
    ForecastJournalCounts,
    ForecastJournalList,
    ForecastList,
    ForecastQualitySummary,
    ForecastState,
    Horizon,
    ListState,
    OutcomeStatus,
    RecurringPlaceList,
    RiskLevel,
    TargetSpecId,
)
from infra_pulse_core.contracts.scheme import ObjectScheme, ObjectSchemeList

_STALE = (forecast_fixture.StaleForecastCursor, forecast_db.StaleForecastCursor)
_BAD_CURSOR = (forecast_fixture.ForecastCursorError, forecast_db.ForecastCursorError)


def _aware(as_of: datetime | None) -> datetime | None:
    if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
        raise HTTPException(status_code=422, detail="aware_as_of_required")
    return as_of


def build_router(config: Settings) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_permission("read"))])
    # Quality ratios (P, R) are research-side: analysts and administrators only.
    research = [Depends(require_permission("research"))]
    fixture = config.mode == "fixture"

    def unavailable() -> HTTPException:
        return HTTPException(status_code=503, detail="forecast_not_implemented")

    @router.get("/api/v1/forecasts", response_model=ForecastList)
    def forecasts(
        horizon: Horizon | None = None,
        target_spec_id: TargetSpecId | None = None,
        risk_level: RiskLevel | None = None,
        status: Literal["scored", "abstained"] | None = None,
        sensor_type: str | None = Query(default=None, min_length=1, max_length=256),
        object_id: str | None = Query(default=None, min_length=1, max_length=256),
        list_state: ListState = "open",
        released_since: datetime | None = None,
        as_of: datetime | None = None,
        cursor: str | None = Query(default=None, min_length=1, max_length=64),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> ForecastList:
        filters = {
            "horizon": horizon,
            "target_spec_id": target_spec_id,
            "risk_level": risk_level,
            "status": status,
            "sensor_type": sensor_type,
            "object_id": object_id,
            "list_state": list_state,
            "released_since": _aware(released_since),
            "cursor": cursor,
            "limit": limit,
        }
        try:
            if fixture:
                return forecast_fixture.forecast_list(**filters)
            return forecast_db.forecast_list(config, as_of=_aware(as_of), **filters)
        except forecast_db.ForecastNotImplemented as error:
            raise unavailable() from error
        except _STALE as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except _BAD_CURSOR as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/api/v1/forecasts/{forecast_id}", response_model=ForecastCardView)
    def forecast(forecast_id: str, as_of: datetime | None = None) -> ForecastCardView:
        try:
            if fixture:
                card = forecast_fixture.forecast_card(forecast_id)
            else:
                card = forecast_db.forecast_card(config, forecast_id, as_of=_aware(as_of))
        except forecast_db.ForecastNotImplemented as error:
            raise unavailable() from error
        if card is None:
            raise HTTPException(status_code=404, detail="forecast_not_found")
        return card

    @router.get("/api/v1/forecast-journal", response_model=ForecastJournalList)
    def forecast_journal(
        outcome: OutcomeStatus | None = None,
        horizon: Horizon | None = None,
        target_spec_id: TargetSpecId | None = None,
        object_id: str | None = Query(default=None, min_length=1, max_length=256),
        list_state: ListState | None = None,
        issued_from: date | None = None,
        issued_to: date | None = None,
        decision_state: Literal["none", "any"] | None = None,
        as_of: datetime | None = None,
        cursor: str | None = Query(default=None, min_length=1, max_length=64),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> ForecastJournalList:
        """Журнал прогнозов: снимок карточки, исход по данным на `as_of` и последнее решение
        и результат проверки, сохранённые к `records_as_of` (по часам сервера, даже если
        позже данных). Без `as_of` — текущий журнал; с `as_of` решения тоже не позже него."""
        if issued_from and issued_to and issued_from > issued_to:
            raise HTTPException(status_code=422, detail="issued_range_reversed")
        filters = {
            "outcome": outcome,
            "horizon": horizon,
            "target_spec_id": target_spec_id,
            "object_id": object_id,
            "list_state": list_state,
            "issued_from": issued_from,
            "issued_to": issued_to,
            "decision_state": decision_state,
            "cursor": cursor,
            "limit": limit,
        }
        try:
            if fixture:
                return forecast_fixture.forecast_journal(**filters)
            return forecast_db.forecast_journal(config, as_of=_aware(as_of), **filters)
        except forecast_db.ForecastNotImplemented as error:
            raise unavailable() from error
        except _BAD_CURSOR as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/api/v1/forecast-journal/summary", response_model=ForecastJournalCounts)
    def forecast_counts(as_of: datetime | None = None) -> ForecastJournalCounts:
        """Dispatcher counters: выдано / снята по событию / без события / неизвестно / открыто."""
        try:
            if fixture:
                return forecast_fixture.journal_counts()
            return forecast_db.journal_counts(config, as_of=_aware(as_of))
        except forecast_db.ForecastNotImplemented as error:
            raise unavailable() from error

    @router.get(
        "/api/v1/forecast-journal/quality",
        response_model=ForecastQualitySummary,
        dependencies=research,
    )
    def forecast_quality(as_of: datetime | None = None) -> ForecastQualitySummary:
        try:
            if fixture:
                return forecast_fixture.quality_summary()
            return forecast_db.quality_summary(config, as_of=_aware(as_of))
        except forecast_db.ForecastNotImplemented as error:
            raise unavailable() from error

    @router.get("/api/v1/forecast-state", response_model=ForecastState)
    def forecast_state() -> ForecastState:
        try:
            if fixture:
                return forecast_fixture.forecast_state()
            return forecast_db.forecast_state(config)
        except forecast_db.ForecastNotImplemented as error:
            raise unavailable() from error

    @router.get("/api/v1/registries/recurring", response_model=RecurringPlaceList)
    def recurring(as_of: datetime | None = None) -> RecurringPlaceList:
        try:
            if fixture:
                return forecast_fixture.recurring_places()
            return forecast_db.recurring_places(config, as_of=_aware(as_of))
        except forecast_db.ForecastNotImplemented as error:
            raise unavailable() from error

    @router.get("/api/v1/schemes", response_model=ObjectSchemeList)
    def schemes(as_of: datetime | None = None) -> ObjectSchemeList:
        try:
            if fixture:
                return forecast_fixture.scheme_list()
            return forecast_db.scheme_list(config, as_of=_aware(as_of))
        except forecast_db.ForecastNotImplemented as error:
            raise unavailable() from error

    @router.get("/api/v1/schemes/{object_id}", response_model=ObjectScheme)
    def scheme(object_id: str, as_of: datetime | None = None) -> ObjectScheme:
        try:
            if fixture:
                found = forecast_fixture.object_scheme(object_id)
            else:
                found = forecast_db.object_scheme(config, object_id, as_of=_aware(as_of))
        except forecast_db.ForecastNotImplemented as error:
            raise unavailable() from error
        if found is None:
            raise HTTPException(status_code=404, detail="object_not_found")
        return found

    return router
