from io import StringIO

from infra_pulse_research.data.audit import profile_events


def test_missing_date_and_fault_without_alarm_remain_visible():
    events = StringIO(
        "ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика\n"
        "1,1,2026-05-31,12:00:00,t,Неисправен\n"
        "2,1,2026-06-02,12:00:00,f,Неисправен\n"
    )
    report = profile_events(events, {"1": "Датчик дыма"})
    assert report["missing_calendar_dates"] == ["2026-06-01"]
    assert report["fault_state_without_alarm_rows"] == 1
    assert report["technical_fault_channels"] == 1
    assert report["known_state_rows"]["Неисправен"] == 2
