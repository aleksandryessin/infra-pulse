"""PostgreSQL path of «Сейчас» (F-04) and of whole-scope `/attention` pages.

Rehearsal 29.09.2026 on a copy of the stand: a file of smoke, gas and pump records
received after the seeded phase history put 1 150 records with older events ahead of the
phase alarms in receipt order. «Сейчас» read the window page by page in that order and
showed none of the 108 alarms of an object with an open forecast card. The window is now
aggregated on the server and ordered by event time; the scheme counts only power lines.

Synthetic records only. Needs ``INFRA_TEST_RECEIVED_DSN`` on a disposable database
(``make check-db``); every test uses its own scope.
"""

from datetime import timedelta

import psycopg
from test_forecast_publish_db import PHASE, START, Stream, dsn  # noqa: F401 (fixture)

from infra_pulse_backend.operations import forecast_publish as fp
from infra_pulse_backend.storage.attention_pg import read_attention_page
from infra_pulse_core.contracts.attention import AttentionList, SourceAlarmWindow
from infra_pulse_core.contracts.forecast import ForecastState
from infra_pulse_core.contracts.scheme import ObjectScheme

SMOKE = "Датчик дыма"
PUMP = "Состояние насоса"


def insert(stream: Stream, rows) -> None:
    """Records (channel, object, sensor, event time, receipt time, text, alarm) with
    dense received positions, in the given receipt order."""
    with psycopg.connect(stream.dsn) as connection:
        with connection.cursor() as cursor:
            with cursor.copy(
                """COPY dispatch_observations (namespace_id, snapshot_id, row_uid,
                     channel_id, object_id, sensor_type, system_type, value_raw, alarm,
                     event_at, available_at, availability_basis, source_file,
                     source_sha256, record_ordinal, event_local_raw, received_position)
                   FROM STDIN"""
            ) as copy:
                for channel, object_id, sensor, at, received, value, alarm in rows:
                    stream.position += 1
                    copy.write_row(
                        (
                            stream.namespace,
                            stream.stream,
                            f"row-{stream.position:06d}",
                            channel,
                            object_id,
                            sensor,
                            "Синтетическая система",
                            value,
                            alarm,
                            at,
                            received,
                            "observed",
                            "synthetic-later.csv",
                            "1" * 64,
                            stream.position,
                            at.strftime("%d.%m.%Y %H:%M:%S"),
                            stream.position,
                        )
                    )
        connection.execute(
            """UPDATE dispatch_replay_snapshots SET row_count = %s
               WHERE namespace_id = %s AND snapshot_id = %s""",
            (stream.position, stream.namespace, stream.stream),
        )


def test_now_window_orders_by_event_time_and_counts_every_object(dsn):  # noqa: F811
    stream = Stream(dsn, objects=3, seed=5)
    zeta, omega, other = stream.objects
    pump = f"{stream.stream}-{zeta}-pump"
    with psycopg.connect(dsn) as connection:  # a pump of the reference: not a power line
        fp.pg.upsert_layout(
            connection,
            [
                fp.layout_row(
                    pump,
                    name="Насос 1",
                    object_id=zeta,
                    sensor_type=PUMP,
                    system_type="Водоотведение",
                    tag="SYN-P",
                    reference_version="synthetic-ref-v1",
                )
            ],
        )
    stream.insert(stream.rows_for_days(0, 3, rate={oid: 0 for oid in stream.objects}))
    day = START + timedelta(days=2)
    phase = [
        (stream.channels[omega][index % 3], omega, day + timedelta(hours=1, minutes=index))
        for index in range(5)
    ] + [
        (stream.channels[zeta][index % 3], zeta, day + timedelta(hours=8, minutes=index))
        for index in range(108)
    ]
    stream.insert([(channel, oid, at, "Неисправен") for channel, oid, at in phase])
    panel = stream.channels[other][-1]  # «АВР Ввод1»: a landmark, not a power line
    stream.insert([(panel, other, day + timedelta(hours=7), "Неисправен")])
    latest = day + timedelta(hours=8, minutes=107)
    stream.recompute(latest)
    # Received long after the seeded history (29.09 against June): older events, a fire
    # alarm object without a scheme, a pump of the scheme object and a channel without
    # an object. In receipt order these 162 records come first.
    received = latest + timedelta(days=90)
    smoke = "900900"
    later = (
        [
            (
                f"{stream.stream}-smoke-{index % 12}",
                smoke,
                SMOKE,
                day + timedelta(hours=2),
                received,
                "Обнаружен дым",
                True,
            )
            for index in range(150)
        ]
        + [
            (
                pump,
                zeta,
                PUMP,
                day + timedelta(hours=3, minutes=index),
                received,
                "Неисправен",
                True,
            )
            for index in range(10)
        ]
        + [
            (
                f"{stream.stream}-orphan",
                None,
                SMOKE,
                day + timedelta(hours=4),
                received,
                "Неисправен",
                True,
            )
            for _ in range(2)
        ]
        + [  # not a source alarm: never in the window
            (pump, zeta, PUMP, day + timedelta(hours=9), received, "Норма", False),
        ]
    )
    insert(stream, later)

    with stream.client() as client:
        state = ForecastState.model_validate(client.get("/api/v1/forecast-state").json())
        assert state.source_messages_as_of == state.data_as_of == latest  # the same window
        event_from = (state.source_messages_as_of - timedelta(hours=24)).isoformat()
        response = client.get(
            "/api/v1/attention/source-alarms", params={"event_from": event_from, "limit": 5}
        )
        assert response.status_code == 200, response.text
        window = SourceAlarmWindow.model_validate(response.json())
        by_receipt = AttentionList.model_validate(
            client.get(
                "/api/v1/attention",
                params={"view": "all", "alarm": "true", "limit": 100, "event_from": event_from},
            ).json()
        )
        schemes = {
            object_id: ObjectScheme.model_validate(
                client.get(f"/api/v1/schemes/{object_id}").json()
            )
            for object_id in (zeta, omega, other)
        }
        assert client.get(f"/api/v1/schemes/{smoke}").status_code == 404
        narrow = SourceAlarmWindow.model_validate(
            client.get(
                "/api/v1/attention/source-alarms",
                params={"event_from": event_from, "limit": 1, "object_limit": 2},
            ).json()
        )
        pinned = window.received_watermark
        insert(stream, [(pump, zeta, PUMP, latest, received, "Неисправен", True)])
        again = SourceAlarmWindow.model_validate(
            client.get(
                "/api/v1/attention/source-alarms",
                params={"event_from": event_from, "received_watermark": pinned},
            ).json()
        )
        moved = SourceAlarmWindow.model_validate(
            client.get("/api/v1/attention/source-alarms", params={"event_from": event_from}).json()
        )

    # Receipt order (the old block): the first hundred holds only the later file.
    assert by_receipt.total == 276
    assert {item.message.object_id for item in by_receipt.items} == {smoke}
    # Event order: the newest phase alarms of the scheme object come first.
    assert window.record_count == 276
    assert (window.object_count, window.without_object_count) == (4, 2)
    assert [message.object_id for message in window.latest] == [zeta] * 5
    assert window.latest[0].event_at == latest
    assert [message.event_at for message in window.latest] == sorted(
        (message.event_at for message in window.latest), reverse=True
    )
    objects = {item.object_id: item for item in window.objects}
    assert [item.object_id for item in window.objects] == [zeta, other, smoke, omega]
    assert (objects[zeta].record_count, objects[zeta].channel_count) == (118, 4)
    assert objects[zeta].first_event_at == day + timedelta(hours=3)
    assert objects[zeta].last_message.row_uid == window.latest[0].row_uid
    assert objects[zeta].last_message.object_name == f"Синтетический объект {zeta}"
    assert objects[smoke].record_count == 150 and objects[smoke].channel_count == 12
    # The scheme counts only its power lines, for the same window: the same numbers.
    for object_id, scheme in schemes.items():
        on_lines = sum(feeder.alarm_records_24h or 0 for feeder in scheme.feeders)
        assert objects[object_id].scheme_record_count == on_lines, object_id
    assert (objects[zeta].scheme_record_count, objects[omega].scheme_record_count) == (108, 5)
    assert objects[other].scheme_record_count == 0  # the input panel is not a power line
    assert objects[smoke].scheme_record_count == 0
    # Fewer objects asked: all are still counted.
    assert [item.object_id for item in narrow.objects] == [zeta, other]
    assert (narrow.object_count, narrow.record_count, len(narrow.latest)) == (4, 276, 1)
    # A pinned watermark keeps the window; without it the new record is counted.
    assert again.model_dump(exclude={"as_of"}) == window.model_dump(exclude={"as_of"}) | {
        "latest": again.model_dump()["latest"]
    }
    assert again.latest[:5] == window.latest
    assert again.record_count == 276 and moved.record_count == 277
    assert moved.received_watermark == pinned + 1


def test_whole_scope_pages_keep_receipt_order_and_counts(dsn):  # noqa: F811
    """`/attention` without filters merges one index range per source flag (true, false,
    not provided) instead of sorting the scope; pages and counts are those of the plain
    order ``available_at DESC, row_uid``."""
    stream = Stream(dsn, objects=1, seed=11)
    (object_id,) = stream.objects
    channel = stream.channels[object_id][0]
    rows = []
    for index in range(90):
        at = START + timedelta(minutes=index)
        flag = (True, False, None)[index % 3]
        value = "Обесточен" if index % 5 == 0 else "Норма"
        received = START + timedelta(hours=index // 7)  # files: equal receipt times
        rows.append((channel, object_id, PHASE, at, received, value, flag))
    insert(stream, rows)
    settings = stream.settings()
    as_of = START + timedelta(days=1)

    expected = sorted(
        (
            {"row_uid": f"row-{position:06d}", "received": row[4], "row": row}
            for position, row in enumerate(rows, start=1)
        ),
        key=lambda item: item["row_uid"],
    )
    expected.sort(key=lambda item: item["received"], reverse=True)
    for view in ("all", "attention"):
        chosen = [
            item
            for item in expected
            if view == "all" or item["row"][6] is True or item["row"][5] == "Обесточен"
        ]
        if view == "attention":
            chosen.sort(key=lambda item: 0 if item["row"][6] is True else 1)
        for offset in (0, 7, 40, 85):
            page = read_attention_page(
                settings.db_dsn.get_secret_value(),
                namespace_id=stream.namespace,
                snapshot_id=stream.stream,
                as_of=as_of,
                mode="received",
                offset=offset,
                limit=10,
                view=view,
            )
            assert [item.message.row_uid for item in page.items] == [
                item["row_uid"] for item in chosen[offset : offset + 10]
            ], (view, offset)
            assert page.all_records_total == 90
            assert page.total == len(chosen)
            assert page.source_alarm_count == 30
            assert page.watch_text_count == sum(
                item["row"][6] is not True and item["row"][5] == "Обесточен" for item in chosen
            )
    # A pinned received watermark bounds every branch of the merged page.
    page = read_attention_page(
        settings.db_dsn.get_secret_value(),
        namespace_id=stream.namespace,
        snapshot_id=stream.stream,
        as_of=as_of,
        mode="received",
        offset=0,
        limit=100,
        view="all",
        received_watermark=45,
    )
    assert page.all_records_total == page.total == 45
    assert {item.message.row_uid for item in page.items} == {
        f"row-{position:06d}" for position in range(1, 46)
    }
