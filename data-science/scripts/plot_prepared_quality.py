"""Plot monthly raw alarm rates from a prepared-data quality audit."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt


def run(audit: Path, output: Path, policy_manifest: Path | None = None) -> None:
    report = json.loads(audit.read_text(encoding="utf-8"))
    totals = defaultdict(lambda: [0, 0])
    gas = {}
    for row in report["monthly_by_sensor"]:
        month = row["month"]
        totals[month][0] += row["records"]
        totals[month][1] += row["alarms"]
        if row["sensor_type"] == "Газовый датчик":
            gas[month] = row
    first, last = min(totals), max(totals)
    year, month = map(int, first.split("-"))
    all_months = []
    while f"{year:04d}-{month:02d}" <= last:
        all_months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    adjusted_totals = {month: counts.copy() for month, counts in totals.items()}
    adjusted_gas = {month: [row["records"], row["alarms"]] for month, row in gas.items()}
    if policy_manifest is not None:
        policy = json.loads(policy_manifest.read_text(encoding="utf-8"))
        for item in policy["by_rule_month"]:
            month = item["month"]
            adjusted_totals[month][0] -= item["excluded_rows"]
            adjusted_totals[month][1] -= item["alarm_true"]
            if item["sensor_type"] is None:
                adjusted_gas[month] = [0, 0]
            else:
                adjusted_gas[month][0] -= item["excluded_rows"]
                adjusted_gas[month][1] -= item["alarm_true"]
    figure, axes = plt.subplots(2, 1, figsize=(13, 7.5), sharex=True)
    figure.subplots_adjust(left=0.09, right=0.98, top=0.89, bottom=0.16, hspace=0.24)
    for axis, selected, adjusted, title in (
        (axes[0], totals, adjusted_totals, "Все типы каналов"),
        (axes[1], gas, adjusted_gas, "Газовый датчик"),
    ):
        months = [dt.date.fromisoformat(month + "-01") for month in all_months]
        original_rates = []
        working_rates = []
        for month in all_months:
            if month not in selected:
                original_rates.append(float("nan"))
            elif selected is totals:
                count, alarms = selected[month]
                original_rates.append(100 * alarms / count if count else float("nan"))
            else:
                original_rates.append(100 * selected[month]["alarm_rate"])
            count, alarms = adjusted.get(month, [0, 0])
            working_rates.append(100 * alarms / count if count else float("nan"))
        if policy_manifest is not None:
            axis.plot(
                months,
                original_rates,
                marker="o",
                markersize=2.4,
                linewidth=1,
                linestyle="--",
                color="#a0a7ae",
                label="Исходный v4",
            )
        axis.plot(
            months,
            working_rates if policy_manifest is not None else original_rates,
            marker="o",
            markersize=2.8,
            linewidth=1.25,
            color="#1767a4",
            label="После исследовательского исключения" if policy_manifest else None,
        )
        axis.axvspan(dt.date(2021, 4, 1), dt.date(2021, 7, 1), color="#dda15e", alpha=0.28)
        if policy_manifest is not None:
            axis.axvspan(dt.date(2020, 2, 1), dt.date(2020, 4, 1), color="#8294a5", alpha=0.18)
        axis.set_title(title, loc="left", fontsize=11, fontweight="bold")
        axis.set_ylabel("Доля alarm=true, %")
        axis.grid(True, alpha=0.25)
    july = dt.date(2021, 7, 1)
    if "2021-07" in gas and policy_manifest is None:
        axes[1].scatter(
            [july], [100 * gas["2021-07"]["alarm_rate"]], color="#c0392b", s=42, zorder=5
        )
        axes[1].annotate(
            "июль 2021: остаточный разрыв",
            (july, 100 * gas["2021-07"]["alarm_rate"]),
            xytext=(12, 10),
            textcoords="offset points",
            fontsize=9,
            color="#9b2d23",
        )
    axes[1].xaxis.set_major_locator(mdates.YearLocator())
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axes[1].set_xlabel("Месяц исходных событий")
    if policy_manifest is not None:
        axes[0].legend(loc="upper right", frameon=False, fontsize=8)
    figure.suptitle("Исходный флаг тревоги в подготовленном журнале", fontsize=14)
    figure.text(
        0.09,
        0.035,
        (
            "Серый фон: исследовательски исключённые февраль–март 2020; "
            "оранжевый: апрель–июнь 2021. "
            "Газ: отдельно 02.07.2021 и 06.11.2025."
            if policy_manifest is not None
            else "Затенено: исключённый апрель–июнь 2021. "
            "Пустой день не означает alarm=false. Это доля записей, не частота физических аварий."
        ),
        fontsize=8,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160, bbox_inches="tight")
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy-manifest", type=Path)
    args = parser.parse_args()
    run(args.audit, args.output, args.policy_manifest)


if __name__ == "__main__":
    main()
