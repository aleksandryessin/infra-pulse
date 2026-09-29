"""Publish aggregate evidence of the local phase-sequence run (28.09.2026).

Reads the ignored ``run.json`` and prepared ``manifest.json`` of
``infra_pulse_research.sequence`` and writes ``reports/phase-sequence-2026-09-28.{json,md}``.
Only aggregates leave the artifacts: no object, channel or journal identifiers, no weekly rows.
Standard library only, so the default ``train`` group is enough (no torch).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "data-science/artifacts/sequence-v1"
REPORTS = ROOT / "data-science/reports"
STATIC = "static_v9_core"
MODELS = ("gru", "lstm", "tcn", "transformer", "catboost")
NAMES = {
    STATIC: "Статичный список (тот же протокол)",
    "gru": "GRU",
    "lstm": "LSTM",
    "tcn": "TCN",
    "transformer": "Transformer Encoder",
    "catboost": "CatBoost, 16 признаков",
}
PERIODS = {"dev2024": "07–12.2024", "viewed2025_2026": "07.2025–06.2026"}
# Ordinal of the read of 07.2025–06.2026 as counted in FINAL_V9/EVALUATION: five metric reads,
# the sixth for «k из n» calibration; this run is the seventh (diagnostic, no product choice).
HOLDOUT_USE = 7
ORDINALS = {7: "седьмое", 8: "восьмое"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    """Checkout-relative path; an input from another checkout keeps only its repo part."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        parts = resolved.parts
        return (
            "/".join(parts[parts.index("data-science") :]) if "data-science" in parts else path.name
        )


def git_sha256(rev: str, path: str) -> str | None:
    try:
        blob = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{rev}:{path}"],
            check=True,
            capture_output=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return hashlib.sha256(blob).hexdigest()


def code_hashes(identity: dict, revs: list[str]) -> list[dict]:
    rows = []
    for path, recorded in sorted(identity["code_sha256"].items()):
        local = ROOT / path
        row = {
            "path": path,
            "run_sha256": recorded,
            "matches_checkout": local.exists() and sha256(local) == recorded,
        }
        for rev in revs:
            found = git_sha256(rev, path)
            row[f"matches_{rev}"] = None if found is None else found == recorded
        rows.append(row)
    return rows


def training_summary(details: dict | None) -> dict | None:
    if not details:
        return None
    out = {k: v for k, v in details.items() if k != "trace"}
    trace = details.get("trace")
    if trace:
        best = min(trace, key=lambda t: t["validation_log_loss"])
        out |= {
            "epochs_run": len(trace),
            "best_epoch": best["epoch"],
            "best_validation_log_loss": best["validation_log_loss"],
        }
    return out


def summarize(
    run_path: Path,
    manifest_path: Path,
    config_path: Path,
    reconciliation: Path,
    output: Path,
    revs: list[str],
) -> dict:
    run = json.loads(run_path.read_text())
    if run["status"] != "complete":
        raise ValueError("do not publish a partial run as a completed result")
    manifest = json.loads(manifest_path.read_text())
    if sha256(manifest_path) != run["identity"]["prepared_manifest_sha256"]:
        raise ValueError("prepared manifest does not belong to this run")
    config = json.loads(config_path.read_text())
    config_matches = all(config.get(k) == v for k, v in run["config"].items())

    runs = []
    for item in run["models"]:
        runs.append(
            {
                "fold": item["fold"],
                "model": item["model"],
                "seed": item.get("seed"),
                "metrics": item["metrics"],
                "paired_vs_static": item.get("paired_vs_static"),
                "split_counts": item.get("split_counts"),
                "training": training_summary(item.get("training")),
                "checkpoint_sha256": item.get("checkpoint_sha256"),
            }
        )

    summary = []
    for fold in (f["name"] for f in run["config"]["folds"]):
        static = next(r for r in runs if r["fold"] == fold and r["model"] == STATIC)
        base_p = static["metrics"]["card_precision"]
        base_r = static["metrics"]["incident_recall"]
        for model in (STATIC, *MODELS):
            items = [r for r in runs if r["fold"] == fold and r["model"] == model]
            p = mean(r["metrics"]["card_precision"] for r in items)
            r_ = mean(r["metrics"]["incident_recall"] for r in items)
            paired = [r["paired_vs_static"] for r in items if r["paired_vs_static"]]
            summary.append(
                {
                    "fold": fold,
                    "model": model,
                    "seeds": [r["seed"] for r in items],
                    "card_precision_mean": p,
                    "incident_recall_mean": r_,
                    "incident_recall_min": min(r["metrics"]["incident_recall"] for r in items),
                    "incident_recall_max": max(r["metrics"]["incident_recall"] for r in items),
                    "delta_precision_pp": 100 * (p - base_p),
                    "delta_recall_pp": 100 * (r_ - base_r),
                    "cards_issued_mean": mean(r["metrics"]["cards_issued"] for r in items),
                    "cards_unknown_mean": mean(r["metrics"]["cards_unknown"] for r in items),
                    "incident_heads": items[0]["metrics"]["incident_heads"],
                    "paired_seeds": len(paired),
                    "seeds_recall_ci_lower_below_zero": sum(
                        x["recall_delta_95"][0] < 0 for x in paired
                    ),
                    "seeds_recall_ci_upper_above_zero": sum(
                        x["recall_delta_95"][1] > 0 for x in paired
                    ),
                    "recall_delta_95_lower_min": min(
                        (x["recall_delta_95"][0] for x in paired), default=None
                    ),
                    "recall_delta_95_upper_max": max(
                        (x["recall_delta_95"][1] for x in paired), default=None
                    ),
                }
            )

    recon = None
    if reconciliation.exists():
        source = json.loads(reconciliation.read_text())["baseline_reconciliation"]
        if source["original_run_sha256"] != sha256(run_path):
            raise ValueError("reconciliation refers to a different sequence run")
        recon = {
            "source": rel(reconciliation),
            "folds": [
                {
                    "fold": f["fold"],
                    "v9_protocol": {
                        k: f["published_protocol_recomputed"][k]
                        for k in (
                            "cards_issued",
                            "card_precision",
                            "incidents",
                            "captured_incidents",
                            "R_incident",
                        )
                    },
                    "sequence_protocol": {
                        k: f["sequence_protocol_recomputed"][k]
                        for k in (
                            "cards_issued",
                            "card_precision",
                            "incident_heads",
                            "incident_heads_caught",
                            "incident_recall",
                        )
                    },
                    "issued_shared": f["issued_shared"],
                    "issued_v9_only": f["issued_v9_only"],
                    "issued_sequence_only": f["issued_sequence_only"],
                    "score_disagreement": f["cutoff_comparison"]["score_disagreement"],
                    "eligible_disagreement": f["cutoff_comparison"]["eligible_disagreement"],
                    "v9_denominator": f["v9_denominator"],
                    "sequence_denominator": f["sequence_denominator"],
                    "warm_start": f["warm_start"],
                    "censoring": f["censoring"],
                }
                for f in source["folds"]
            ],
        }

    hashes = code_hashes(run["identity"], revs)
    lock = ROOT / "uv.lock"
    identity = {k: v for k, v in run["identity"].items() if k != "code_sha256"}
    lock_check = {"checkout": lock.exists() and sha256(lock) == identity["lock_sha256"]}
    lock_check |= {rev: git_sha256(rev, "uv.lock") == identity["lock_sha256"] for rev in revs}
    if lock.exists():
        locked = tomllib.loads(lock.read_text())["package"]
        lock_check["checkout_torch"] = next(
            (p["version"] for p in locked if p["name"] == "torch"), None
        )
    report = {
        "report": output.stem,
        "status": run["status"],
        "evaluation": run["evaluation"],
        "calibration": run["calibration"],
        "holdout": {
            "period": "2025-07-01..2026-07-01 (fold viewed2025_2026)",
            "use": HOLDOUT_USE,
            "choice_for_product": False,
            "note": "both test periods were viewed before; diagnostic comparison only",
        },
        "source": {
            "run": rel(run_path),
            "run_sha256": sha256(run_path),
            "run_signature": run["signature"],
            "prepared_manifest": rel(manifest_path),
            "prepared_manifest_sha256": sha256(manifest_path),
            "config": rel(config_path),
            "config_matches_run": config_matches,
            "entrypoint": run["entrypoint"],
            "summary_code_sha256": sha256(Path(__file__)),
        },
        "identity": identity,
        "code_hashes": hashes,
        "lock_matches": lock_check,
        "prepared": {
            k: manifest[k]
            for k in (
                "objects",
                "rows",
                "eligible",
                "unknown",
                "truncated",
                "start",
                "end",
                "availability",
                "reference",
                "coverage",
                "snapshot_manifest_sha256",
                "overlay_manifest_sha256",
            )
        }
        | {"truncated_share": manifest["truncated"] / manifest["rows"]},
        "config": run["config"],
        "summary": summary,
        "runs": runs,
        "baseline_reconciliation": recon,
        "limits": [
            "viewed periods: 07.2025-06.2026 is the seventh read, not an independent holdout",
            "protocol is stricter than v9 section 3.4; numbers are not comparable with v9",
            "paired weekly bootstrap is descriptive; object bootstrap and ablations are planned",
            "128 messages cover less than 90 days in most slices; 90 days is an upper bound",
            "objects are the same in time; transfer to new objects was not tested",
            "scores are uncalibrated; AP/ROC AUC/Brier are not probabilities",
            "label is a registered loss of connection, not a confirmed failure",
            "run made on a dirty tree; see code_hashes",
        ],
    }
    output.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
    )
    output.with_suffix(".md").write_text(render(report, revs) + "\n")
    return report


def num(value: float, digits: int = 3) -> str:
    text = f"{value:.{digits}f}".replace(".", ",")
    if text.lstrip("-").strip("0,") == "":
        return text.lstrip("-")
    return text.replace("-", "−")


def grouped(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def signed(value: float, digits: int = 1) -> str:
    text = num(value, digits)
    return text if text.startswith("−") or text.strip("0,") == "" else "+" + text


def render(report: dict, revs: list[str]) -> str:
    by = {(s["fold"], s["model"]): s for s in report["summary"]}
    folds = [f["name"] for f in report["config"]["folds"]]
    identity = report["identity"]
    prepared = report["prepared"]
    lines = [
        "# Последовательности фазы: диагностика 28.09.2026",
        "",
        "Агрегированный отчёт локального прогона `phase-sequence-v1`. Диагностика на уже",
        "просмотренной истории, продукт v9 не меняет. Метка — зарегистрированная потеря связи",
        "«Неисправен» (`technical_fault_state_proxy`), а не подтверждённая поломка; `alarm` —",
        "не подтверждённый отказ. Протокол — [README](../experiments/sequence/README.md),",
        "конфигурация — [sequence_research_v1.json](../configs/sequence_research_v1.json),",
        "числа — [JSON рядом](phase-sequence-2026-09-28.json).",
        "",
        "## Постановка",
        "",
        "- Фаза, 14 суток, до 10 открытых карточек, карточка снимается в момент события;",
        "  выдача — общий core `select_new`, срез 00:00 МСК.",
        "- GRU, LSTM, TCN, Transformer Encoder: последние 128 сообщений объекта за 90 суток",
        "  (текст состояния по словарю train, alarm, возраст, интервал, покрытие).",
        "  CatBoost — 16 табличных признаков. Seed 17, 43, 91; CPU.",
        "- Конфигурация зафиксирована до обучения, ранняя остановка — по validation logloss;",
        "  test эпоху не выбирает. Оценки не откалиброваны.",
        "",
        "| Фолд | Обучение | Validation | Test |",
        "|---|---|---|---|",
    ]
    for f in report["config"]["folds"]:
        lines.append(
            f"| `{f['name']}` | {f['train_start']} … {f['validation_start']} | "
            f"{f['validation_start']} … {f['test_start']} | {f['test_start']} … {f['test_end']} |"
        )
    lines += [
        "",
        f"Период 07.2025–06.2026 — **{ORDINALS[report['holdout']['use']]} использование**"
        " отложенного",
        "периода v9 (пять — для метрик, шестое — калибровка «k из n»). Период 07–12.2024 тоже",
        "просматривался раньше. Выбора для продукта по этому прогону нет.",
        "",
        "## Результат",
        "",
        "Среднее трёх seed; P — точность карточек с известным исходом, R — полнота по головам",
        "инцидентов (пауза ≥ 24 ч). Протокол строже раздела v9, поэтому с P / `R_incident` v9",
        "эти числа не сравниваются (сверка ниже).",
        "",
        "| Метод | " + " | ".join(PERIODS[f] for f in folds) + " |",
        "|---|" + "---|" * len(folds),
    ]
    for model in (STATIC, *MODELS):
        cells = [
            f"{num(by[(f, model)]['card_precision_mean'])} / "
            f"{num(by[(f, model)]['incident_recall_mean'])}"
            for f in folds
        ]
        lines.append(f"| {NAMES[model]} | " + " | ".join(cells) + " |")
    deltas = [by[(f, m)]["delta_recall_pp"] for f in folds for m in MODELS]
    lower = sum(by[(f, m)]["seeds_recall_ci_lower_below_zero"] for f in folds for m in MODELS)
    paired = sum(by[(f, m)]["paired_seeds"] for f in folds for m in MODELS)
    lines += [
        "",
        f"Разница по полноте со списком (среднее seed) — от {signed(min(deltas), 2)} до",
        f"{signed(max(deltas), 2)} п. п.; лидер меняется между периодами. Парный недельный",
        "bootstrap (1 000 повторов, одни недели для обоих методов): нижняя граница 95%",
        f"интервала ΔR ниже нуля в {lower} из {paired} прогонов на обоих периодах.",
        "",
        "| Метод | "
        + " | ".join(f"ΔR, п. п. ({PERIODS[f]})" for f in folds)
        + " | 95% ΔR по seed, п. п. |",
        "|---|" + "---:|" * len(folds) + "---|",
    ]
    for model in MODELS:
        cells = [signed(by[(f, model)]["delta_recall_pp"], 2) for f in folds]
        ci = "; ".join(
            f"{PERIODS[f]}: {signed(100 * by[(f, model)]['recall_delta_95_lower_min'])} … "
            f"{signed(100 * by[(f, model)]['recall_delta_95_upper_max'])}"
            for f in folds
        )
        lines.append(f"| {NAMES[model]} | " + " | ".join(cells) + f" | {ci} |")
    lines += [
        "",
        "Интервал в последнем столбце — самая низкая нижняя и самая высокая верхняя граница",
        "среди трёх seed; по каждому seed — в JSON (`runs[].paired_vs_static`).",
    ]
    recon = report["baseline_reconciliation"]
    if recon:
        late = next(f for f in recon["folds"] if f["fold"] == "viewed2025_2026")
        v9, seq = late["v9_protocol"], late["sequence_protocol"]
        lines += [
            "",
            "## Почему список здесь не 0,773 / 0,646",
            "",
            "Регресса нет — отличается протокол. Оценки списка совпадают",
            f"(`score_disagreement = {late['score_disagreement']}`). Сверка тем же кодом",
            "([отчёт эпизодов](registered-episodes-2026-09-29.json), `baseline_reconciliation`):",
            f"протокол v9 даёт {num(v9['card_precision'], 4)} / {num(v9['R_incident'], 4)},",
            f"протокол последовательностей — {num(seq['card_precision'], 4)} / "
            f"{num(seq['incident_recall'], 4)}.",
            "",
            "- допустимость строже: год известной истории объекта, покрытый предыдущий день и",
            "  ≥ 1 событие за 365 суток; расхождений допустимости "
            f"{late['eligible_disagreement']};",
            f"- выдача — карточек {seq['cards_issued']} против {v9['cards_issued']}; общих "
            f"{late['issued_shared']}, только v9 — {late['issued_v9_only']}, только здесь — "
            f"{late['issued_sequence_only']};",
            "- при неполном покрытии будущего окна исход unknown даже при видимом событии;",
            "- знаменатель полноты — покрытые головы инцидентов, включая недопустимые пары:",
            f"  {seq['incident_heads']} против {v9['incidents']}; поймано "
            f"{seq['incident_heads_caught']} против {v9['captured_incidents']};",
            "- фолд начинается с пустой очереди, dev-фолд — с 07.2024, а не с января.",
        ]
    lines += [
        "",
        "## Подготовленный набор",
        "",
        f"{prepared['objects']} объектов, {grouped(prepared['rows'])} срезов "
        f"({prepared['start']} … {prepared['end']}), допустимых {grouped(prepared['eligible'])},",
        f"unknown {prepared['unknown']}. В {grouped(prepared['truncated'])} из "
        f"{grouped(prepared['rows'])} срезов ({num(100 * prepared['truncated_share'], 0)}%)"
        " 128 сообщений покрывают меньше",
        "90 суток: «90-дневная история» у сетей — только верхняя граница. Доступность",
        "имитируется временем источника; справочник текущий, без дат действия.",
        "",
        "## Воспроизводимость",
        "",
        f"Прогон выполнен на рабочем дереве поверх `{identity['git_sha'][:7]}` с",
        f"`git_dirty: {str(identity['git_dirty']).lower()}`: кода исследования тогда не было в",
        "коммите. Хеши SHA-256 файлов кода из `run.json` сравнены с репозиторием:",
        "",
        "| Файл | " + " | ".join(f"`{r}`" for r in revs) + " | Это дерево |",
        "|---|" + "---|" * (len(revs) + 1),
    ]

    def mark(value):
        return "нет в ревизии" if value is None else ("совпадает" if value else "**не совпадает**")

    for row in report["code_hashes"]:
        cells = [mark(row[f"matches_{r}"]) for r in revs] + [mark(row["matches_checkout"])]
        lines.append(f"| `{row['path'].split('/src/')[-1]}` | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Для `sequence/models.py` и `sequence/training.py` версии кода, на которой шёл прогон,",
        "в Git нет: из коммита эти числа точно не воспроизводятся. Проверить можно только",
        "повтором `train` той же конфигурацией в новый `--output`; это ещё одно чтение",
        "отложенного периода (без нового выбора). `incident_list.py` в релизе 29.09 дополнен",
        "рекомендацией v5 — функции выдачи не менялись, но хеш другой, и `--resume` старых",
        "checkpoint не примет. `uv.lock` прогона (`"
        + identity["lock_sha256"][:12]
        + "…`) "
        + "; ".join(
            f"{'совпадает' if report['lock_matches'][r] else 'не совпадает'} с `{r}`" for r in revs
        )
        + ("; " if revs else "")
        + (
            "совпадает в этом дереве"
            if report["lock_matches"]["checkout"]
            else "в этом дереве отличается — релиз 29.09 добавил `fastapi-swagger`"
        )
        + " (torch в прогоне "
        + identity["packages"]["torch"]
        + ", в lock дерева "
        + str(report["lock_matches"].get("checkout_torch"))
        + ").",
        f"Подготовка: manifest `{report['source']['prepared_manifest_sha256'][:12]}…`, snapshot",
        f"`{prepared['snapshot_manifest_sha256'][:12]}…`, overlay "
        f"`{prepared['overlay_manifest_sha256'][:12]}…`.",
        "",
        "## Ограничения",
        "",
        "- интервалы разницы со списком включают 0; bootstrap по объектам и абляции — planned;",
        "- объекты во времени те же, перенос на новые объекты не проверялся;",
        "- оценки не откалиброваны, AP / ROC AUC / Brier в JSON — не вероятности;",
        "- свежих инцидентов мало (десятки голов за период), разница по ним — несколько событий;",
        "- CUDA, MPS и Linux-запуск не проверялись, всё обучение — CPU.",
        "",
        "Пересборка отчёта (нужны локальные ignored `run.json` и `manifest.json`):",
        "",
        "```sh",
        "uv run --locked --group train python \\",
        "  data-science/scripts/summarize_sequence_research.py \\",
        "  " + " ".join(f"--compare-rev {r}" for r in revs),
        "```",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=ARTIFACTS / "run/run.json")
    parser.add_argument("--manifest", type=Path, default=ARTIFACTS / "prepared/manifest.json")
    parser.add_argument(
        "--config", type=Path, default=ROOT / "data-science/configs/sequence_research_v1.json"
    )
    parser.add_argument(
        "--reconciliation", type=Path, default=REPORTS / "registered-episodes-2026-09-29.json"
    )
    parser.add_argument("--output", type=Path, default=REPORTS / "phase-sequence-2026-09-28.json")
    parser.add_argument("--compare-rev", action="append", default=[])
    args = parser.parse_args()
    summarize(
        args.run, args.manifest, args.config, args.reconciliation, args.output, args.compare_rev
    )


if __name__ == "__main__":
    main()
