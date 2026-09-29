"""PostgreSQL path of «обесточивание фидеров объекта»: recompute, publication and reads.

Synthetic observations only (no customer data). Needs ``INFRA_TEST_RECEIVED_DSN`` on a
disposable database (``make check-db``); every test uses its own scope.
"""

import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from app_role import app_dsn
from fastapi.testclient import TestClient

from infra_pulse_backend.api import forecast_db
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_backend.operations import forecast_publish as fp
from infra_pulse_core.contracts.forecast import (
    ForecastCardView,
    ForecastJournalCounts,
    ForecastJournalList,
    ForecastList,
    ForecastQualitySummary,
    ForecastState,
)
from infra_pulse_core.contracts.scheme import ObjectScheme, ObjectSchemeList
from infra_pulse_core.features import channel_names as cn
from infra_pulse_core.features import incident_list as il
from infra_pulse_core.features.phase_feeder_episodes import MSK

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PHASE = "Состояние фазы"
START = datetime(2024, 1, 1, tzinfo=MSK)
NAMES = ["ГРО{n} ПК{p}", "ФВ{n} ПК{p}-{q}", "ФАНС{n} ПК{p}", "Резерв {n}"]


@pytest.fixture(scope="module")
def dsn() -> str:
    value = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not value:
        pytest.skip("local PostgreSQL integration DSN not provided")
    with psycopg.connect(value) as connection:
        for _ in range(2):  # every migration is re-applied by the loader: idempotent
            for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                connection.execute(migration.read_text(encoding="utf-8"))
    return app_dsn(value)


class Stream:
    """A synthetic received scope: layout, observations with dense positions."""

    def __init__(
        self,
        dsn: str,
        objects: int = 12,
        days: int = 420,
        seed: int = 7,
        names: list[str] = NAMES,
    ):
        self.dsn = dsn
        self.namespace = "b2-test"
        self.stream = f"b2-{uuid4().hex[:10]}"
        self.position = 0
        self.random = random.Random(seed)
        self.objects = [str(900000 + index) for index in range(objects)]
        self.channels: dict[str, list[str]] = {}
        with psycopg.connect(dsn) as connection:
            connection.execute(
                """INSERT INTO dispatch_replay_snapshots (namespace_id, snapshot_id,
                     manifest_sha256, window_start, window_end, row_count, alarm_count,
                     scope_kind)
                   VALUES (%s, %s, NULL, %s, %s, 0, 0, 'received')""",
                (self.namespace, self.stream, START, START + timedelta(days=days)),
            )
            layout = []
            for number, object_id in enumerate(self.objects):
                ids = []
                for index, template in enumerate(names):
                    channel = f"{self.stream}-{object_id}-{index}"
                    ids.append(channel)
                    name = template.format(n=index + 1, p=10 * index + number, q=10 * index + 5)
                    layout.append(
                        fp.layout_row(
                            channel,
                            name=name,
                            object_id=object_id,
                            sensor_type=PHASE,
                            system_type="Диспетчерский контроль",
                            tag=f"SYN-{index}",
                            reference_version="synthetic-ref-v1",
                        )
                    )
                landmark = f"{self.stream}-{object_id}-in"
                layout.append(
                    fp.layout_row(
                        landmark,
                        name=f"АВР Ввод1 ПК{number}",
                        object_id=object_id,
                        sensor_type=PHASE,
                        system_type="Диспетчерский контроль",
                        tag="SYN-IN",
                        reference_version="synthetic-ref-v1",
                    )
                )
                ids.append(landmark)
                self.channels[object_id] = ids
            fp.pg.upsert_layout(connection, layout)
            fp.pg.upsert_objects(
                connection, [(oid, f"Синтетический объект {oid}", "syn") for oid in self.objects]
            )

    def rows_for_days(self, first: int, last: int, *, rate: dict[str, float] | None = None):
        """Daily «Есть питание» on every channel and random «Неисправен» episodes of
        feeders (object ``i`` fails with probability ~ its rate per day)."""
        rows = []
        for day in range(first, last):
            base = START + timedelta(days=day)
            for number, object_id in enumerate(self.objects):
                chance = (rate or {}).get(object_id, 0.02 + 0.03 * number)
                for index, channel in enumerate(self.channels[object_id]):
                    rows.append((channel, object_id, base + timedelta(hours=6), "Есть питание"))
                    if index < 3 and self.random.random() < chance:
                        start = base + timedelta(hours=10, minutes=index * 3)
                        rows.append((channel, object_id, start, "Неисправен"))
                        rows.append((channel, object_id, start + timedelta(minutes=2), "Обесточен"))
                        rows.append((channel, object_id, start + timedelta(hours=2), "Норма"))
        return rows

    def insert(self, rows) -> None:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                with cursor.copy(
                    """COPY dispatch_observations (namespace_id, snapshot_id, row_uid,
                         channel_id, object_id, sensor_type, system_type, value_raw, alarm,
                         event_at, available_at, availability_basis, source_file,
                         source_sha256, record_ordinal, event_local_raw, received_position)
                       FROM STDIN"""
                ) as copy:
                    for channel, object_id, at, value in rows:
                        self.position += 1
                        copy.write_row(
                            (
                                self.namespace,
                                self.stream,
                                f"row-{self.position}",
                                channel,
                                object_id,
                                PHASE,
                                "Диспетчерский контроль",
                                value,
                                value == "Неисправен",
                                at,
                                at,
                                "observed",
                                "synthetic.csv",
                                "0" * 64,
                                self.position,
                                at.strftime("%d.%m.%Y %H:%M:%S"),
                                self.position,
                            )
                        )
            connection.execute(
                """UPDATE dispatch_replay_snapshots SET row_count = %s
                   WHERE namespace_id = %s AND snapshot_id = %s""",
                (self.position, self.namespace, self.stream),
            )

    def recompute(self, as_of: datetime, **kwargs) -> fp.PublishResult:
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            return fp.recompute(
                connection,
                data_as_of=as_of,
                namespace_id=self.namespace,
                snapshot_id=self.stream,
                **kwargs,
            )

    def settings(self) -> Settings:
        return Settings(
            mode="received",
            db_dsn=self.dsn,
            received_namespace=self.namespace,
            received_stream_id=self.stream,
            _env_file=None,
        )

    def client(self) -> TestClient:
        return TestClient(create_app(self.settings()))

    def fetch(self, sql: str, *params):
        with psycopg.connect(self.dsn) as connection:
            return connection.execute(sql, params).fetchall()


def end_of(day: int) -> datetime:
    return START + timedelta(days=day) - timedelta(seconds=1)


def test_recompute_publishes_rolling_list_and_reads_it_back(dsn):
    stream = Stream(dsn)
    stream.insert(stream.rows_for_days(0, 380))
    first = stream.recompute(end_of(380), list_from=(START + timedelta(days=370)).date())
    assert first.generation == 1
    assert first.cutoffs == 10  # cutoffs 370..379 00:00 are not later than data_as_of
    assert first.new_card_ids
    cards = stream.fetch(
        """SELECT status, card FROM forecast_cards WHERE namespace_id = %s AND snapshot_id = %s""",
        stream.namespace,
        stream.stream,
    )
    scored = [card for status, card in cards if status == "scored"]
    assert scored and all(card["score"]["kind"] == "frequency_share" for card in scored)
    assert all(min(3, card["channels_total"]) <= len(card["channels"]) <= 5 for card in scored)
    assert all(card["channels_total"] >= len(card["channels"]) for card in scored)
    assert all(card["recurrence"] in ("chronic", "fresh") for card in scored)
    text = str(scored)
    for forbidden in ("ввод ПК", "фаза A", "6 каналов"):
        assert forbidden not in text

    with stream.client() as client:
        listing = client.get("/api/v1/forecasts", params={"limit": 100})
        assert listing.status_code == 200, listing.text
        page = ForecastList.model_validate(listing.json())
        assert 1 <= page.total <= 10
        assert page.as_of == end_of(380)
        assert all(view.list_state == "open" for view in page.items)
        view = page.items[0]
        assert view.recommendation.text.startswith("Передать дежурному энергетику")
        assert "ресурсоснабжающей организации" in view.recommendation.text
        assert view.recommendation.policy_version == "phase-feeders-recommendation-v5"
        assert view.recommendation.decision_code is None
        assert view.recommendation.regulation_confirmed is False
        for item in page.items:  # v5: at most two groups of the card lines, by priority
            lines = {
                cn.recommendation_group(line.channel_name, line.feeder_kind)
                for line in item.card.channels
            }
            groups = [g for g in il.RECOMMENDATION_GROUP_ORDER if g in lines][:2]
            rules = [rule for rule in item.recommendation.rule_ids if rule != "repeat_realized"]
            assert rules == [f"group_{group}" for group in groups]
            assert len(item.recommendation.details) == len(item.recommendation.rule_ids)
        assert view.days_total == 14
        card = client.get(f"/api/v1/forecasts/{view.card.id}")
        assert card.status_code == 200
        ForecastCardView.model_validate(card.json())
        journal = client.get("/api/v1/forecast-journal", params={"limit": 100})
        assert journal.status_code == 200, journal.text
        ForecastJournalList.model_validate(journal.json())
        counts = ForecastJournalCounts.model_validate(
            client.get("/api/v1/forecast-journal/summary").json()
        )
        assert sum(row.cards_issued for row in counts.rows) == len(scored)
        quality = client.get("/api/v1/forecast-journal/quality")
        assert quality.status_code == 200, quality.text
        summary = ForecastQualitySummary.model_validate(quality.json())
        assert {m.name for row in summary.rows for m in row.event_metrics} == {
            "r_incident",
            "r_strict",
        }
        state = ForecastState.model_validate(client.get("/api/v1/forecast-state").json())
        assert state.generation == 1 and state.data_as_of == end_of(380)
        schemes = ObjectSchemeList.model_validate(client.get("/api/v1/schemes").json())
        assert schemes.total == 12
        scheme = ObjectScheme.model_validate(
            client.get(f"/api/v1/schemes/{view.card.object_id}").json()
        )
        assert scheme.landmarks and scheme.landmarks[0].kind == "ats"
        assert any(feeder.picket.form == "unknown" for feeder in scheme.feeders)
        assert view.card.id in scheme.open_card_ids
        assert client.get("/api/v1/registries/recurring").status_code == 200
        assert client.get("/api/v1/forecasts/unknown-card").status_code == 404
        # /attention carries the picket parsed from the channel name (B2 layout).
        attention = client.get(
            "/api/v1/attention",
            params={"view": "all", "limit": 20, "alarm": "true", "object_id": view.card.object_id},
        )
        assert attention.status_code == 200, attention.text
        messages = [item["message"] for item in attention.json()["items"]]
        assert messages and all(m["picket_basis"] == "channel_name" for m in messages)
        assert {m["picket_form"] for m in messages} <= {"point", "range"}
        # F6 B-2: «Сейчас» — window of 24 h before the cut, total in the window, line names.
        day_ago = end_of(380) - timedelta(hours=24)
        everything = client.get(
            "/api/v1/attention", params={"view": "all", "limit": 5, "alarm": "true"}
        ).json()
        window = client.get(
            "/api/v1/attention",
            params={
                "view": "all",
                "limit": 100,
                "alarm": "true",
                "event_from": day_ago.isoformat(),
            },
        )
        assert window.status_code == 200, window.text
        day = window.json()
        assert 0 < day["total"] < everything["total"]
        day_messages = [item["message"] for item in day["items"]]
        assert all(datetime.fromisoformat(m["event_at"]) >= day_ago for m in day_messages)
        assert all(m["channel_name"] and m["feeder_kind"] for m in day_messages)
        # F6 B-1: one generation — «с прошлой выдачи» counts releases of the last day only.
        released = ForecastList.model_validate(
            client.get("/api/v1/forecasts", params={"list_state": "released", "limit": 100}).json()
        )
        horizon = state.horizons[0]
        since = horizon.issued_at - timedelta(days=1)
        expected = sum(1 for item in released.items if item.released_at > since)
        assert horizon.released_since_previous == expected <= released.total
        # F6 B-3: an alarm record followed by «Норма» carries the time it was cleared.
        alarms = [
            alarm
            for summary in schemes.items
            for feeder in ObjectScheme.model_validate(
                client.get(f"/api/v1/schemes/{summary.object_id}").json()
            ).feeders
            for alarm in feeder.current_alarms
            if feeder.alarm_records_24h is not None
            and feeder.alarm_records_24h >= len(feeder.current_alarms)
        ]
        assert alarms and all(alarm.event_at >= day_ago for alarm in alarms)
        assert all(alarm.cleared_value_raw == "Норма" for alarm in alarms if alarm.cleared_at)
        assert any(alarm.cleared_at is not None for alarm in alarms)


def test_recommendation_v5_repeat_and_section_switch_at_read(dsn):
    """Policy v5 is assembled at read time: the card lines, the pair's earlier cards and
    their releases (repeat) and the section switch of the object's layout. The same card reads
    the same recommendation at any ``as_of`` it is visible (spec recommendation-v5, 3–4)."""
    names = ["ГРО{n} ПК{p}", "ФРО{n} ПК{p}-{q}", "ПУИ{n} ПК{p}", "Резерв {n}"]  # lighting only
    stream = Stream(dsn, objects=3, seed=5, names=names)
    section_object = stream.objects[0]
    with psycopg.connect(dsn) as connection:
        fp.pg.upsert_layout(
            connection,
            [
                fp.layout_row(
                    f"{stream.stream}-{section_object}-sec",
                    name="Межсекционный автомат ПК3",
                    object_id=section_object,
                    sensor_type=PHASE,
                    system_type="Диспетчерский контроль",
                    tag="SYN-SEC",
                    reference_version="synthetic-ref-v1",
                )
            ],
        )
    stream.insert(stream.rows_for_days(0, 400, rate={oid: 0.35 for oid in stream.objects}))
    stream.recompute(end_of(400), list_from=(START + timedelta(days=370)).date())
    with stream.client() as client:
        journal = ForecastJournalList.model_validate(
            client.get("/api/v1/forecast-journal", params={"limit": 100}).json()
        )
        assert 0 < journal.total <= 100
        entries = journal.items
        repeats = sections = lighting = 0
        for entry in entries:
            card = entry.card
            # Expected by the spec, from the journal: cards of the object in [t − 30 d, t].
            since = card.issued_at - timedelta(days=30)
            same = [
                other
                for other in entries
                if other.card.object_id == card.object_id
                and since <= other.card.issued_at <= card.issued_at
            ]
            earlier = [
                other
                for other in same
                if other.card.status == "scored" and other.card.issued_at < card.issued_at
            ]
            latest = max(earlier, key=lambda other: other.card.issued_at, default=None)
            realized = (
                latest is not None
                and latest.released_at is not None
                and latest.released_at <= card.issued_at
            )
            expected = []
            if card.status == "scored":
                if realized and len(same) >= il.RECOMMENDATION_REPEAT_MIN_CARDS:
                    expected.append("repeat_realized")
                # Lines in an episode or its shadow at the cutoff are not candidates of the card.
                lines = {
                    cn.recommendation_group(line.channel_name, line.feeder_kind)
                    for line in card.channels
                }
                assert lines <= {"lighting", None}
                if "lighting" in lines:
                    expected.append("group_lighting")
                if card.object_id == section_object:
                    expected.append("group_section")
            view = ForecastCardView.model_validate(
                client.get(f"/api/v1/forecasts/{card.id}").json()
            )
            recommendation = view.recommendation
            assert recommendation.rule_ids == expected, card.id
            assert recommendation.text == (
                il.RECOMMENDATION_TEXT
                if card.status == "scored"
                else il.ABSTAINED_RECOMMENDATION_TEXT
            )
            if "repeat_realized" in expected:
                repeats += 1
                assert recommendation.details[0] == il.RECOMMENDATION_REPEAT_TEXT.format(
                    n=len(same)
                )
            if "group_section" in expected:
                assert recommendation.details[-1] == il.RECOMMENDATION_GROUP_TEXT["section"]
            # Point in time: at the card's own cutoff the recommendation is the same.
            early = ForecastCardView.model_validate(
                client.get(
                    f"/api/v1/forecasts/{card.id}", params={"as_of": card.issued_at.isoformat()}
                ).json()
            )
            assert early.recommendation == recommendation
            sections += "group_section" in expected
            lighting += "group_lighting" in expected
        assert repeats > 0 and sections > 0 and lighting > 0


def test_release_at_event_new_generation_and_stale_cursor(dsn):
    stream = Stream(dsn, seed=11)
    stream.insert(stream.rows_for_days(0, 380))
    stream.recompute(end_of(380), list_from=(START + timedelta(days=375)).date())
    with stream.client() as client:
        first = client.get("/api/v1/forecasts", params={"limit": 1}).json()
    assert first["next_cursor"]
    open_card = ForecastList.model_validate(first).items[0].card
    # A failure of a listed feeder of the open card's object the next day.
    channel = open_card.channels[0].channel_id
    event_at = START + timedelta(days=380, hours=15)
    stream.insert(
        [
            (channel, open_card.object_id, event_at, "Неисправен"),
            (channel, open_card.object_id, event_at + timedelta(hours=1), "Норма"),
        ]
        + stream.rows_for_days(380, 381, rate={oid: 0 for oid in stream.objects})
    )
    second = stream.recompute(end_of(381))
    assert second.generation == 2
    assert open_card.id in second.released_card_ids
    with stream.client() as client:
        stale = client.get("/api/v1/forecasts", params={"cursor": first["next_cursor"]})
        assert stale.status_code == 409
        view = ForecastCardView.model_validate(
            client.get(f"/api/v1/forecasts/{open_card.id}").json()
        )
        assert view.list_state == "released" and view.released_at == event_at
        # Negative test: before the event the same card is open, the release is not seen.
        before = client.get(
            f"/api/v1/forecasts/{open_card.id}",
            params={"as_of": (event_at - timedelta(minutes=1)).isoformat()},
        ).json()
        assert before["list_state"] == "open" and before["released_at"] is None
        journal = ForecastJournalList.model_validate(
            client.get(
                "/api/v1/forecast-journal",
                params={"as_of": (event_at - timedelta(minutes=1)).isoformat(), "limit": 100},
            ).json()
        )
        entry = next(item for item in journal.items if item.card.id == open_card.id)
        assert entry.outcome.status == "pending" and entry.events == []
        after = ForecastJournalList.model_validate(
            client.get("/api/v1/forecast-journal", params={"limit": 100}).json()
        )
        entry = next(item for item in after.items if item.card.id == open_card.id)
        assert entry.outcome.status == "realized"
        assert entry.outcome.first_event_at == event_at
        assert entry.events[0].while_open is True
        # Cards of later cutoffs are invisible at an earlier as_of.
        early = client.get(
            "/api/v1/forecast-journal",
            params={"as_of": (START + timedelta(days=375, hours=1)).isoformat(), "limit": 100},
        ).json()
        assert all(item["card"]["issued_at"] <= early["as_of"] for item in early["items"])


def test_card_snapshot_is_immutable(dsn):
    stream = Stream(dsn, objects=3, seed=3)
    stream.insert(stream.rows_for_days(0, 372))
    stream.recompute(end_of(372), list_from=(START + timedelta(days=370)).date())
    with psycopg.connect(dsn) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                """UPDATE forecast_cards SET card = '{}'::jsonb
                   WHERE namespace_id = %s AND snapshot_id = %s""",
                (stream.namespace, stream.stream),
            )


def test_incremental_uploads_equal_one_run(dsn):
    whole = Stream(dsn, objects=5, seed=5)
    parts = Stream(dsn, objects=5, seed=5)
    rows = whole.rows_for_days(0, 390)
    parts_rows = parts.rows_for_days(0, 390)
    assert [r[2:] for r in rows] == [r[2:] for r in parts_rows]
    whole.insert(rows)
    whole.recompute(end_of(390), list_from=(START + timedelta(days=366)).date())
    split = [0, 200, 367, 371, 380, 390]
    for low, high in zip(split, split[1:], strict=False):
        chunk = [
            r
            for r in parts_rows
            if START + timedelta(days=low) <= r[2] < START + timedelta(days=high)
        ]
        parts.insert(chunk)
        parts.recompute(end_of(high), list_from=(START + timedelta(days=366)).date())

    def snapshot(stream: Stream, sql: str):
        rows = stream.fetch(sql, stream.namespace, stream.stream)
        return sorted(tuple(str(v).replace(stream.stream, "S") for v in row) for row in rows)

    for sql in (
        """SELECT channel_id, start_at, end_at, power_off_at FROM forecast_phase_episodes
           WHERE namespace_id = %s AND snapshot_id = %s""",
        """SELECT event_id, start_at, size FROM forecast_phase_events
           WHERE namespace_id = %s AND snapshot_id = %s""",
        """SELECT card_id, status, issued_at, candidate_channel_ids FROM forecast_cards
           WHERE namespace_id = %s AND snapshot_id = %s""",
        """SELECT card_id, status, release_at FROM forecast_card_outcomes
           WHERE namespace_id = %s AND snapshot_id = %s""",
    ):
        assert snapshot(whole, sql) == snapshot(parts, sql)


def test_gap_marks_partial_coverage_and_warms_up(dsn):
    stream = Stream(dsn, objects=4, seed=9)
    stream.insert(stream.rows_for_days(0, 380))
    stream.recompute(end_of(380), list_from=(START + timedelta(days=375)).date())
    # A 20-day gap (like July 2026), then one day of data.
    stream.insert(stream.rows_for_days(400, 402))
    result = stream.recompute(end_of(402))
    cutoffs = dict(
        stream.fetch(
            """SELECT cutoff_at, ranked FROM forecast_cutoffs
               WHERE namespace_id = %s AND snapshot_id = %s""",
            stream.namespace,
            stream.stream,
        )
    )
    assert cutoffs[START + timedelta(days=390)] == 0  # lookback day uncovered: nothing eligible
    assert cutoffs[START + timedelta(days=400)] == 0  # warm-up: the day before had no data
    assert cutoffs[START + timedelta(days=401)] > 0
    partial = stream.fetch(
        """SELECT card -> 'freshness' ->> 'coverage' FROM forecast_cards
           WHERE namespace_id = %s AND snapshot_id = %s AND issued_at >= %s""",
        stream.namespace,
        stream.stream,
        START + timedelta(days=401),
    )
    assert result.generation == 2
    assert partial and all(row[0] == "partial" for row in partial)


def test_object_without_a_year_of_history_is_abstained(dsn):
    stream = Stream(dsn, objects=3, seed=4)
    stream.insert(stream.rows_for_days(0, 200))
    stream.recompute(end_of(200), list_from=(START + timedelta(days=190)).date())
    rows = stream.fetch(
        """SELECT status, card ->> 'abstention_reason' FROM forecast_cards
           WHERE namespace_id = %s AND snapshot_id = %s""",
        stream.namespace,
        stream.stream,
    )
    assert rows and all(row == ("abstained", "insufficient_history") for row in rows)
    with stream.client() as client:
        listing = client.get("/api/v1/forecasts", params={"status": "abstained"})
        assert listing.status_code == 200
        assert listing.json()["total"] == 3
        journal = client.get("/api/v1/forecast-journal").json()
        assert all(item["outcome"]["excluded_from_quality_metrics"] for item in journal["items"])


def test_decision_check_finds_published_card(dsn):
    stream = Stream(dsn, objects=3, seed=2)
    stream.insert(stream.rows_for_days(0, 372))
    stream.recompute(end_of(372), list_from=(START + timedelta(days=370)).date())
    settings = stream.settings()
    card_id = stream.fetch(
        "SELECT card_id FROM forecast_cards WHERE namespace_id = %s AND snapshot_id = %s",
        stream.namespace,
        stream.stream,
    )[0][0]
    assert forecast_db.forecast_card(settings, card_id, as_of=None) is not None
    assert forecast_db.forecast_card(settings, "missing", as_of=None) is None


def test_now_block_counts_the_objects_of_the_scheme_for_the_same_window(dsn):
    """Audit 29.09.2026: «Сейчас» counted objects on the first page of 100 alarm messages.

    108 newer alarm messages of one object pushed the 5 of another out of the first page
    («113 сообщений · объектов: 1»). Every page of the 24 h window gives both objects, the
    same as the scheme for that window. The scheme counts only power lines of the layout:
    an alarm of another channel (here an input panel) is in «Сейчас» and not on the scheme,
    which the block's hint says.
    """
    stream = Stream(dsn, objects=3, seed=5)
    zeta, omega, other = stream.objects
    stream.insert(stream.rows_for_days(0, 3, rate={oid: 0 for oid in stream.objects}))
    day = START + timedelta(days=2)
    rows = [
        (
            stream.channels[omega][index % 3],
            omega,
            day + timedelta(hours=1, minutes=index),
            "Неисправен",
        )
        for index in range(5)
    ]
    rows += [
        (
            stream.channels[zeta][index % 3],
            zeta,
            day + timedelta(hours=8, minutes=index),
            "Неисправен",
        )
        for index in range(108)
    ]
    panel = stream.channels[other][-1]  # «АВР Ввод1»: a landmark, not a power line
    rows.append((panel, other, day + timedelta(hours=7), "Неисправен"))
    stream.insert(rows)
    latest = day + timedelta(hours=8, minutes=107)
    stream.recompute(latest)
    with stream.client() as client:
        state = ForecastState.model_validate(client.get("/api/v1/forecast-state").json())
        assert state.source_messages_as_of == state.data_as_of == latest  # the same window
        params = {
            "view": "all",
            "alarm": "true",
            "limit": 100,
            "event_from": (state.source_messages_as_of - timedelta(hours=24)).isoformat(),
        }
        first = client.get("/api/v1/attention", params=params).json()
        items = list(first["items"])
        while len(items) < first["total"]:
            page = client.get(
                "/api/v1/attention",
                params={
                    **params,
                    "offset": len(items),
                    "received_watermark": first["received_watermark"],
                },
            ).json()
            items += page["items"]
        schemes = ObjectSchemeList.model_validate(client.get("/api/v1/schemes").json())
    assert first["total"] == len(items) == 114
    assert {item["message"]["object_id"] for item in first["items"]} == {zeta}
    now_objects = {item["message"]["object_id"] for item in items}
    scheme_objects = {item.object_id for item in schemes.items if item.current_alarms}
    assert scheme_objects == {zeta, omega}
    assert now_objects == scheme_objects | {other}


def test_state_names_the_days_without_data_before_the_latest_cutoff(dsn):
    """P1-2 (rehearsal 29.09.2026): an empty forecast after a jump in time explains itself.

    The cutoffs issued on days without data have no cards; the state names those days
    (MSK) until the day before the latest cutoff has data again.
    """
    stream = Stream(dsn, objects=2, seed=3)
    stream.insert(stream.rows_for_days(0, 380))
    stream.recompute(end_of(380), list_from=(START + timedelta(days=375)).date())

    def state() -> ForecastState:
        with stream.client() as client:
            return ForecastState.model_validate(client.get("/api/v1/forecast-state").json())

    assert (state().no_data_from, state().no_data_to) == (None, None)
    # 20 days without data, then one day: the latest cutoff (day 400) had no data before it.
    stream.insert(stream.rows_for_days(400, 401))
    stream.recompute(end_of(401))
    jumped = state()
    assert jumped.horizons[0].issued_at == START + timedelta(days=400)
    assert (jumped.no_data_from, jumped.no_data_to) == (
        (START + timedelta(days=380)).date(),
        (START + timedelta(days=399)).date(),
    )
    ranked = stream.fetch(
        """SELECT sum(ranked) FROM forecast_cutoffs
           WHERE namespace_id = %s AND snapshot_id = %s AND cutoff_at > %s""",
        stream.namespace,
        stream.stream,
        START + timedelta(days=380),
    )
    assert ranked == [(0,)]
    # The next cutoff has a day of data before it: nothing to explain.
    stream.insert(stream.rows_for_days(401, 402))
    stream.recompute(end_of(402))
    assert (state().no_data_from, state().no_data_to) == (None, None)
