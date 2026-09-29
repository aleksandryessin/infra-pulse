"""Management reports outside fixture mode and XLSX in every mode (R1); called by the
router ``api/reports.py``.

replay/received: cards, outcomes and decisions from the forecast journal
(``forecast_db``, B2) up to the stand clock of the published state; source alarms and
object names from the observation scope (``storage/reports_pg.py``). Until B2 publishes,
``forecast_db`` raises ``ForecastNotImplemented`` (503). fixture: the monthly XLSX renders
the same ``MonthlyReport`` as the JSON route, the journal XLSX pages the fixture journal.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

from infra_pulse_backend.api import forecast_db, forecast_fixture
from infra_pulse_backend.api.notifications_db import observation_scope
from infra_pulse_backend.config import Settings
from infra_pulse_backend.operations.notifications import stand_clock
from infra_pulse_backend.operations.reports import (
    JOURNAL_PAGE_LIMIT,
    JournalExport,
    build_monthly_report,
    check_journal_period,
    iter_journal,
    journal_issue_bounds,
    journal_xlsx,
    month_window,
    parse_month,
)
from infra_pulse_backend.storage import reports_pg
from infra_pulse_core.contracts.forecast import ForecastJournalEntry
from infra_pulse_core.contracts.reports import MonthlyReport


def _db_journal(
    config: Settings, *, issued_from: date, issued_to: date, as_of: datetime, now: datetime
) -> Iterator[ForecastJournalEntry]:
    """Journal entries at the stand clock ``as_of``; decisions and check results saved by
    ``now`` (the report's ``generated_at``), even after the data (historical scope)."""

    def fetch(cursor: str | None):
        return forecast_db.forecast_journal(
            config,
            as_of=as_of,
            records_as_of=now,
            outcome=None,
            horizon=None,
            target_spec_id=None,
            object_id=None,
            list_state=None,
            issued_from=issued_from,
            issued_to=issued_to,
            cursor=cursor,
            limit=JOURNAL_PAGE_LIMIT,
        )

    return iter_journal(fetch)


def monthly_report(config: Settings, month: str, *, now: datetime | None = None) -> MonthlyReport:
    """replay/received monthly summary. Raises ``ReportRequestError`` (422),
    ``ForecastNotImplemented``/``StandClockUnavailable``/``ObservationScopeNotConfigured``/
    ``LookupError``/``psycopg.Error`` (503)."""
    now = now or datetime.now(UTC)
    start, end = parse_month(month)
    state = forecast_db.forecast_state(config)
    clock = stand_clock(config.mode, state, now=now)
    issued_from, issued_to = journal_issue_bounds(start, end)
    entries = list(
        _db_journal(
            config,
            issued_from=issued_from,
            issued_to=issued_to,
            as_of=clock.as_of,
            now=now,
        )
    )
    dsn, namespace, snapshot = observation_scope(config)
    window_start, window_end = month_window(month)
    alarms = reports_pg.read_source_alarm_counts(
        dsn,
        namespace_id=namespace,
        snapshot_id=snapshot,
        mode=config.mode,
        start=window_start,
        end=window_end,
        data_as_of=clock.data_as_of,
        available_as_of=clock.available_as_of,
    )
    report = build_monthly_report(
        mode=config.mode,
        month=month,
        generated_at=now,
        data_as_of=clock.data_as_of,
        entries=entries,
        targets=[horizon.target_spec_id for horizon in state.horizons],
        alarms=alarms,
    )
    names = reports_pg.read_names(dsn, [row.object_id for row in report.top_objects])
    return report.model_copy(
        update={
            "top_objects": [
                row.model_copy(update={"object_name": names.get(row.object_id)})
                for row in report.top_objects
            ]
        }
    )


def journal_export(
    config: Settings, *, issued_from: date, issued_to: date, now: datetime | None = None
) -> JournalExport:
    """Journal XLSX of any mode (fixture pages the synthetic journal)."""
    now = now or datetime.now(UTC)
    check_journal_period(issued_from, issued_to)
    if config.mode == "fixture":
        state = forecast_fixture.forecast_state()

        def fetch(cursor: str | None):
            return forecast_fixture.forecast_journal(
                issued_from=issued_from,
                issued_to=issued_to,
                cursor=cursor,
                limit=JOURNAL_PAGE_LIMIT,
            )

        return journal_xlsx(
            iter_journal(fetch),
            mode="fixture",
            issued_from=issued_from,
            issued_to=issued_to,
            generated_at=now,
            data_as_of=state.data_as_of,
            object_names=_fixture_names,
        )
    state = forecast_db.forecast_state(config)
    clock = stand_clock(config.mode, state, now=now)
    dsn, _, _ = observation_scope(config)
    return journal_xlsx(
        _db_journal(
            config, issued_from=issued_from, issued_to=issued_to, as_of=clock.as_of, now=now
        ),
        mode=config.mode,
        issued_from=issued_from,
        issued_to=issued_to,
        generated_at=now,
        data_as_of=clock.data_as_of,
        object_names=lambda object_ids: reports_pg.read_names(dsn, object_ids),
    )


def _fixture_names(object_ids: list[str]) -> dict[str, str]:
    return {
        object_id: f"Синтетический объект {object_id.rsplit('-', 1)[1]}"
        for object_id in object_ids
        if object_id.startswith("synthetic-object-")
    }
