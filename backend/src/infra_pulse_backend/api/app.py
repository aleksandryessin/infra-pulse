from datetime import UTC, datetime
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from psycopg.rows import dict_row

from infra_pulse_backend.api import (
    auth,
    decisions,
    forecast_db,
    forecasts,
    imports,
    notifications,
    observations,
    reports,
    research,
)
from infra_pulse_backend.api import docs as api_docs
from infra_pulse_backend.api.audit_log import AuditMiddleware, PgAuditSink
from infra_pulse_backend.api.auth_deps import require_permission
from infra_pulse_backend.config import Settings
from infra_pulse_backend.operations.attention import order_for_review, summarize_source_alarms
from infra_pulse_backend.storage.attention_pg import (
    WATCH_CLAUSE,
    WATCH_PARAMS,
    read_attention_page,
    read_channel_summaries,
    read_object_summaries,
    read_source_alarm_window,
)
from infra_pulse_backend.storage.coverage_pg import read_coverage_channels, read_coverage_objects
from infra_pulse_backend.storage.review_notes_pg import (
    IdempotencyConflict,
    RevisionConflict,
    create_review_note,
    read_review_journal,
    read_review_notes,
)
from infra_pulse_core.contracts.attention import (
    AttentionList,
    Capabilities,
    ChannelAttentionList,
    ObjectAttentionList,
    ObservedMessage,
    ReplayCoverageChannelList,
    ReplayCoverageObjectList,
    ReviewJournalList,
    ReviewNote,
    ReviewNoteCreate,
    ReviewNoteList,
    SourceAlarmWindow,
)
from infra_pulse_core.contracts.auth import Me
from infra_pulse_core.contracts.risk import Risk, RiskList

# B3: every data route checks a permission on the server (backend/tests/test_rbac_matrix.py).
READ = [Depends(require_permission("read"))]


def fixture() -> Risk:
    return Risk(
        id="fixture-risk-001",
        channel_id="synthetic-channel-001",
        as_of="2026-09-15T12:00:00Z",
        data_cutoff="2026-09-15T11:55:00Z",
        assessment_kind="condition_assessment",
        label_type="unlabeled_anomaly",
        status="abstained",
        risk_level="unknown",
        abstention_reason="Синтетический пример контракта. Модель ещё не подключена.",
        model_version="not-trained",
        feature_version="not-built",
        label_version="not-defined",
        policy_version="not-defined",
        source="synthetic_fixture",
    )


# Moment of the synthetic source messages (`/attention` and its window in fixture mode).
ATTENTION_FIXTURE_AS_OF = datetime(2026, 9, 24, 9, 2, tzinfo=UTC)


def attention_fixture_messages() -> list[ObservedMessage]:
    """Small synthetic set demonstrating distinct raw text and source alarm."""
    received = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
    common = {
        "source_namespace": "synthetic-fixture",
        "snapshot_id": "synthetic-fixture-v1",
        "availability_basis": "simulated",
        "reference_version": "synthetic-reference-v1",
    }
    return [
        ObservedMessage(
            **common,
            row_uid="synthetic-row-001",
            channel_id="synthetic-gas-001",
            channel_name="Синт. газоанализатор 1",
            object_id="synthetic-object-01",
            object_name="Синтетический объект 01",
            sensor_type="Газовый датчик",
            system_type="Газовый контроль",
            value_raw="Обнаружен газ",
            alarm=False,
            event_at=datetime(2026, 9, 24, 8, 55, tzinfo=UTC),
            available_at=received,
        ),
        ObservedMessage(
            **common,
            row_uid="synthetic-row-002",
            channel_id="synthetic-pump-001",
            channel_name="Синт. насос 1",
            object_id="synthetic-object-01",
            object_name="Синтетический объект 01",
            sensor_type="Насос",
            system_type="Водоотведение",
            value_raw="Неисправен",
            alarm=True,
            event_at=datetime(2026, 9, 24, 8, 56, tzinfo=UTC),
            available_at=received,
        ),
        ObservedMessage(
            **common,
            row_uid="synthetic-row-003",
            channel_id="synthetic-ups-001",
            channel_name="Синт. ИБП 1",
            object_id=None,
            sensor_type="ИБП",
            system_type="Электроснабжение",
            value_raw="Батарея неисправна",
            alarm=False,
            event_at=datetime(2026, 9, 24, 8, 58, tzinfo=UTC),
            available_at=datetime(2026, 9, 24, 9, 1, tzinfo=UTC),
        ),
    ]


def attention_fixture(
    offset: int = 0,
    limit: int = 100,
    object_id: str | None = None,
    channel_id: str | None = None,
    row_uid: str | None = None,
    view: Literal["attention", "all"] = "attention",
    local_note: Literal["any", "present", "absent"] = "any",
    alarm: bool | None = None,
    event_at: datetime | None = None,
    event_from: datetime | None = None,
) -> AttentionList:
    """`/attention` over the synthetic set: filters, then the provisional review order."""
    messages = attention_fixture_messages()
    if object_id == "__unknown__":
        messages = [message for message in messages if message.object_id is None]
    elif object_id is not None:
        messages = [message for message in messages if message.object_id == object_id]
    if channel_id is not None:
        messages = [message for message in messages if message.channel_id == channel_id]
    if row_uid is not None:
        messages = [message for message in messages if message.row_uid == row_uid]
    if alarm is not None:
        messages = [message for message in messages if message.alarm is alarm]
    if event_at is not None:
        messages = [message for message in messages if message.event_at == event_at]
    if event_from is not None:
        messages = [message for message in messages if message.event_at >= event_from]
    result = order_for_review(
        messages,
        as_of=ATTENTION_FIXTURE_AS_OF,
        mode="fixture",
        view=view,
        offset=offset,
        limit=limit,
    )
    if local_note == "present":
        return result.model_copy(
            update={
                "items": [],
                "total": 0,
                "source_alarm_count": 0,
                "watch_text_count": 0,
                "chronological_count": 0,
            }
        )
    return result


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()
    # B3: a directory-login (public) stand hides the API schema unless INFRA_PUBLIC_DOCS.
    # Swagger UI /api/docs and the schema /api/openapi.json (api/docs.py); no root /docs,
    # /redoc or /openapi.json, no ReDoc. Only the documentation is public.
    docs = config.auth_mode == "dev_stub" or config.public_docs
    app = FastAPI(
        title="InfraPulse MSK",
        version="0.1.0",
        description=api_docs.DESCRIPTION,
        docs_url=None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
        openapi_url=api_docs.OPENAPI_URL if docs else None,
    )
    api_docs.install(app, serve=docs)
    app.state.settings = config
    # G1: every identified request is audited (views, exports, rejected writes; polls
    # summed per window) — api/audit_log.py; nothing is written without a database.
    app.add_middleware(
        AuditMiddleware,
        sink=PgAuditSink(config.db_dsn.get_secret_value()).write if config.db_dsn else None,
        window_seconds=config.audit_poll_window_seconds,
        fold_seconds=config.audit_fold_seconds,
    )

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive", "mode": config.mode}

    def active_scope_config() -> tuple[str, str, str]:
        if config.db_dsn is None:
            raise HTTPException(status_code=503, detail="observations_not_configured")
        if config.mode == "replay" and config.replay_snapshot_id is not None:
            return (
                config.db_dsn.get_secret_value(),
                config.replay_namespace,
                config.replay_snapshot_id,
            )
        if config.mode == "received" and config.received_stream_id is not None:
            return (
                config.db_dsn.get_secret_value(),
                config.received_namespace,
                config.received_stream_id,
            )
        raise HTTPException(status_code=503, detail="observations_not_configured")

    def active_scope(after_received_watermark: int | None = None) -> dict | None:
        try:
            dsn, namespace, snapshot = active_scope_config()
            with psycopg.connect(dsn, row_factory=dict_row) as connection:
                if config.mode == "received":
                    if after_received_watermark is None:
                        return connection.execute(
                            """SELECT scope.window_start, scope.window_end,
                                      scope.row_count, scope.alarm_count,
                                      scope.last_received_at, scope.last_scanned_at,
                                      clock_timestamp() AS status_checked_at,
                                      NULL::bigint AS rows_after_watermark,
                                      NULL::bigint AS source_alarms_after_watermark,
                                      NULL::bigint AS candidates_after_watermark,
                                      NULL::bigint AS candidate_groups_after_watermark,
                                      (SELECT count(*)
                                       FROM dispatch_received_inbox_failures AS failure
                                       WHERE failure.namespace_id = scope.namespace_id
                                         AND failure.snapshot_id = scope.snapshot_id)
                                        AS inbox_failure_count,
                                      (SELECT max(detected_at)
                                       FROM dispatch_received_inbox_failures AS failure
                                       WHERE failure.namespace_id = scope.namespace_id
                                         AND failure.snapshot_id = scope.snapshot_id)
                                        AS inbox_last_failure_at
                               FROM dispatch_replay_snapshots AS scope
                               WHERE scope.namespace_id = %s AND scope.snapshot_id = %s
                                 AND scope.scope_kind = 'received'""",
                            (namespace, snapshot),
                        ).fetchone()
                    return connection.execute(
                        f"""SELECT scope.window_start, scope.window_end,
                                  scope.row_count, scope.alarm_count,
                                  scope.last_received_at, scope.last_scanned_at,
                                  clock_timestamp() AS status_checked_at,
                                  updates.rows_after_watermark,
                                  updates.source_alarms_after_watermark,
                                  updates.candidates_after_watermark,
                                  updates.candidate_groups_after_watermark,
                                  (SELECT count(*) FROM dispatch_received_inbox_failures AS failure
                                   WHERE failure.namespace_id = scope.namespace_id
                                     AND failure.snapshot_id = scope.snapshot_id)
                                    AS inbox_failure_count,
                                  (SELECT max(detected_at)
                                   FROM dispatch_received_inbox_failures AS failure
                                   WHERE failure.namespace_id = scope.namespace_id
                                     AND failure.snapshot_id = scope.snapshot_id)
                                    AS inbox_last_failure_at
                           FROM dispatch_replay_snapshots AS scope
                           LEFT JOIN LATERAL (
                             SELECT count(*) AS rows_after_watermark,
                                    count(*) FILTER (WHERE observation.alarm)
                                      AS source_alarms_after_watermark,
                                    count(*) FILTER (WHERE observation.alarm OR {WATCH_CLAUSE})
                                      AS candidates_after_watermark,
                                    count(DISTINCT ROW(observation.object_id,
                                                       observation.channel_id))
                                      FILTER (WHERE observation.alarm OR {WATCH_CLAUSE})
                                      AS candidate_groups_after_watermark
                             FROM dispatch_observations AS observation
                             WHERE observation.namespace_id = scope.namespace_id
                               AND observation.snapshot_id = scope.snapshot_id
                               AND observation.received_position > %s
                               AND observation.received_position <= scope.row_count
                           ) AS updates ON TRUE
                           WHERE scope.namespace_id = %s AND scope.snapshot_id = %s
                             AND scope.scope_kind = 'received'""",
                        (
                            *WATCH_PARAMS,
                            *WATCH_PARAMS,
                            after_received_watermark,
                            namespace,
                            snapshot,
                        ),
                    ).fetchone()
                return connection.execute(
                    """SELECT window_start, window_end, row_count, alarm_count,
                              last_received_at
                       FROM dispatch_replay_snapshots
                       WHERE namespace_id = %s AND snapshot_id = %s
                         AND scope_kind = 'replay'""",
                    (namespace, snapshot),
                ).fetchone()
        except (HTTPException, psycopg.Error):
            return None

    def stage(observations_ready: bool) -> str:
        """Actual stage of the stand (``Capabilities.stage``): the forecast is published by
        the worker (``operations.forecast_publish``) and only read here."""
        if config.mode in ("scaffold", "fixture"):
            return config.mode
        if not observations_ready:
            return "not_ready"
        published = forecast_db.published_generation(config) is not None
        return "forecast_published" if published else "observations_only"

    @app.get("/health/ready")
    def ready() -> dict[str, object]:
        if config.mode not in ("replay", "received") or active_scope() is None:
            raise HTTPException(status_code=503, detail="configured_capability_not_ready")
        current = stage(True)
        return {
            "status": "ready",
            "mode": config.mode,
            "stage": current,
            "observations_ready": True,
            "inference_ready": current == "forecast_published",
        }

    @app.get("/api/v1/capabilities", response_model=Capabilities, dependencies=READ)
    def capabilities(
        after_received_watermark: int | None = Query(default=None, ge=0),
    ) -> Capabilities:
        if after_received_watermark is not None and config.mode != "received":
            raise HTTPException(status_code=422, detail="received_watermark_requires_received_mode")
        snapshot = (
            active_scope(after_received_watermark)
            if config.mode in ("replay", "received")
            else None
        )
        if snapshot and after_received_watermark is not None:
            if after_received_watermark > snapshot["row_count"]:
                raise HTTPException(status_code=422, detail="received_watermark_out_of_range")
        current = stage(snapshot is not None)
        return Capabilities(
            stage=current,
            mode=config.mode,
            inference_ready=current == "forecast_published",
            persistence_ready=snapshot is not None,
            authentication_ready=config.auth_mode == "ldap",
            fixture_available=config.mode == "fixture",
            observations_ready=snapshot is not None,
            local_reviews_enabled=config.mode in ("replay", "received")
            and snapshot is not None
            and config.enable_local_reviews,
            replay_window_start=snapshot["window_start"]
            if snapshot and config.mode == "replay"
            else None,
            replay_window_end=snapshot["window_end"]
            if snapshot and config.mode == "replay"
            else None,
            replay_rows=snapshot["row_count"] if snapshot and config.mode == "replay" else None,
            received_last_at=snapshot["last_received_at"]
            if snapshot and config.mode == "received"
            else None,
            received_rows=snapshot["row_count"] if snapshot and config.mode == "received" else None,
            received_after_watermark=after_received_watermark
            if snapshot and config.mode == "received"
            else None,
            received_rows_after_watermark=snapshot["rows_after_watermark"]
            if snapshot and config.mode == "received" and after_received_watermark is not None
            else None,
            received_source_alarms_after_watermark=snapshot["source_alarms_after_watermark"]
            if snapshot and config.mode == "received" and after_received_watermark is not None
            else None,
            received_candidates_after_watermark=snapshot["candidates_after_watermark"]
            if snapshot and config.mode == "received" and after_received_watermark is not None
            else None,
            received_candidate_groups_after_watermark=snapshot["candidate_groups_after_watermark"]
            if snapshot and config.mode == "received" and after_received_watermark is not None
            else None,
            received_status_checked_at=snapshot["status_checked_at"]
            if snapshot and config.mode == "received"
            else None,
            received_inbox_last_scan_at=snapshot["last_scanned_at"]
            if snapshot and config.mode == "received"
            else None,
            received_inbox_failure_count=snapshot["inbox_failure_count"]
            if snapshot and config.mode == "received"
            else None,
            received_inbox_last_failure_at=snapshot["inbox_last_failure_at"]
            if snapshot and config.mode == "received"
            else None,
        )

    # The starter per-channel Risk contract: kept for the synthetic fixture and its tests,
    # outside the public schema. The product forecast is /api/v1/forecasts (object cards
    # published by the worker); other modes answer 410 with that pointer.
    @app.get("/api/v1/risks", response_model=RiskList, dependencies=READ, include_in_schema=False)
    def risks() -> RiskList:
        if config.mode != "fixture":
            raise HTTPException(status_code=410, detail="risks_replaced_by_forecasts")
        return RiskList(items=[fixture()], total=1)

    # Forecast, decisions, imports, auth and research routes live in their own routers
    # (C0, 27.09.2026). Each package fills its router or data module, not this file.
    app.include_router(auth.build_router(config))
    app.include_router(forecasts.build_router(config))
    app.include_router(decisions.build_router(config))
    app.include_router(imports.build_router(config))
    app.include_router(research.build_router(config))
    # C0.2: streaming ingest (B1x), notifications (N1), management reports (R1).
    app.include_router(observations.build_router(config))
    app.include_router(notifications.build_router(config))
    app.include_router(reports.build_router(config))

    @app.get("/api/v1/attention", response_model=AttentionList, dependencies=READ)
    def attention(
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=100),
        as_of: datetime | None = None,
        object_id: str | None = None,
        channel_id: str | None = Query(default=None, min_length=1, max_length=256),
        row_uid: str | None = Query(default=None, min_length=1, max_length=256),
        view: Literal["attention", "all"] = "attention",
        local_note: Literal["any", "present", "absent"] = "any",
        alarm: bool | None = None,
        received_watermark: int | None = Query(default=None, ge=0),
        after_received_watermark: int | None = Query(default=None, ge=0),
        event_at: datetime | None = None,
        event_from: datetime | None = None,
    ) -> AttentionList:
        if received_watermark is not None and config.mode != "received":
            raise HTTPException(status_code=422, detail="received_watermark_requires_received_mode")
        if after_received_watermark is not None and config.mode != "received":
            raise HTTPException(status_code=422, detail="received_watermark_requires_received_mode")
        if event_at is not None and (event_at.tzinfo is None or event_at.utcoffset() is None):
            raise HTTPException(status_code=422, detail="aware_event_at_required")
        if event_from is not None and (event_from.tzinfo is None or event_from.utcoffset() is None):
            raise HTTPException(status_code=422, detail="aware_event_from_required")
        if config.mode == "fixture":
            return attention_fixture(
                offset=offset,
                limit=limit,
                object_id=object_id,
                channel_id=channel_id,
                row_uid=row_uid,
                view=view,
                local_note=local_note,
                alarm=alarm,
                event_at=event_at,
                event_from=event_from,
            )
        if config.mode not in ("replay", "received"):
            raise HTTPException(status_code=503, detail="observations_not_integrated")
        if config.mode == "received" and as_of is None:
            as_of = datetime.now(UTC)
        if as_of is None or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise HTTPException(status_code=422, detail="aware_as_of_required")
        dsn, namespace, snapshot = active_scope_config()
        try:
            return read_attention_page(
                dsn,
                namespace_id=namespace,
                snapshot_id=snapshot,
                as_of=as_of,
                mode=config.mode,
                offset=offset,
                limit=limit,
                object_id=object_id,
                channel_id=channel_id,
                row_uid=row_uid,
                view=view,
                local_note=local_note,
                alarm=alarm,
                received_watermark=received_watermark,
                after_received_watermark=after_received_watermark,
                event_at=event_at,
                event_from=event_from,
            )
        except LookupError as error:
            raise HTTPException(status_code=503, detail="observation_scope_not_loaded") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="replay_storage_unavailable") from error

    @app.get("/api/v1/attention/source-alarms", response_model=SourceAlarmWindow, dependencies=READ)
    def source_alarm_window(
        event_from: datetime,
        as_of: datetime | None = None,
        received_watermark: int | None = Query(default=None, ge=0),
        limit: int = Query(default=10, ge=1, le=50),
        object_limit: int = Query(default=100, ge=1, le=500),
    ) -> SourceAlarmWindow:
        """«Сейчас» (F-04): source alarms (``alarm=true``) with event time from
        ``event_from`` — counts over the whole window, objects and the latest ``limit``
        records by event time, newest first. The source flag is not a confirmed failure."""
        if event_from.tzinfo is None or event_from.utcoffset() is None:
            raise HTTPException(status_code=422, detail="aware_event_from_required")
        if received_watermark is not None and config.mode != "received":
            raise HTTPException(status_code=422, detail="received_watermark_requires_received_mode")
        if config.mode == "fixture":
            return summarize_source_alarms(
                attention_fixture_messages(),
                as_of=ATTENTION_FIXTURE_AS_OF,
                event_from=event_from,
                mode="fixture",
                latest_limit=limit,
                object_limit=object_limit,
            )
        if config.mode not in ("replay", "received"):
            raise HTTPException(status_code=503, detail="observations_not_integrated")
        if config.mode == "received" and as_of is None:
            as_of = datetime.now(UTC)
        if as_of is None or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise HTTPException(status_code=422, detail="aware_as_of_required")
        dsn, namespace, snapshot = active_scope_config()
        try:
            return read_source_alarm_window(
                dsn,
                namespace_id=namespace,
                snapshot_id=snapshot,
                as_of=as_of,
                event_from=event_from,
                mode=config.mode,
                received_watermark=received_watermark,
                latest_limit=limit,
                object_limit=object_limit,
            )
        except LookupError as error:
            raise HTTPException(status_code=503, detail="observation_scope_not_loaded") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="replay_storage_unavailable") from error

    @app.get("/api/v1/attention/objects", response_model=ObjectAttentionList, dependencies=READ)
    def attention_objects(
        candidate_kind: Literal["all", "alarm", "text", "candidate"] = "all",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=25, ge=1, le=100),
        as_of: datetime | None = None,
        received_watermark: int | None = Query(default=None, ge=0),
    ) -> ObjectAttentionList:
        if config.mode not in ("replay", "received"):
            raise HTTPException(status_code=503, detail="observations_not_integrated")
        if received_watermark is not None and config.mode != "received":
            raise HTTPException(status_code=422, detail="received_watermark_requires_received_mode")
        if config.mode == "received" and as_of is None:
            as_of = datetime.now(UTC)
        if as_of is None or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise HTTPException(status_code=422, detail="aware_as_of_required")
        dsn, namespace, snapshot = active_scope_config()
        try:
            return read_object_summaries(
                dsn,
                namespace_id=namespace,
                snapshot_id=snapshot,
                as_of=as_of,
                mode=config.mode,
                received_watermark=received_watermark,
                candidate_kind=candidate_kind,
                offset=offset,
                limit=limit,
            )
        except LookupError as error:
            raise HTTPException(status_code=503, detail="observation_scope_not_loaded") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="replay_storage_unavailable") from error

    @app.get("/api/v1/attention/channels", response_model=ChannelAttentionList, dependencies=READ)
    def attention_channels(
        object_id: str = Query(min_length=1, max_length=256),
        channel_id: str | None = Query(default=None, min_length=1, max_length=256),
        candidate_kind: Literal["all", "alarm", "text", "candidate"] = "all",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=25, ge=1, le=100),
        as_of: datetime | None = None,
        received_watermark: int | None = Query(default=None, ge=0),
    ) -> ChannelAttentionList:
        if config.mode not in ("replay", "received"):
            raise HTTPException(status_code=503, detail="observations_not_integrated")
        if received_watermark is not None and config.mode != "received":
            raise HTTPException(status_code=422, detail="received_watermark_requires_received_mode")
        if config.mode == "received" and as_of is None:
            as_of = datetime.now(UTC)
        if as_of is None or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise HTTPException(status_code=422, detail="aware_as_of_required")
        dsn, namespace, snapshot = active_scope_config()
        try:
            return read_channel_summaries(
                dsn,
                namespace_id=namespace,
                snapshot_id=snapshot,
                as_of=as_of,
                object_id=object_id,
                channel_id=channel_id,
                candidate_kind=candidate_kind,
                mode=config.mode,
                received_watermark=received_watermark,
                offset=offset,
                limit=limit,
            )
        except LookupError as error:
            raise HTTPException(status_code=503, detail="observation_scope_not_loaded") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except psycopg.Error as error:
            raise HTTPException(
                status_code=503, detail="observation_storage_unavailable"
            ) from error

    @app.get(
        "/api/v1/attention/coverage/objects",
        response_model=ReplayCoverageObjectList,
        dependencies=READ,
    )
    def coverage_objects(as_of: datetime) -> ReplayCoverageObjectList:
        if config.mode != "replay":
            raise HTTPException(status_code=503, detail="reference_coverage_replay_only")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise HTTPException(status_code=422, detail="aware_as_of_required")
        dsn, namespace, snapshot = active_scope_config()
        try:
            return read_coverage_objects(
                dsn, namespace_id=namespace, snapshot_id=snapshot, as_of=as_of
            )
        except LookupError as error:
            raise HTTPException(status_code=503, detail="reference_roster_not_loaded") from error
        except psycopg.Error as error:
            raise HTTPException(
                status_code=503, detail="observation_storage_unavailable"
            ) from error

    @app.get(
        "/api/v1/attention/coverage/channels",
        response_model=ReplayCoverageChannelList,
        dependencies=READ,
    )
    def coverage_channels(
        as_of: datetime,
        object_id: str = Query(min_length=1, max_length=256),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> ReplayCoverageChannelList:
        if config.mode != "replay":
            raise HTTPException(status_code=503, detail="reference_coverage_replay_only")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise HTTPException(status_code=422, detail="aware_as_of_required")
        dsn, namespace, snapshot = active_scope_config()
        try:
            return read_coverage_channels(
                dsn,
                namespace_id=namespace,
                snapshot_id=snapshot,
                as_of=as_of,
                object_id=object_id,
                offset=offset,
                limit=limit,
            )
        except LookupError as error:
            raise HTTPException(status_code=503, detail="reference_roster_not_loaded") from error
        except psycopg.Error as error:
            raise HTTPException(
                status_code=503, detail="observation_storage_unavailable"
            ) from error

    def review_config() -> tuple[str, str, str]:
        if config.mode not in ("replay", "received") or not config.enable_local_reviews:
            raise HTTPException(status_code=503, detail="local_reviews_disabled")
        return active_scope_config()

    @app.get("/api/v1/review-journal", response_model=ReviewJournalList, dependencies=READ)
    def review_journal(
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=25, ge=1, le=100),
        row_uid: str | None = Query(default=None, min_length=1, max_length=256),
        object_id: str | None = Query(default=None, min_length=1, max_length=256),
        channel_id: str | None = Query(default=None, min_length=1, max_length=256),
        review_watermark: int | None = Query(default=None, ge=0),
    ) -> ReviewJournalList:
        dsn, namespace, snapshot = review_config()
        try:
            return read_review_journal(
                dsn,
                namespace_id=namespace,
                snapshot_id=snapshot,
                offset=offset,
                limit=limit,
                row_uid=row_uid,
                object_id=object_id,
                channel_id=channel_id,
                review_watermark=review_watermark,
                mode=config.mode,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="replay_storage_unavailable") from error

    @app.get(
        "/api/v1/attention/{row_uid}/reviews", response_model=ReviewNoteList, dependencies=READ
    )
    def reviews(row_uid: str) -> ReviewNoteList:
        dsn, namespace, snapshot = review_config()
        try:
            return read_review_notes(
                dsn,
                namespace_id=namespace,
                snapshot_id=snapshot,
                row_uid=row_uid,
                mode=config.mode,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail="observation_not_found") from error
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="replay_storage_unavailable") from error

    @app.post("/api/v1/attention/{row_uid}/reviews", response_model=ReviewNote, status_code=201)
    def add_review(
        row_uid: str,
        payload: ReviewNoteCreate,
        actor: Annotated[Me, Depends(require_permission("decide"))],
    ) -> ReviewNote:
        dsn, namespace, snapshot = review_config()
        try:
            return create_review_note(
                dsn,
                namespace_id=namespace,
                snapshot_id=snapshot,
                row_uid=row_uid,
                payload=payload,
                mode=config.mode,
                actor_id=actor.subject_id,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail="observation_not_found") from error
        except (RevisionConflict, IdempotencyConflict) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="replay_storage_unavailable") from error

    return app


app = create_app()
