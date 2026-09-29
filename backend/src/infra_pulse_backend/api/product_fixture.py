"""Synthetic imports, research summary and identity for ``INFRA_MODE=fixture``.

Invented file names, hashes, counts and research numbers: they show the contract
shape for the frontend and are not metrics, uploads or real accounts.
"""

from datetime import date, datetime, timedelta, timezone

from infra_pulse_core.contracts.imports import (
    ImportFile,
    ImportList,
    ImportStageTiming,
    QuarantineSample,
)
from infra_pulse_core.contracts.research import (
    ResearchBlock,
    ResearchColumn,
    ResearchLadderRow,
    ResearchMetric,
    ResearchScope,
    ResearchSummary,
)

MSK = timezone(timedelta(hours=3))
_UPLOADED = datetime(2026, 9, 23, 23, 50, tzinfo=MSK)


def _sha(seed: str) -> str:
    return (seed * 64)[:64]


def imports() -> list[ImportFile]:
    """Newest first: published journal, duplicate, failed file, reference upload."""
    return [
        ImportFile(
            id="synthetic-import-001",
            format="journal_csv",
            file_name="synthetic-journal-2026-09-23.csv",
            sha256=_sha("a1"),
            size_bytes=9_700_000,
            uploaded_by="synthetic-admin-01",
            uploaded_at=_UPLOADED,
            status="published",
            finished_at=_UPLOADED + timedelta(seconds=11),
            rows_total=170_000,
            rows_accepted=169_870,
            rows_duplicate=120,
            rows_quarantined=10,
            unknown_channels=3,
            event_from=datetime(2026, 9, 23, 0, 0, 1, tzinfo=MSK),
            event_to=datetime(2026, 9, 23, 23, 59, 58, tzinfo=MSK),
            reference_version="synthetic-reference-v1",
            forecast_generation=9,
            timings=[
                ImportStageTiming(stage="store", seconds=0.4),
                ImportStageTiming(stage="parse", seconds=2.1),
                ImportStageTiming(stage="load", seconds=3.8),
                ImportStageTiming(stage="detect", seconds=1.2),
                ImportStageTiming(stage="score", seconds=0.1),
                ImportStageTiming(stage="publish", seconds=0.3),
            ],
            quarantine_reasons={"bad_date": 6, "bad_bool": 4},
            quarantine_sample=[
                QuarantineSample(
                    line_no=1042, reason="bad_date", raw_excerpt="synthetic,…,2026-13-01"
                ),
                QuarantineSample(
                    line_no=88_311, reason="bad_bool", raw_excerpt="synthetic,…,maybe"
                ),
            ],
            new_card_ids=[
                "synthetic-feeders-14d-019",
                "synthetic-feeders-14d-020",
            ],
            released_card_ids=["synthetic-feeders-14d-021"],
            simulated=True,
        ),
        ImportFile(
            id="synthetic-import-002",
            format="journal_csv",
            file_name="synthetic-journal-2026-09-23-copy.csv",
            sha256=_sha("a1"),
            size_bytes=9_700_000,
            uploaded_by="synthetic-admin-01",
            uploaded_at=_UPLOADED + timedelta(minutes=2),
            status="duplicate",
            finished_at=_UPLOADED + timedelta(minutes=2, seconds=1),
            rows_total=0,
            rows_accepted=0,
            rows_duplicate=0,
            rows_quarantined=0,
            duplicate_of="synthetic-import-001",
            simulated=True,
        ),
        ImportFile(
            id="synthetic-import-003",
            format="journal_csv",
            file_name="synthetic-not-a-journal.csv",
            sha256=_sha("b2"),
            size_bytes=2_048,
            uploaded_by="synthetic-admin-01",
            uploaded_at=_UPLOADED - timedelta(hours=1),
            status="failed",
            finished_at=_UPLOADED - timedelta(hours=1) + timedelta(seconds=1),
            error_code="bad_header",
            simulated=True,
        ),
        ImportFile(
            id="synthetic-import-004",
            format="reference_channels_csv",
            file_name="synthetic-channels.csv",
            sha256=_sha("c3"),
            size_bytes=1_300_000,
            uploaded_by="synthetic-admin-01",
            uploaded_at=_UPLOADED - timedelta(days=1),
            status="published",
            finished_at=_UPLOADED - timedelta(days=1) + timedelta(seconds=2),
            rows_total=11_500,
            rows_accepted=11_500,
            rows_duplicate=0,
            rows_quarantined=0,
            reference_version="synthetic-reference-v1",
            simulated=True,
        ),
    ]


def import_list(limit: int = 25) -> ImportList:
    items = imports()
    return ImportList(items=items[:limit], total=len(items), limit=limit)


def _metrics(precision: float, recall: float, low: float, high: float) -> list[ResearchMetric]:
    return [
        ResearchMetric(name="precision", value=precision, ci_low=low, ci_high=min(high, 1.0)),
        ResearchMetric(name="recall", value=recall),
    ]


def research_summary() -> ResearchSummary:
    """Synthetic shape of the «Исследование» page; the real file comes from data-science."""
    return ResearchSummary(
        version="synthetic-research-summary-v0",
        synthetic=True,
        scopes=[
            ResearchScope(
                scope_id="phase_feeders",
                title="Обесточивание электрооборудования объекта",
                sensor_types=["Состояние фазы"],
                status="in_product",
                horizon_days=14,
                budget_k=10,
                policy="скользящий список с освобождением",
                evaluation_period="синтетика",
                period_use_count=1,
                ladder=[
                    ResearchLadderRow(
                        step="static_list",
                        label="статичный список",
                        metrics=_metrics(0.7, 0.6, 0.65, 0.75),
                    ),
                    ResearchLadderRow(
                        step="persistence",
                        label="persistence",
                        metrics=_metrics(0.68, 0.5, 0.6, 0.75),
                    ),
                    ResearchLadderRow(
                        step="random_list",
                        label="случайный список",
                        metrics=_metrics(0.5, 0.4, 0.45, 0.55),
                    ),
                ],
            ),
            ResearchScope(
                scope_id="s4_sensors",
                title="Датчики ТЗ",
                sensor_types=["Датчик дыма", "Тепловой датчик", "Газовый датчик"],
                status="research_only",
                horizon_days=14,
                evaluation_period="синтетика",
                ladder=[
                    ResearchLadderRow(
                        step="static_list",
                        label="статичный список",
                        metrics=_metrics(0.5, 0.4, 0.4, 0.6),
                    ),
                ],
            ),
            ResearchScope(
                scope_id="gas_layer",
                title="Газовые датчики",
                sensor_types=["Газовый датчик"],
                status="needs_more_data",
                evaluation_period="синтетика",
                events_per_year=20,
                note="Событий слишком мало для оценки.",
            ),
        ],
        blocks=[
            ResearchBlock(
                block_id="synthetic_registry",
                title="Синтетический реестр: каналы для проверки при ТО",
                caption="Пример формы блока; состав реестров утверждает технолог.",
                status="research_only",
                columns=[
                    ResearchColumn(key="object", label="Объект"),
                    ResearchColumn(key="sensor_type", label="Тип датчика"),
                    ResearchColumn(key="events_90d", label="Срабатываний за 90 сут"),
                ],
                rows=[
                    {
                        "object": "synthetic-object-21",
                        "sensor_type": "Датчик дыма",
                        "events_90d": 14,
                    },
                    {
                        "object": "synthetic-object-22",
                        "sensor_type": "Тепловой датчик",
                        "events_90d": 9,
                    },
                ],
            )
        ],
        caveats=["Синтетические числа для проверки интерфейса, не результат исследования."],
    )


def product_fixture_document() -> dict:
    """Checked-in ``contracts/product.fixture.json``: imports, research and dev identity."""
    from infra_pulse_backend.api.auth_deps import LOCAL_OPERATOR

    return {
        "imports": import_list(limit=100).model_dump(mode="json"),
        "research_summary": research_summary().model_dump(mode="json"),
        "me_dev_stub": LOCAL_OPERATOR.model_dump(mode="json"),
        "observation_batch": observation_batch("synthetic-batch-001").model_dump(mode="json"),
        "notifications": notifications(NOTIFICATIONS_EXAMPLE_SINCE).model_dump(mode="json"),
        "monthly_report": monthly_report("2026-09").model_dump(mode="json"),
    }


# --- C0.2: streaming batch, notifications, monthly report (synthetic) -------------------


def observation_batch(import_id: str) -> ImportFile | None:
    """A synthetic streaming batch already published (``journal_json``)."""
    if import_id != "synthetic-batch-001":
        return None
    received = datetime(2026, 9, 24, 9, 55, tzinfo=MSK)
    return ImportFile(
        id="synthetic-batch-001",
        format="journal_json",
        file_name="synthetic-batch-001",
        sha256=_sha("d4"),
        size_bytes=48_000,
        uploaded_by="integration:synthetic-scada",
        uploaded_at=received,
        status="published",
        finished_at=received + timedelta(seconds=6),
        rows_total=400,
        rows_accepted=398,
        rows_duplicate=2,
        rows_quarantined=0,
        unknown_channels=0,
        event_from=received - timedelta(minutes=1),
        event_to=received - timedelta(seconds=5),
        reference_version="synthetic-reference-v1",
        forecast_generation=9,
        timings=[
            ImportStageTiming(stage="store", seconds=0.02),
            ImportStageTiming(stage="parse", seconds=0.05),
            ImportStageTiming(stage="load", seconds=0.2),
            ImportStageTiming(stage="detect", seconds=0.1),
            ImportStageTiming(stage="score", seconds=0.05),
            ImportStageTiming(stage="publish", seconds=0.1),
        ],
        simulated=True,
    )


NOTIFICATION_POLICY = "synthetic-critical-alarm-policy-v0"
NOTIFICATIONS_EXAMPLE_SINCE = datetime(2026, 9, 24, 0, 0, tzinfo=MSK)


def _alarm_row(
    ref: str,
    object_no: int,
    title: str,
    rule_id: str,
    rule_text: str,
    times: list[datetime],
    *,
    row_kind: str = "single",
    channels: int = 1,
    value: str,
    priority: int = 1,
):
    from infra_pulse_core.contracts.notifications import NotificationItem, NotificationMember

    members = [
        NotificationMember(
            row_uid=f"{ref}-{index}" if len(times) > 1 else ref,
            channel_id=f"synthetic-channel-{object_no}{index % channels:02d}",
            channel_name=f"Синт. датчик {object_no}{index % channels:02d}",
            event_at=moment,
            value_raw=value,
        )
        for index, moment in enumerate(times, start=1)
    ]
    return NotificationItem(
        kind="critical_alarm",
        ref_id=members[-1].row_uid,
        object_id=f"synthetic-object-{object_no}",
        object_name=f"Синтетический объект {object_no}",
        title=title,
        at=times[-1],
        rule_id=rule_id,
        rule_text=rule_text,
        priority=priority,
        row_kind=row_kind,
        collapsed_count=len(times),
        channels_count=channels,
        first_at=times[0],
        members=members,
    )


def _notification_items():
    """Synthetic bell rows of policy critical-alarms-v1 (C0.5): a single gas record first,
    a folded detector test series, a single smoke record and the new cards."""
    from infra_pulse_backend.api import forecast_fixture as ff
    from infra_pulse_core.contracts.notifications import NotificationItem

    items = [
        NotificationItem(
            kind="new_card",
            ref_id=card.id,
            object_id=card.object_id,
            object_name=f"Синтетический объект {card.object_id.rsplit('-', 1)[1]}",
            title=f"Новая карточка: {card.target_label}",
            at=card.published_at,
            target_spec_id=card.target_spec_id,
        )
        for card in ff.current_cards()
    ]
    day = datetime(2026, 9, 24, tzinfo=MSK)
    items += [
        _alarm_row(
            "synthetic-row-1502",
            15,
            "Газовый датчик: «Обнаружен газ»",
            "gas_detected",
            "газовый датчик: «Обнаружен газ», независимо от отметки «тревожное», "
            "вне серии проверки",
            [day + timedelta(hours=2, minutes=10)],
            value="Обнаружен газ",
            priority=0,
        ),
        _alarm_row(
            "synthetic-row-1207",
            12,
            "Датчик дыма: «Обнаружен дым»",
            "fire_smoke",
            "дымовой извещатель: «Обнаружен дым», независимо от отметки «тревожное»",
            [day + timedelta(hours=7, minutes=15)],
            value="Обнаружен дым",
        ),
        _alarm_row(
            "synthetic-row-1690",
            16,
            "Похоже на ППР или ТО: 6 извещателей — сверить с графиком",
            "fire_test_series",
            "серия дыма, тепловых и ручных извещателей объекта: 6 записей, 09:00–09:06 МСК, "
            "будни 07–19; раскрывается в список записей",
            [day + timedelta(hours=9, minutes=minute) for minute in range(6)],
            row_kind="test_series",
            channels=6,
            value="Обнаружен дым",
        ),
    ]
    return sorted(items, key=lambda item: (item.priority, -item.at.timestamp(), item.ref_id))


def notifications(since: datetime):
    """New cards and critical alarm rows after ``since`` up to the fixture as_of."""
    from infra_pulse_backend.api import forecast_fixture as ff
    from infra_pulse_backend.operations.notifications import NOTIFICATION_CAPTION
    from infra_pulse_core.contracts.notifications import (
        MAX_NOTIFICATION_ITEMS,
        NotificationSummary,
    )

    selected = [item for item in _notification_items() if since < item.at <= ff.FIXTURE_AS_OF]
    cards = sum(item.kind == "new_card" for item in selected)
    return NotificationSummary(
        mode="fixture",
        since=since,
        as_of=ff.FIXTURE_AS_OF,
        checked_at=ff.FIXTURE_AS_OF,
        new_cards=cards,
        critical_alarms=len(selected) - cards,
        items=selected[:MAX_NOTIFICATION_ITEMS],
        truncated=len(selected) > MAX_NOTIFICATION_ITEMS,
        policy_version=NOTIFICATION_POLICY,
        policy_confirmed=False,
        maintenance_check="not_applied",
        # The same caption as the runtime policy, marked as synthetic (C0.5).
        policy_caption=f"Синтетика. {NOTIFICATION_CAPTION}",
    )


def monthly_report(month: str):
    """Synthetic monthly summary built from the served fixture journal."""
    from infra_pulse_backend.api import forecast_fixture as ff
    from infra_pulse_core.contracts.reports import (
        MonthlyReport,
        ReportAlarmTypeRow,
        ReportCardCounts,
        ReportDecisionCount,
        ReportObjectRow,
    )

    year, number = (int(part) for part in month.split("-"))
    start = date(year, number, 1)
    end = date(year + number // 12, number % 12 + 1, 1)
    entries = [
        entry
        for entry in ff.journal_entries()
        if entry.card.status == "scored"
        and start <= entry.card.issued_at.astimezone(MSK).date() < end
    ]
    states = [entry.list_state for entry in entries]
    statuses = [entry.outcome.status for entry in entries]
    decisions: dict[str, int] = {}
    for entry in entries:
        if entry.decision is not None:
            code = entry.decision.decision_code
            decisions[code] = decisions.get(code, 0) + 1
    objects: dict[str, ReportObjectRow] = {}
    for entry in entries:
        key = entry.card.object_id
        row = objects.get(key) or ReportObjectRow(
            object_id=key,
            object_name=f"Синтетический объект {key.rsplit('-', 1)[1]}",
            cards=0,
            released=0,
            source_alarms=0,
        )
        objects[key] = row.model_copy(
            update={
                "cards": row.cards + 1,
                "released": row.released + (entry.list_state == "released"),
            }
        )
    top = sorted(objects.values(), key=lambda row: (-row.cards, -row.released, row.object_id))
    cards = []
    if entries:
        cards.append(
            ReportCardCounts(
                target_spec_id=ff.FEEDERS,
                issued=len(entries),
                released=states.count("released"),
                no_event=statuses.count("not_realized"),
                unknown=statuses.count("unknown"),
                open=states.count("open"),
            )
        )
    return MonthlyReport(
        mode="fixture",
        month=month,
        period_start=start,
        period_end=end,
        generated_at=ff.FIXTURE_AS_OF,
        data_as_of=ff.FIXTURE_AS_OF,
        cards=cards,
        decisions=[
            ReportDecisionCount(decision_code=code, count=count)
            for code, count in sorted(decisions.items())
        ],
        top_objects=top[:10],
        alarms_by_type=[
            ReportAlarmTypeRow(sensor_type="Датчик дыма", source_alarms=14),
            ReportAlarmTypeRow(sensor_type="Состояние фазы", source_alarms=9),
        ]
        if entries
        else [],
        notes=["Синтетические данные; счётчики — зарегистрированные события, не метрика качества."],
    )
