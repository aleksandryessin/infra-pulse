"""In-app notifications: new cards and critical source alarms. **Owned by package N1.**

fixture: a synthetic summary (C0.2). replay/received: ``notifications_db`` — new cards
from the forecast journal (B2) and critical source records by the technologist's policy
(``operations/notifications.py``) up to the stand clock of the published state. Until B2
lands the answer is 503 ``forecast_not_implemented``; scaffold answers 503
``notifications_not_implemented``.
"""

from datetime import datetime

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response

from infra_pulse_backend.api import forecast_db, forecast_fixture, notifications_db, product_fixture
from infra_pulse_backend.api.auth_deps import require_permission
from infra_pulse_backend.config import Settings
from infra_pulse_backend.operations.notifications import (
    NotificationWindowError,
    StandClockUnavailable,
)
from infra_pulse_core.contracts.notifications import MAX_NOTIFICATION_WINDOW, NotificationSummary


def build_router(config: Settings) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_permission("read"))])
    fixture = config.mode == "fixture"

    def from_database(since: datetime) -> NotificationSummary:
        if config.mode not in ("replay", "received"):
            raise HTTPException(status_code=503, detail="notifications_not_implemented")
        try:
            return notifications_db.notification_summary(config, since=since)
        except NotificationWindowError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except forecast_db.ForecastNotImplemented as error:
            raise HTTPException(status_code=503, detail="forecast_not_implemented") from error
        except (StandClockUnavailable, notifications_db.ObservationScopeNotConfigured) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=503, detail="observation_scope_not_loaded") from error
        except psycopg.Error as error:
            raise HTTPException(
                status_code=503, detail="notifications_storage_unavailable"
            ) from error

    @router.get("/api/v1/notifications", response_model=NotificationSummary)
    def notifications(since: datetime, response: Response) -> NotificationSummary:
        response.headers["Cache-Control"] = "no-store"
        if since.tzinfo is None or since.utcoffset() is None:
            raise HTTPException(status_code=422, detail="aware_since_required")
        if not fixture:
            return from_database(since)
        as_of = forecast_fixture.FIXTURE_AS_OF
        if since > as_of:
            raise HTTPException(status_code=422, detail="since_after_as_of")
        if as_of - since > MAX_NOTIFICATION_WINDOW:
            raise HTTPException(status_code=422, detail="since_window_too_long")
        return product_fixture.notifications(since)

    return router
