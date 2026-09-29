"""Synthetic checks for the numeric ``technical_value`` rule of sensor-failure-v1."""

from __future__ import annotations

import duckdb
import pytest

from infra_pulse_research.modeling.sensor_technical_values import (
    load_config,
    measurement_exclusion_sql,
    technical_reason_sql,
    technical_value_sql,
)

TEMP, GAS, UPS = "Датчик температуры", "Газовый датчик", "ИБП"
GUARD = "Состояние охраны"
# sensor_type, value_numeric, value_raw, alarm, expected reason
CASES = [
    (TEMP, -127.0, "-127", False, "code"),
    (TEMP, -100.0, "-100", False, "code"),
    (TEMP, 255.0, "255", False, "code"),
    (TEMP, -3276.0, "-3276", False, "code"),
    (TEMP, 20.0, "20", False, None),
    (TEMP, 0.0, "0", False, None),
    (TEMP, -40.0, "-40", False, None),
    (TEMP, 85.0, "85", False, None),
    (TEMP, 85.5, "85.5", False, "above_range"),
    (TEMP, -45.0, "-45", False, "below_range"),
    (TEMP, None, "-127", False, "code"),
    (TEMP, None, "-255,0", False, "code"),
    (TEMP, None, "Не определено", True, "text_code"),
    (TEMP, None, " Не определено ", False, "text_code"),
    (TEMP, None, "Неопределен", False, None),
    (TEMP, None, "Норма", False, None),
    (GUARD, None, "01.01.1970 03:00:00", False, "epoch_value"),
    (GUARD, None, "01.01.1970 03:00:01", False, "epoch_value"),
    (GUARD, None, "22.09.2019 20:01:31", False, None),
    (GUARD, None, "На охране", False, None),
    (GAS, None, "Не определено", False, None),
    (GAS, 0.0, "0", False, None),
    (GAS, 0.01, "0.01", False, None),
    (GAS, -0.01, "-0.01", False, "below_range"),
    (GAS, 2.55, "2.55", False, "code"),
    (GAS, 2.5500000000000003, "2.55", False, "code"),
    (GAS, -2.56, "-2.56", False, "code"),
    (GAS, 327.68, "327.68", False, "code"),
    (GAS, 2.56, "2.56", False, None),
    (GAS, 5.78, "5.78", False, None),
    (GAS, 255.0, "255", False, "code"),
    (UPS, 50.0, "50", False, None),
    (UPS, 101.0, "101", False, "above_range"),
    ("Датчик дыма", 255.0, "255", False, None),
    (None, -127.0, "-127", False, None),
    (TEMP, None, None, False, None),
]


def _connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE ev (i INTEGER, sensor_type VARCHAR, value_numeric DOUBLE, "
        "value_raw VARCHAR, alarm BOOLEAN)"
    )
    con.executemany(
        "INSERT INTO ev VALUES (?, ?, ?, ?, ?)",
        [(i, *case[:4]) for i, case in enumerate(CASES)],
    )
    return con


def test_reason_and_flag_per_case() -> None:
    con = _connection()
    rows = con.execute(
        f"SELECT i, {technical_reason_sql('e')}, {technical_value_sql('e')} FROM ev e ORDER BY i"
    ).fetchall()
    for (i, reason, flag), case in zip(rows, CASES, strict=True):
        assert reason == case[4], (i, case)
        assert flag is (case[4] is not None), (i, case)


def test_alarm_does_not_change_flag_but_blocks_exclusion() -> None:
    con = _connection()
    con.execute("INSERT INTO ev VALUES (100, ?, -127, '-127', true)", [TEMP])
    query = (
        f"SELECT {technical_value_sql('x')}, {measurement_exclusion_sql('x')} "
        "FROM ev x WHERE i = 100"
    )
    flag, exclude = con.execute(query).fetchone()
    assert flag is True and exclude is False
    assert con.execute(
        f"SELECT {measurement_exclusion_sql('x')} FROM ev x WHERE i = 0"
    ).fetchone() == (True,)


def test_config_versioned_with_sources_and_valid_ranges() -> None:
    config = load_config()
    assert config["version"] == "sensor-technical-values-v1"
    assert config["sources"]
    assert set(config["sensor_types"]) == {TEMP, GAS, UPS, GUARD}
    assert config["sensor_types"][GAS]["zero_is_technical"] is False
    assert config["sensor_types"][TEMP]["text_values"] == ["Не определено"]
    for spec in config["sensor_types"].values():
        assert spec["range"] is None or spec["range"][0] < spec["range"][1]
        assert spec["range_basis"]


def test_custom_config_and_alias_validation() -> None:
    config = load_config()
    config["sensor_types"][TEMP]["range"] = [-10, 30]
    con = _connection()
    assert con.execute(
        f"SELECT {technical_reason_sql('e', config)} FROM ev e WHERE i = 4"
    ).fetchone() == (None,)
    config["sensor_types"][TEMP]["range"] = [-10, 15]
    assert con.execute(
        f"SELECT {technical_reason_sql('e', config)} FROM ev e WHERE i = 4"
    ).fetchone() == ("above_range",)
    with pytest.raises(ValueError):
        technical_value_sql("e; DROP TABLE ev")
