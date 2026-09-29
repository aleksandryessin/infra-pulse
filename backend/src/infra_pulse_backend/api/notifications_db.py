"""Notifications outside fixture mode (N1); called by the router ``api/notifications.py``.

New cards come from the forecast journal (``forecast_db``, B2); critical source records
from the observation scope (``storage/notifications_pg.py``). The window ends on the data
timeline: ``data_as_of`` bounds event time, and ``as_of`` is the data watermark or the
latest card publication (B2 publishes at the cutoff; its ``published_at`` of the run is
the wall clock and does not extend the window). Until B2 publishes, ``forecast_db`` raises
``ForecastNotImplemented`` and the router answers 503 — new cards cannot be counted, and a
zero would read as «нет новых».
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from infra_pulse_backend.api import forecast_db
from infra_pulse_backend.config import Settings
from infra_pulse_backend.ingestion.journal_csv import MSK
from infra_pulse_backend.operations.notifications import (
    NotificationWindowError,
    build_summary,
    check_window,
    new_cards,
    settle_on_cards,
    stand_clock,
)
from infra_pulse_backend.operations.reports import JOURNAL_PAGE_LIMIT, iter_journal
from infra_pulse_backend.storage import notifications_pg
from infra_pulse_core.contracts.forecast import HORIZON_HOURS, ForecastCard
from infra_pulse_core.contracts.notifications import (
    MAX_NOTIFICATION_ITEMS,
    MAX_NOTIFICATION_WINDOW,
    NotificationSummary,
)

# A card may be published up to its horizon after its cutoff (contract rule).
_LONGEST_HORIZON = timedelta(hours=max(HORIZON_HOURS.values()))


class ObservationScopeNotConfigured(LookupError):
    """No DSN or no replay/received scope in the settings (HTTP 503)."""


def observation_scope(config: Settings) -> tuple[str, str, str]:
    """(dsn, namespace, snapshot or stream) of the active replay/received scope."""
    if config.db_dsn is None:
        raise ObservationScopeNotConfigured("observations_not_configured")
    if config.mode == "replay" and config.replay_snapshot_id is not None:
        return config.db_dsn.get_secret_value(), config.replay_namespace, config.replay_snapshot_id
    if config.mode == "received" and config.received_stream_id is not None:
        return (
            config.db_dsn.get_secret_value(),
            config.received_namespace,
            config.received_stream_id,
        )
    raise ObservationScopeNotConfigured("observations_not_configured")


def journal_cards(
    config: Settings, *, issued_from, issued_to, as_of: datetime
) -> Iterator[ForecastCard]:
    """Cards of the forecast journal issued in ``[issued_from, issued_to]`` (MSK dates)."""

    def fetch(cursor: str | None):
        return forecast_db.forecast_journal(
            config,
            as_of=as_of,
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

    return (entry.card for entry in iter_journal(fetch))


def notification_summary(
    config: Settings, *, since: datetime, now: datetime | None = None
) -> NotificationSummary:
    """Summary of new cards and critical records in ``(since, as_of]`` of the stand.

    Raises ``forecast_db.ForecastNotImplemented`` (503), ``StandClockUnavailable`` (503),
    ``NotificationWindowError`` (422), ``ObservationScopeNotConfigured`` (503),
    ``LookupError`` for an unloaded scope (503) and ``psycopg.Error`` (503).
    """
    now = now or datetime.now(UTC)
    if since.tzinfo is None or since.utcoffset() is None:
        check_window(since, now)  # raises aware_since_required
    state = forecast_db.forecast_state(config)
    clock = stand_clock(config.mode, state, now=now)
    # The final as_of lies in [data_as_of, clock.as_of]: reject what no journal can fix
    # before reading it, then settle the window on the data timeline (settle_on_cards).
    if since > clock.as_of:
        raise NotificationWindowError("since_after_as_of")
    if clock.data_as_of - since > MAX_NOTIFICATION_WINDOW:
        raise NotificationWindowError("since_window_too_long")
    issued_from = (since - _LONGEST_HORIZON).astimezone(MSK).date()
    issued_to = clock.as_of.astimezone(MSK).date()
    journal = list(
        journal_cards(config, issued_from=issued_from, issued_to=issued_to, as_of=clock.as_of)
    )
    clock = settle_on_cards(clock, journal)
    check_window(since, clock.as_of)
    cards = new_cards(journal, since=since, as_of=clock.as_of)
    dsn, namespace, snapshot = observation_scope(config)
    read = notifications_pg.read_critical_alarms(
        dsn,
        namespace_id=namespace,
        snapshot_id=snapshot,
        mode=config.mode,
        since=since,
        data_as_of=clock.data_as_of,
        available_as_of=clock.available_as_of,
        limit=MAX_NOTIFICATION_ITEMS,
        extra_object_ids=[card.object_id for card in cards[:MAX_NOTIFICATION_ITEMS]],
    )
    return build_summary(
        mode=config.mode,
        since=since,
        clock=clock,
        checked_at=now,
        cards=cards,
        object_names=read.object_names,
        alarms=read.records,
        alarms_total=read.total,
        test_rows=read.test_rows,
    )
