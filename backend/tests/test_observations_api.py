"""Observation API without PostgreSQL (B1x): JSON batch parser, bearer header, limiter.

The PostgreSQL path (tokens, idempotency, worker, audit, latency) is in
``test_integration_db.py``. All records are synthetic.
"""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from infra_pulse_backend.auth.ldap import roles_for_groups
from infra_pulse_backend.auth.tokens import (
    TOKEN_PATTERN,
    TokenRateLimiter,
    bearer_token,
    new_token,
    new_token_id,
)
from infra_pulse_backend.ingestion.csv_source import FileRejected
from infra_pulse_backend.ingestion.journal_csv import MSK, dedup_identity, parse_journal
from infra_pulse_backend.ingestion.journal_json import parse_batch

CSV_HEADER = '"ид_события","ид_канала_данных","дата","время","тревожное","значение_датчика"'


def batch(records: list, batch_id: str = "b-0001", *, utf8: bool = False) -> bytes:
    """Body as a client sends it: ``\\uXXXX`` escapes by default, raw UTF-8 on request."""
    body = {"batch_id": batch_id, "records": records}
    return json.dumps(body, ensure_ascii=not utf8).encode()


def record(**fields) -> dict:
    base = {
        "event_id": "9000001",
        "channel_id": "700001",
        "date": "2026-09-20",
        "time": "03:09:27",
        "alarm": False,
        "value": "28",
    }
    return {**base, **fields}


def test_batch_records_follow_the_csv_rules():
    parsed = parse_batch(
        batch(
            [
                record(),
                record(event_id="9000002", alarm=True, value="Неисправен", time="23:59:59.5"),
                record(event_id=None, value="1,5"),
                record(date="1970-01-01", time="03:00:00", value="Норма"),
                record(value="01.01.1970 03:00:01"),
            ]
        )
    )
    assert parsed.quarantined == []
    first, second, third, epoch_time, epoch_value = parsed.rows
    assert (first.line_no, first.record_ordinal) == (1, 1)
    assert first.event_at == datetime(2026, 9, 20, 3, 9, 27, tzinfo=MSK)
    assert first.event_at.astimezone(UTC) == datetime(2026, 9, 20, 0, 9, 27, tzinfo=UTC)
    assert first.event_local_raw == "2026-09-20 03:09:27"
    assert (first.value_raw, first.value_numeric) == ("28", 28.0)
    assert (second.alarm, second.value_raw, second.event_at.microsecond) == (
        True,
        "Неисправен",
        500_000,
    )
    # Text is kept verbatim; a decimal comma is not a number (same as CSV).
    assert (third.event_id, third.value_raw, third.value_numeric) == (None, "1,5", None)
    assert epoch_time.is_epoch_placeholder and epoch_value.is_epoch_placeholder
    assert not first.is_epoch_placeholder


def test_csv_names_integer_ids_and_event_at_are_accepted():
    parsed = parse_batch(
        batch(
            [
                {
                    "ид_события": 9000001,
                    "ид_канала_данных": 700001,
                    "дата": "2026-09-20",
                    "время": "03:09:27",
                    "тревожное": True,
                    "значение_датчика": "Обрыв; ТО",
                },
                {
                    "channel_id": "700001",
                    "event_at": "2026-09-20T00:09:27Z",
                    "alarm": False,
                    "value": "28",
                },
                {
                    "channel_id": "700001",
                    "event_at": "2026-09-20T05:09:27.250+05:00",
                    "alarm": False,
                    "value": "29",
                },
            ],
            utf8=True,
        )
    )
    assert parsed.quarantined == []
    csv_names, utc, offset = parsed.rows
    assert (csv_names.event_id, csv_names.channel_id, csv_names.alarm) == (
        "9000001",
        "700001",
        True,
    )
    assert csv_names.value_raw == "Обрыв; ТО"
    assert utc.event_at == csv_names.event_at
    assert utc.event_local_raw == "2026-09-20T00:09:27Z"
    assert offset.event_at == datetime(2026, 9, 20, 0, 9, 27, 250_000, tzinfo=UTC)
    assert offset.event_at.utcoffset() == timedelta(hours=5)


def test_bad_records_are_quarantined_by_number_and_others_accepted():
    records = [
        record(),
        record(date="2026-02-30"),
        record(time="25:00:00"),
        record(channel_id=""),
        record(alarm="t"),
        record(value=28),
        "not an object",
        record(value="a\u0000b"),
        record(value="x" * 5000),
        {"channel_id": "700001", "event_at": "2026-09-20T03:09:27", "alarm": False, "value": "1"},
        record(event_at="2026-09-20T00:09:27Z"),
        record(value="\ud800"),
        record(event_id=1.5),
        record(),
    ]
    parsed = parse_batch(batch(records))
    assert [row.line_no for row in parsed.rows] == [1, 14]
    assert [(item.line_no, item.reason) for item in parsed.quarantined] == [
        (2, "bad_date"),
        (3, "bad_time"),
        (4, "empty_channel"),
        (5, "bad_bool"),
        (6, "bad_column_count"),
        (7, "bad_column_count"),
        (8, "bad_encoding"),
        (9, "value_too_long"),
        (10, "bad_time"),  # event_at without an offset
        (11, "bad_time"),  # both date/time and event_at
        (12, "bad_encoding"),  # lone surrogate: PostgreSQL text cannot hold it
        (13, "bad_column_count"),  # event ID as a float is not an ID
    ]
    assert parsed.records == len(records)
    # Quarantine cells are storable text in CSV column order.
    for item in parsed.quarantined:
        text = item.raw_json()
        assert "\x00" not in text
        text.encode("utf-8")
    assert json.loads(parsed.quarantined[0].raw_json())[:3] == ["9000001", "700001", "2026-02-30"]


def test_same_event_has_the_same_overlap_key_in_csv_and_json():
    csv = (
        CSV_HEADER + '\n9000001,700001,"2026-09-20","03:09:27",false,"28"\n'
        ',700002,"2026-09-20","03:09:28",true,"Неисправен"\n'
    ).encode()
    from_csv = parse_journal(csv).rows
    from_json = parse_batch(
        batch(
            [
                record(),
                {
                    "channel_id": "700002",
                    "event_at": "2026-09-20T00:09:28+00:00",
                    "alarm": True,
                    "value": "Неисправен",
                },
            ]
        )
    ).rows
    assert [dedup_identity(row) for row in from_csv] == [dedup_identity(row) for row in from_json]


def test_a_utf8_bom_is_accepted_like_the_api_does():
    parsed = parse_batch(b"\xef\xbb\xbf" + batch([record()], utf8=True))
    assert [row.value_raw for row in parsed.rows] == ["28"]


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b'{"batch_id": "\xff", "records": []}', "bad_encoding"),
        (b"{not json", "bad_header"),
        (b"[]", "bad_header"),
        (b'{"batch_id": "x", "records": {}}', "bad_header"),
    ],
)
def test_a_body_that_is_not_a_batch_is_rejected(data, code):
    with pytest.raises(FileRejected) as error:
        parse_batch(data)
    assert error.value.code == code


def test_bearer_header_parsing_and_token_shape():
    token = new_token()
    assert TOKEN_PATTERN.fullmatch(token) and token.startswith("ipk_")
    assert new_token() != token
    assert new_token_id().startswith("tok-") and len(new_token_id()) == 16
    assert bearer_token(None) is None
    assert bearer_token("Basic abc") is None
    assert bearer_token(f"Bearer {token}") == token
    assert bearer_token(f"bearer   {token} ") == token
    assert bearer_token("Bearer") == ""
    assert bearer_token("Bearer a b") == ""


def test_rate_limiter_is_a_sliding_minute_per_token():
    now = [0.0]
    limiter = TokenRateLimiter(3, clock=lambda: now[0])
    assert [limiter.acquire("tok-a") for _ in range(3)] == [0, 0, 0]
    assert limiter.acquire("tok-a") == 61
    assert limiter.acquire("tok-b") == 0  # other tokens are independent
    now[0] = 30.0
    assert limiter.acquire("tok-a") == 31
    now[0] = 60.5
    assert limiter.acquire("tok-a") == 0
    with pytest.raises(ValueError):
        TokenRateLimiter(0)


def test_directory_groups_never_grant_the_integration_role():
    assert roles_for_groups(["integration", "analyst"]) == ["analyst"]
    assert roles_for_groups(["INTEGRATION"]) == []


def test_msk_rule_matches_fixed_offset():
    assert MSK.utcoffset(None) == timezone(timedelta(hours=3)).utcoffset(None)
