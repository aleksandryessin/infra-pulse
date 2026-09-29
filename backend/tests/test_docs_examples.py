"""Synthetic examples of docs/examples parse cleanly and agree with each other.

The files are what docs/INTEGRATION_API.md («Сквозная проверка за 5 минут») tells an
expert to load. Here they are only parsed (no PostgreSQL); the path to «published» is
``test_docs_examples_db.py``. The headers that the document lists are the parser's headers.
"""

import json
from datetime import datetime
from pathlib import Path

from infra_pulse_backend.api.observations import JSON_EXAMPLE
from infra_pulse_backend.ingestion.journal_csv import (
    JOURNAL_HEADER,
    MSK,
    TZ_JOURNAL_HEADER,
    parse_journal,
)
from infra_pulse_backend.ingestion.journal_json import parse_batch
from infra_pulse_backend.ingestion.reference_csv import HEADERS, parse_reference

DOCS = Path(__file__).resolve().parents[2] / "docs"
EXAMPLES = DOCS / "examples"


def read(name: str) -> bytes:
    return (EXAMPLES / name).read_bytes()


def test_references_are_clean_and_synthetic():
    channels = parse_reference(read("reference-channels.csv"), "channels")
    objects = parse_reference(read("reference-objects.csv"), "objects")
    states = parse_reference(read("reference-states.csv"), "states")
    for parsed in (channels, objects, states):
        assert parsed.quarantined == [] and parsed.duplicates == 0
    assert 10 <= len(channels.rows) <= 20 and 2 <= len(objects.rows) <= 3
    channel_ids = {row[1] for row in channels.rows}
    object_ids = {row[1] for row in objects.rows}
    assert all(channel.startswith("demo-") for channel in channel_ids)
    assert {row[6] for row in channels.rows} == object_ids
    assert all(row[5].startswith("Синт. объект") for row in objects.rows)
    # States describe the text sensors of the channels (temperature is a number);
    # «Состояние фазы» feeds the forecast.
    state_types = {row[1] for row in states.rows}
    assert "Состояние фазы" in state_types
    assert state_types == {row[3] for row in channels.rows} - {"Датчик температуры"}


def test_journals_are_one_day_on_known_channels():
    channels = {row[1] for row in parse_reference(read("reference-channels.csv"), "channels").rows}
    journal = parse_journal(read("journal-2026-06-29.csv"))
    assert journal.layout == "organizers_export" and journal.quarantined == []
    assert 100 <= len(journal.rows) <= 200
    assert {row.event_at.astimezone(MSK).date().isoformat() for row in journal.rows} == {
        "2026-06-29"
    }
    assert {row.channel_id for row in journal.rows} <= channels
    assert [row.value_raw for row in journal.rows].count("Неисправен") == 3

    appendix = parse_journal(read("journal-appendix1-2026-06-29.csv"))
    assert appendix.layout == "tz_appendix1" and appendix.quarantined == []
    assert appendix.alarm_not_provided == len(appendix.rows) > 0
    assert {row.channel_id for row in appendix.rows} <= channels
    # «18,20» is kept as text: only a dot makes a number.
    assert all(row.value_numeric is None and "," in row.value_raw for row in appendix.rows)


def test_batches_follow_the_journal_and_the_documented_example():
    assert json.loads(read("batch-000184.json")) == JSON_EXAMPLE
    first = parse_batch(read("batch-000184.json"), "json")
    second = parse_batch(read("batch-000185.xml"), "xml")
    last_journal = max(row.event_at for row in parse_journal(read("journal-2026-06-29.csv")).rows)
    for parsed in (first, second):
        assert parsed.quarantined == []
        # The day after the journal, about 12:00 MSK: time moves forward by hours, not months.
        assert all(last_journal < row.event_at for row in parsed.rows)
        assert all(row.event_at < datetime(2026, 7, 1, tzinfo=MSK) for row in parsed.rows)
    assert max(row.event_at for row in first.rows) < min(row.event_at for row in second.rows)


def test_integration_document_lists_the_parser_headers():
    """A ``bad_header`` report gives only the code: the document must name the columns."""
    text = (DOCS / "INTEGRATION_API.md").read_text(encoding="utf-8")
    for header in (*HEADERS.values(), JOURNAL_HEADER):
        assert ",".join(header) in text, header
    assert ";".join(TZ_JOURNAL_HEADER) in text  # the Appendix 1 example uses «;»
    for name in sorted(path.name for path in EXAMPLES.iterdir()):
        assert f"examples/{name}" in text, name
