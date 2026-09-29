"""docs/examples on PostgreSQL: the expert's five-minute path reaches «published».

The synthetic references, the journal of 29.06.2026 (both layouts) and the two batches of
30.06.2026 that docs/INTEGRATION_API.md («Сквозная проверка за 5 минут») tells an expert
to load go through the same HTTP routes, worker and forecast recompute as on the stand.
One day of history is less than the 365 days the list needs, so the cutoff 30.06.2026
00:00 MSK gives cards without a score (abstained), never a scored card. A file declared
with the wrong format fails ``bad_header``.
Needs ``INFRA_TEST_RECEIVED_DSN`` (``make check-db``); own schema, dropped after.
"""

import io
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from app_role import app_dsn
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_backend.storage import integration_pg
from infra_pulse_backend.worker import __main__ as worker_main
from infra_pulse_backend.worker import runner
from infra_pulse_core.contracts.imports import ImportFile
from infra_pulse_core.features.phase_feeder_episodes import MSK

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples"
NAMESPACE = "docs-examples"
STREAM = "docs-examples-1"


@pytest.fixture
def dsn():
    base = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not base:
        pytest.skip("local PostgreSQL integration DSN not provided")
    schema = f"ex_{uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    scoped = make_conninfo(base, options=f"-csearch_path={schema}")
    worker_main.apply_migrations(scoped, MIGRATIONS)
    try:
        yield app_dsn(scoped)
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_examples_reach_published_in_the_documented_order(dsn, tmp_path):
    settings = Settings(
        mode="received",
        db_dsn=dsn,
        upload_dir=tmp_path / "uploads",
        received_namespace=NAMESPACE,
        received_stream_id=STREAM,
        _env_file=None,
    )
    client = TestClient(create_app(settings))
    config = runner.WorkerConfig(
        dsn=dsn,
        upload_dir=settings.upload_dir,
        namespace_id=NAMESPACE,
        stream_id=STREAM,
        worker_id="worker-examples",
        lease_seconds=60,
        retry_seconds=0,
    )
    recompute = runner.resolve_recompute()

    def finished(import_id: str) -> ImportFile:
        while runner.run_once(config, recompute):
            pass
        response = client.get(f"/api/v1/imports/{import_id}")
        assert response.status_code == 200, response.text
        return ImportFile.model_validate(response.json())

    def upload(name: str, file_format: str) -> ImportFile:
        response = client.post(
            "/api/v1/imports",
            files={"file": (name, io.BytesIO((EXAMPLES / name).read_bytes()), "text/csv")},
            data={"format": file_format},
        )
        assert response.status_code == 202, response.text
        return finished(response.json()["id"])

    # A journal declared as the channel reference: the whole file is refused; the report
    # gives only the code, the expected columns are in docs/INTEGRATION_API.md.
    wrong = upload("journal-2026-06-29.csv", "reference_channels_csv")
    assert (wrong.status, wrong.error_code, wrong.rows_total) == ("failed", "bad_header", None)

    for name, file_format, rows in (
        ("reference-channels.csv", "reference_channels_csv", 16),
        ("reference-objects.csv", "reference_objects_csv", 3),
        ("reference-states.csv", "reference_states_csv", 11),
    ):
        loaded = upload(name, file_format)
        assert (loaded.status, loaded.rows_accepted, loaded.rows_quarantined) == (
            "published",
            rows,
            0,
        ), loaded

    journal = upload("journal-2026-06-29.csv", "journal_csv")
    assert (journal.status, journal.rows_accepted, journal.unknown_channels) == (
        "published",
        134,
        0,
    ), journal
    assert journal.new_card_ids == []  # no 00:00 cutoff after the first record yet
    appendix = upload("journal-appendix1-2026-06-29.csv", "journal_csv")
    assert (appendix.status, appendix.source_layout, appendix.alarm_not_provided) == (
        "published",
        "tz_appendix1",
        24,
    ), appendix

    token, _ = integration_pg.create_token(
        dsn, name="demo", actor="docs-examples", request_id="req-examples"
    )
    posted = {}
    for name, content_type in (
        ("batch-000184.json", "application/json"),
        ("batch-000185.xml", "application/xml"),
    ):
        response = client.post(
            "/api/v1/observations",
            content=(EXAMPLES / name).read_bytes(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        )
        assert response.status_code == 202, response.text
        posted[name] = finished(response.json()["id"])
    first, second = posted["batch-000184.json"], posted["batch-000185.xml"]
    assert (first.status, first.rows_accepted, first.uploaded_by) == (
        "published",
        3,
        "integration:demo",
    ), first
    assert (second.status, second.rows_accepted, second.source_container) == (
        "published",
        2,
        "xml",
    ), second
    # The first batch passes the cutoff 30.06.2026 00:00 MSK: one day of history, so every
    # object with phase channels gets a card without a score.
    with psycopg.connect(dsn) as connection:
        cards = connection.execute(
            """SELECT card_id, object_id, status, issued_at FROM forecast_cards
               WHERE namespace_id = %s AND snapshot_id = %s ORDER BY object_id""",
            (NAMESPACE, STREAM),
        ).fetchall()
    assert set(first.new_card_ids) == {card_id for card_id, *_ in cards}
    assert [(obj, status, at) for _, obj, status, at in cards] == [
        (obj, "abstained", datetime(2026, 6, 30, tzinfo=MSK))
        for obj in ("demo-obj-1", "demo-obj-2", "demo-obj-3")
    ]
    assert second.new_card_ids == []
    # Data time follows the records: 30.06.2026 12:04:58 MSK, the last record of the XML batch.
    state = client.get("/api/v1/forecast-state").json()
    last = datetime(2026, 6, 30, 12, 4, 58, tzinfo=MSK)
    assert datetime.fromisoformat(state["data_as_of"]) == last
