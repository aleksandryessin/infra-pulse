"""Management reports (ТЗ §8, optional). **Owned by package R1.**

fixture: the synthetic ``MonthlyReport`` (C0.2) and its XLSX; the journal XLSX pages the
fixture journal. replay/received: ``reports_db`` — cards, outcomes and decisions from the
forecast journal (B2), source alarms from the observation scope; until B2 lands 503
``forecast_not_implemented``. scaffold answers 503 ``reports_not_implemented``. Every
answer is ``Cache-Control: no-store``; files carry ``Content-Disposition`` with a name.
PDF is printed from the browser page.
"""

from collections.abc import Callable, Iterator
from datetime import date
from typing import IO, Annotated, TypeVar

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from infra_pulse_backend.api import forecast_db, notifications_db, product_fixture, reports_db
from infra_pulse_backend.api.auth_deps import require_permission
from infra_pulse_backend.config import Settings
from infra_pulse_backend.operations.notifications import StandClockUnavailable
from infra_pulse_backend.operations.reports import (
    ReportRequestError,
    monthly_report_xlsx,
    xlsx_file_name,
)
from infra_pulse_core.contracts.reports import MonthlyReport

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
NO_STORE = {"Cache-Control": "no-store"}
_CHUNK = 64 * 1024
T = TypeVar("T")


def _stream(file: IO[bytes]) -> Iterator[bytes]:
    try:
        while chunk := file.read(_CHUNK):
            yield chunk
    finally:
        file.close()


def xlsx_response(file: IO[bytes], name: str) -> StreamingResponse:
    return StreamingResponse(
        _stream(file),
        media_type=XLSX,
        headers={**NO_STORE, "Content-Disposition": f'attachment; filename="{name}"'},
    )


def build_router(config: Settings) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_permission("report"))])
    fixture = config.mode == "fixture"

    def guarded(read: Callable[[], T]) -> T:
        if not fixture and config.mode not in ("replay", "received"):
            raise HTTPException(status_code=503, detail="reports_not_implemented")
        try:
            return read()
        except ReportRequestError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except forecast_db.ForecastNotImplemented as error:
            raise HTTPException(status_code=503, detail="forecast_not_implemented") from error
        except (StandClockUnavailable, notifications_db.ObservationScopeNotConfigured) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=503, detail="observation_scope_not_loaded") from error
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="reports_storage_unavailable") from error

    def monthly_report(month: str) -> MonthlyReport:
        if fixture:
            return product_fixture.monthly_report(month)
        return guarded(lambda: reports_db.monthly_report(config, month))

    @router.get("/api/v1/reports/monthly", response_model=MonthlyReport)
    def monthly(month: Month, response: Response) -> MonthlyReport:
        response.headers.update(NO_STORE)
        return monthly_report(month)

    @router.get(
        "/api/v1/reports/monthly.xlsx",
        response_class=Response,
        responses={200: {"content": {XLSX: {}}}},
    )
    def monthly_xlsx(month: Month) -> Response:
        report = monthly_report(month)
        return xlsx_response(monthly_report_xlsx(report), xlsx_file_name("monthly", month))

    @router.get(
        "/api/v1/reports/journal.xlsx",
        response_class=Response,
        responses={200: {"content": {XLSX: {}}}},
    )
    def journal_xlsx(issued_from: date, issued_to: date) -> Response:
        if issued_from > issued_to:
            raise HTTPException(status_code=422, detail="issued_range_reversed")
        export = guarded(
            lambda: reports_db.journal_export(config, issued_from=issued_from, issued_to=issued_to)
        )
        name = xlsx_file_name("journal", issued_from.isoformat(), issued_to.isoformat())
        return xlsx_response(export.file, name)

    return router
