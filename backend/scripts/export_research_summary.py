"""Export the «Исследование» summary (contract ``ResearchSummary``) from the v9 report.

Usage (from the repository root)::

    uv run --locked python backend/scripts/export_research_summary.py \
        --output backend/research/research-summary.json

The API serves the file named by ``INFRA_RESEARCH_SUMMARY_PATH`` outside fixture mode
(``api/research.py``). Every number is read from the checked-in aggregated report
``data-science/reports/sensor-failure-final-v9-2026-09-27.json`` (sections
``holdout_v9`` and ``posthoc_baselines``); nothing is recomputed and no identifiers are
read. The texts of the scopes without metrics come from the decision log named in
``source_reports``. The script only reads JSON: it does not import research code,
MLflow or training libraries.

``--check`` compares the output file with a fresh export and exits 1 on a difference,
so a stale file is caught by the test suite (``backend/tests/test_research_export.py``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from infra_pulse_core.contracts.research import (
    ResearchLadderRow,
    ResearchMetric,
    ResearchScope,
    ResearchSummary,
)

ROOT = Path(__file__).resolve().parents[2]
REPORT = "data-science/reports/sensor-failure-final-v9-2026-09-27.json"
FINAL_V9 = "data-science/experiments/sensor-failure/FINAL_V9.md"
DECISION_LOG = "data-science/experiments/sensor-failure/DECISION_LOG.md"
DEFAULT_OUTPUT = "backend/research/research-summary.json"
VERSION = "research-summary-v9-2026-09-27"
HOLDOUT = "отложенный период 2025-07…2026-06"


def _r(value: float) -> float:
    return round(float(value), 3)


def _comma(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _ci_metric(name: str, interval: dict) -> ResearchMetric:
    return ResearchMetric(
        name=name,
        value=_r(interval["point"]),
        ci_low=_r(interval["low"]),
        ci_high=_r(interval["high"]),
    )


def _config_ladder(config: dict) -> list[ResearchLadderRow]:
    weeks = config["ci_weeks"]
    return [
        ResearchLadderRow(
            step="static_list",
            label="статистический список",
            metrics=[
                _ci_metric("precision", weeks["card_precision"]),
                _ci_metric("recall_incident", weeks["R_incident"]),
            ],
        )
    ]


def _random_metric(name: str, spread: dict) -> ResearchMetric:
    """Median of 1 000 random issues; the bounds are the 2.5–97.5% range, not a CI."""
    return ResearchMetric(
        name=name,
        value=_r(spread["median"]),
        ci_low=_r(spread["low"]),
        ci_high=_r(spread["high"]),
    )


def _prod_scope(report: dict) -> ResearchScope:
    holdout = report["holdout_v9"]
    config = holdout["configurations"]["prod_phase"]
    weeks, objects = config["ci_weeks"], config["ci_objects"]
    counts = config["holdout"]
    baselines = report["posthoc_baselines"]["periods"]["holdout"]["k"][str(config["k"])]
    persistence, random = baselines["persistence"], baselines["random"]
    lift = baselines["list_minus_random_median"]
    static = ResearchLadderRow(
        step="static_list",
        label="статистический список",
        metrics=[
            _ci_metric("precision", weeks["card_precision"]),
            _ci_metric("recall_incident", weeks["R_incident"]),
            _ci_metric("precision_by_object", objects["card_precision"]),
            ResearchMetric(name="lift_precision", value=_r(lift["card_precision"])),
            ResearchMetric(name="lift_recall", value=_r(lift["R_incident"])),
        ],
    )
    return ResearchScope(
        scope_id="phase_feeders",
        title="Обесточивание электрооборудования объекта",
        sensor_types=["Состояние фазы"],
        status="in_product",
        horizon_days=14,
        budget_k=config["k"],
        policy="статистический список за 365 сут; карточка снимается в момент события",
        evaluation_period=HOLDOUT,
        period_use_count=holdout["holdout_use"],
        events_per_year=counts["events"],
        ladder=[
            static,
            ResearchLadderRow(
                step="persistence",
                label="повтор прошлого периода",
                metrics=[
                    ResearchMetric(name="precision", value=_r(persistence["card_precision"])),
                    ResearchMetric(name="recall_incident", value=_r(persistence["R_incident"])),
                ],
            ),
            ResearchLadderRow(
                step="random_list",
                label="случайный список",
                metrics=[
                    _random_metric("precision", random["card_precision"]),
                    _random_metric("recall_incident", random["R_incident"]),
                ],
            ),
        ],
        note=(
            "Целевые показатели проекта P ≥ 0,7 и R ≥ 0,5 выполнены по нижним границам "
            "95% ДИ по неделям. "
            f"Период использован {holdout['holdout_use']}-й раз. По объектам нижняя граница "
            f"P {_comma(objects['card_precision']['low'])} — ниже 0,70; карточки получали "
            f"{counts['pairs_issued']} объектов, "
            f"{round(counts['chronic_share_issued'] * 100)}% карточек — хронические. "
            "Потеря связи «Неисправен» — зарегистрированное событие, не подтверждённая поломка."
        ),
    )


def _demo_scope(
    report: dict, key: str, scope_id: str, title: str, sensors: list[str], note: str
) -> ResearchScope:
    holdout = report["holdout_v9"]
    config = holdout["configurations"][key]
    return ResearchScope(
        scope_id=scope_id,
        title=title,
        sensor_types=sensors,
        status="research_only",
        horizon_days=14,
        budget_k=config["k"],
        policy="статистический список за 365 сут; маска сессий ТО дыма и теплового",
        evaluation_period=HOLDOUT,
        period_use_count=holdout["holdout_use"],
        events_per_year=config["holdout"]["events"],
        ladder=_config_ladder(config),
        note=note,
    )


MASK_SHIFT = (
    "С 2025-11 почти каждое «Обнаружен дым» приходит с alarm = true, детектор ТО сессий не "
    "нашёл: на этом периоде маска фактически не применялась."
)


def build_summary(report: dict) -> ResearchSummary:
    holdout = report["holdout_v9"]
    detector = holdout["configurations"]["demo_fire_detector_failure_k5"]
    scopes: list[ResearchScope] = [
        _prod_scope(report),
        _demo_scope(
            report,
            "demo_S1_mask_k13",
            "without_rare",
            "Без редких типов",
            ["все типы, кроме редких по числу каналов"],
            "Целевые показатели проекта не выполнены: R по инцидентам ниже 0,5. "
            "Метрику определяет фаза. " + MASK_SHIFT,
        ),
        _demo_scope(
            report,
            "demo_S4_mask_k14",
            "tz_sensors",
            "Датчики ТЗ",
            ["дым", "тепловой", "температура", "газ", "КД Дверь", "КД АВ", "движение"],
            "Целевые показатели проекта не выполнены: P и R ниже 0,5; 87% событий области — дым. "
            + MASK_SHIFT,
        ),
        _demo_scope(
            report,
            "demo_fire_mask_k6",
            "fire_layer",
            "Пожарный слой",
            ["Датчик дыма", "Тепловой датчик"],
            "Целевые показатели проекта не выполнены: P около 0,6, R около 0,4. " + MASK_SHIFT,
        ),
        ResearchScope(
            scope_id="fire_detector_failure",
            title="Отказ пожарных извещателей",
            sensor_types=["Датчик дыма", "Тепловой датчик"],
            status="research_only",
            horizon_days=14,
            budget_k=detector["k"],
            policy="объект × пожарная подсистема; маска сессий ТО",
            evaluation_period=HOLDOUT,
            period_use_count=holdout["holdout_use"],
            note=(
                "Числа отложенного периода не показываются: по FINAL_V9 их нужно сначала "
                "проверить технологу (сдвиг практики ТО или её записи с 2025-11). На dev "
                "2023–2024 это по сути реестр 7–8 хронических объектов, а не прогноз отказа."
            ),
        ),
        ResearchScope(
            scope_id="gas",
            title="Газовые датчики",
            sensor_types=["Газовый датчик"],
            status="needs_more_data",
            evaluation_period="dev 2023–2024",
            note=(
                "Потерь связи за 2023–2024 — 183; «Обнаружен газ» — в основном серии проверок "
                "газоанализаторов при ППР или ТО. "
                "По истории пары не предсказывается; реестр газоанализаторов прогнозом "
                "не является."
            ),
        ),
        ResearchScope(
            scope_id="temperature",
            title="Датчики температуры",
            sensor_types=["Датчик температуры"],
            status="needs_more_data",
            evaluation_period="dev 2023–2024",
            note="Потерь связи за 2023–2024 — 33; по истории пары не предсказывается.",
        ),
        ResearchScope(
            scope_id="security",
            title="Охранные датчики",
            sensor_types=["КД Дверь", "КД АВ", "Датчик движения"],
            status="needs_more_data",
            evaluation_period="dev 2023–2024",
            note=(
                "Потерь связи за 2023–2024: КД АВ — 90, КД Дверь — 73, движение — 72. "
                "60% «Не замкнут» у КД АВ — открытия людьми, а не отказы."
            ),
        ),
    ]
    return ResearchSummary(
        version=VERSION,
        synthetic=False,
        source_reports=[REPORT, FINAL_V9, DECISION_LOG],
        scopes=scopes,
        caveats=[
            "Отложенный период 2025-07…2026-06: пять использований для метрик, шестое — "
            "только калибровка «k из n»; определения полноты по инцидентам и снятия по "
            "событию приняты 27.09 после третьего и четвёртого использования.",
            "В квадратных скобках — 95% ДИ bootstrap по неделям. У случайного списка — "
            "медиана и диапазон 2,5–97,5% по 1 000 случайным выдачам, это не ДИ.",
            "Случайный список и повтор прошлого периода — справочно, после заморозки.",
            "Карточки с неизвестным исходом в P не входят; «k из n» — не вероятность для объекта.",
        ],
    )


def export(report_path: Path) -> str:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = build_summary(report)
    return summary.model_dump_json(indent=2, exclude_none=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, default=ROOT / REPORT)
    parser.add_argument("--output", type=Path, default=ROOT / DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail if --output is stale")
    args = parser.parse_args(argv)
    text = export(args.report)
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if current != text:
            print(f"{args.output} is stale: run this script without --check", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output} ({len(text)} bytes, {VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
