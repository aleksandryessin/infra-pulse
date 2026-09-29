"""Row-level ``technical_value`` flag for sensor channels (``sensor-failure-v1``).

A technical value is a manufacturer code, a configured technical text or epoch
value, or a numeric reading outside the permissible range of the sensor type,
as configured in ``sensor_technical_values_v1.json``. It is a registered
technical event, not a confirmed failure. The flag does not depend on
``alarm``; ``measurement_exclusion_sql`` applies the organizer rule that
technical values are dropped from measurements only when not alarm-flagged.
Rows are flagged individually; simultaneous rows of a channel are combined by
the label builder.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "sensor_technical_values_v1.json"
REASONS = ("text_code", "epoch_value", "code", "below_range", "above_range")


def load_config(path: Path | None = None) -> dict:
    config = json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))
    for sensor_type, spec in config["sensor_types"].items():
        if spec.get("range") is not None and spec["range"][0] >= spec["range"][1]:
            raise ValueError(f"empty range for {sensor_type}")
    return config


def _alias(alias: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError(f"invalid SQL alias: {alias}")
    return alias


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _number(value: float) -> str:
    return f"CAST({float(value)!r} AS DOUBLE)"


def value_sql(alias: str = "e") -> str:
    """Numeric value of a row: ``value_numeric``, else ``value_raw`` parsed as a number."""
    a = _alias(alias)
    return (
        f"COALESCE({a}.value_numeric, TRY_CAST(replace(trim({a}.value_raw), ',', '.') AS DOUBLE))"
    )


def technical_reason_sql(alias: str = "e", config: dict | None = None) -> str:
    """SQL expression: one of ``REASONS`` or NULL (not technical, unknown type, no value)."""
    config = config or load_config()
    a = _alias(alias)
    value = value_sql(a)
    raw = f"trim({a}.value_raw)"
    tol = float(config["code_tolerance"])
    branches = []
    for sensor_type, spec in config["sensor_types"].items():
        tests = []
        texts = spec.get("text_values", [])
        if texts:
            tests.append(f"WHEN {raw} IN ({', '.join(map(_literal, texts))}) THEN 'text_code'")
        for prefix in spec.get("text_prefixes", []):
            tests.append(f"WHEN starts_with({raw}, {_literal(prefix)}) THEN 'epoch_value'")
        if spec.get("range") is not None:
            codes = sorted({float(x) for x in [*config["common_codes"], *spec["codes"]]})
            code_test = " OR ".join(f"abs({value} - {_number(c)}) <= {tol!r}" for c in codes)
            low, high = spec["range"]
            tests += [
                f"WHEN {code_test or 'false'} THEN 'code'",
                f"WHEN {value} < {_number(low)} THEN 'below_range'",
                f"WHEN {value} > {_number(high)} THEN 'above_range'",
            ]
        if tests:
            branches.append(f"WHEN {_literal(sensor_type)} THEN CASE {' '.join(tests)} END")
    return f"(CASE {a}.sensor_type {' '.join(branches)} END)"


def technical_value_sql(alias: str = "e", config: dict | None = None) -> str:
    """DuckDB boolean expression over ``sensor_type``, ``value_numeric``, ``value_raw``."""
    return f"({technical_reason_sql(alias, config)} IS NOT NULL)"


def measurement_exclusion_sql(alias: str = "e", config: dict | None = None) -> str:
    """Rows to drop from measurement features: technical and not alarm-flagged."""
    a = _alias(alias)
    return f"({technical_value_sql(a, config)} AND NOT COALESCE({a}.alarm, false))"
