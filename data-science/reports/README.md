# Агрегированные результаты проверок

Только агрегаты: без идентификаторов объектов и каналов, без строк журнала.
Где отчёт это фиксирует, в нём есть хеши источника, конфигурации и кода, Git SHA и
признак dirty. Новые локальные запуски по умолчанию пишутся в `data-science/reports/local/`
(вне Git).

## «Отказ датчика» v1–v11

| Версия | Отчёты | Документ |
| --- | --- | --- |
| v1 — метки и первая модель | `sensor-failure-measurement-2026-09-26.json`, `sensor-failure-model-2026-09-26.json` | [README](../experiments/sensor-failure/README.md), [LABELS](../experiments/sensor-failure/LABELS.md) |
| v2 — подбор, > 2 с | `sensor-failure-tuning-v2-2026-09-26.json` | [TUNING_V2](../experiments/sensor-failure/TUNING_V2.md) |
| v3 — выдача 06:00, бюджет, список | `sensor-failure-v3-prep-2026-09-26.json`, `sensor-failure-tuning-v3-cv-2026-09-26.json`, `sensor-failure-v3-d-feasibility-2026-09-26.json` | [TUNING_V3](../experiments/sensor-failure/TUNING_V3.md), [V3_BRANCHES](../experiments/sensor-failure/V3_BRANCHES.md) |
| v4 — узлы, счётчики, 120 ч | `sensor-failure-v4-prep-2026-09-26.json`, `sensor-failure-v4-i2-precursors-2026-09-26.json` | [TUNING_V4](../experiments/sensor-failure/TUNING_V4.md) |
| v5 — 10/14 сут, разведки 1–5, третий отложенный | `sensor-failure-v5-stage0-2026-09-26.json`, `sensor-failure-tuning-v5-cv-2026-09-26.json`, `sensor-failure-v5-exploration*-2026-09-2*.json` (5), `sensor-failure-holdout-list-v5-2026-09-27.json` | [TUNING_V5](../experiments/sensor-failure/TUNING_V5.md) |
| v6 — семейства моделей | `sensor-failure-models-v6-2026-09-27.json` | [TUNING_V6](../experiments/sensor-failure/TUNING_V6.md) |
| v7 — признаки пары | — (результаты в документе) | [FEATURES_V7](../experiments/sensor-failure/FEATURES_V7.md) |
| v8 — четвёртый отложенный | `sensor-failure-final-models-v8-2026-09-27.json` | [FINAL_V8](../experiments/sensor-failure/FINAL_V8.md) |
| **v9 — прод-список, пятый отложенный** | `sensor-failure-final-v9-2026-09-27.json`, `core-research-parity-v9-2026-09-27.json` | [FINAL_V9](../experiments/sensor-failure/FINAL_V9.md) |
| v10 — компактный CatBoost на фазе | `sensor-failure-phase-catboost-v10-2026-09-27.json` | [FEATURES_TRIED](../experiments/sensor-failure/FEATURES_TRIED.md) |
| v11 — вероятностный слой | `sensor-failure-phase-probability-v11-2026-09-27.json` | [PROBABILITY_V11](../experiments/sensor-failure/PROBABILITY_V11.md) |

`core-research-parity-v9-2026-09-27.json` сверяет core-детектор и прод-список B2 с
research-кодом v9 на слое curated v4 и `alarm-spike-exclusions-v1`: эпизоды фазы
2019–2026 11 058 = 11 058 (расхождений начал и концов — 0), события 2 947 = 2 947; на фолде
2024 совпали score, открытые наборы за все 353 дня и 509 = 509 карточек. Названия линий,
попавшие в «прочее» словаря `phase-feeder-kinds-v1`, даны основами с замаскированными
числами, без ID; в публичной версии две основы с топонимами заменены на `<место 1>` и
`<место 2>`. Скрипт — `scripts/check_core_parity_v9.py`, синтетические тесты —
`tests/test_core_research_parity.py`.

## Локальные исследования 28–29.09 (диагностика, v9 не меняют)

| Отчёт | Документ |
| --- | --- |
| `phase-sequence-2026-09-28.json`, [`.md`](phase-sequence-2026-09-28.md) — последовательные сети и CatBoost против списка, фаза, 14 сут | [протокол](../experiments/sequence/README.md) |
| `registered-episodes-2026-09-29.json`, [`.md`](registered-episodes-2026-09-29.md) — новые эпизоды A/B и фаза, CatBoost/TCN/Transformer против частоты | [протокол](../experiments/episodes/README.md) |

Числа этих отчётов с v9 и между собой не сравниваются: другие таргет, единица карточки и
знаменатели. В публичной версии `phase-sequence-2026-09-28.json` ключи сверки с коммитом
исследования названы `matches_research_commit_2026_09_29` (в исходном отчёте — по короткому
SHA коммита).

## Слой данных

| Отчёт | Что подтверждает |
| --- | --- |
| `curated-etl-2026-09-23.json`, `curated-repro-2026-09-23.json`, `source-parquet-verification-2026-09-23.json` | DATA-03: сверки ETL, повтор 393/393, сверка 7z и Parquet ([протокол](../experiments/curated-etl/README.md)) |
| `prepared-data-2026-09-25.json`, `prepared-repeat-2026-09-25.json` | слой v4 восьми лет и повтор 453/453; имена двух XLSX графиков ТО и ППР заменены на `<архив 12>`, `<архив 13>`, SHA-256 сохранены |
| `alarm-spike-policy-2026-09-25.json`, `alarm-spike-repeat-2026-09-25.json` | overlay `alarm-spike-exclusions-v1` и его повтор |
| `early-alarm-threshold-2026-09-25.json`, `blind-holdout-sizing-2026-09-25.json` | E22 и E23 ([реестр решений](../../docs/REQUIREMENTS_DECISIONS.md)) |
| `state-reference-audit-2026-09-23.json` | DATA-04: справочник состояний; сам словарь и исходные лексемы в отчёт не копируются |
| `data-profile-2026.json` | профиль источников (`scripts/profile_data.py`): размеры и SHA-256 файлов, агрегаты; без исходных рядов и локальных путей. В публичной версии имена исходных файлов организаторов заменены на `<архив 1>`…`<архив 11>` (1–8 — журналы 2019–2026, 9 — журнал-пример, 10 — справочник каналов, 11 — справочник объектов) |
| `dispatch-signal-inventory-2026-09-24.json` | инвентарь лексем, на котором основаны правила колокольчика (`frontend/src/shared/config/notifications.ts`) |

Отчёты исследований до выбора «Отказа датчика» (EDA и когорты, газовая постановка,
дымовой baseline, alarm подсистемы) в публичный репозиторий не входят.
