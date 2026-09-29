from io import StringIO

import pytest

from infra_pulse_research.data.audit import parse_alarm, profile_events


@pytest.mark.parametrize(
    "value, expected", [("t", True), ("false", False), ("true", True), ("f", False)]
)
def test_two_source_boolean_encodings(value, expected):
    assert parse_alarm(value) is expected


def test_invalid_alarm_flag_is_not_silently_false():
    with pytest.raises(ValueError):
        parse_alarm("unknown")


def test_text_states_and_embedded_newlines_are_not_lost():
    csv = StringIO(
        "ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика\n"
        "1,1,2026-01-01,00:00:00,t,Неисправен\n"
        "2,1,2026-01-01,00:00:01,f,Норма\n"
        '3,2,2026-01-01,00:00:02,false,"a\nb"\n'
        "3,2,2026-01-01,00:00:03,unknown,25\n"
    )
    report = profile_events(csv, {"1": "Дым"}, check_ids=True)
    assert report["rows"] == 4
    assert report["technical_fault_state_rows"] == 1
    assert report["nonnumeric_value_rows"] == 3
    assert report["duplicate_event_ids"] == 1
    assert report["unmatched_rows"] == 2
    assert report["invalid_alarm_flags"] == 1
