# Исследовательский контур InfraPulse

Офлайн-часть проекта: ETL журнала в Parquet, метки «Отказа датчика», версии
исследования v1–v11 и агрегированные отчёты. HTTP runtime этот пакет не импортирует;
прогноз в продукте считает `packages/core`. Данные организаторов, Parquet, веса и прогоны
в репозиторий не входят: публикуются код, замороженные конфигурации, команды
воспроизведения и агрегированные отчёты без идентификаторов.

## Итоговая модель в продукте (v9)

**Постановка.** Конфигурация `prod_phase`: будет ли в ближайшие 14 суток на объекте ДУ
зарегистрирована потеря связи («Неисправен») линии электропитания — канала «Состояния фазы».
Карточка — пара «объект × тип датчика», 19 объектов. Метка — зарегистрированное событие, все
эпизоды без порога длительности, эпизоды объекта с разрывом между началами до 10 мин — одно
событие; это не подтверждённая поломка. В интерфейсе — «Обесточивание электрооборудования
объекта»: в 8 из 10 событий линия затем «Обесточен». Определения —
[EVALUATION](experiments/sensor-failure/EVALUATION.md),
[LABELS](experiments/sensor-failure/LABELS.md).

**Метод.** Статичный список без обучения: оценка пары — число событий за 365 суток до
среза; каждые сутки в 00:00 МСК список дополняется до 10 открытых карточек, карточка
снимается в момент события. Та же логика — в `packages/core/.../features/incident_list.py`,
его выполняет worker стенда; сверка core с исследованием — 0 расхождений
([отчёт](reports/core-research-parity-v9-2026-09-27.json)). ML-модели v1–v10 и последовательные
сети 28–29.09 (GRU, LSTM, TCN, Transformer Encoder) список не превзошли
([FEATURES_TRIED](experiments/sensor-failure/FEATURES_TRIED.md)).

**Данные и валидация.** Журнал СМВУ организаторов 2019–06.2026 (curated-снимок и окна
`alarm-spike-exclusions-v1`, ниже). Выбор — только на фолдах разработки 2023 и 2024 по
заранее записанному правилу; конфигурация и код заморожены с SHA-256 до чтения отложенного
периода 07.2025–06.2026; сухой прогон кода отложенного периода на фолдах совпал 494 из 494.
Интервалы — bootstrap по неделям и по объектам. Точность — по карточкам с известным исходом;
основная полнота — по инцидентам (серия событий с паузами меньше 24 ч), `R_strict` — для
справки. Утечки проверены отдельно ([LEAKAGE_AUDIT](experiments/sensor-failure/LEAKAGE_AUDIT.md)).

**Результат на отложенном периоде** (10 карточек, [FINAL_V9](experiments/sensor-failure/FINAL_V9.md),
[отчёт](reports/sensor-failure-final-v9-2026-09-27.json)): P 0,773 [0,728; 0,815],
`R_incident` 0,646 [0,604; 0,689], `R_strict` 0,567 [0,496; 0,630]; по объектам нижняя граница
P 0,662. Целевые показатели проекта P ≥ 0,7 и R ≥ 0,5 выполнены по нижним границам по неделям;
в ТЗ этих чисел нет — §9 оставляет их на этап проектирования, 0,7 и 0,5 организаторы назвали
плановыми по умолчанию. Справочно, после заморозки: persistence — P 0,763, `R_incident` 0,554;
случайный список — 0,54 и 0,39; прирост над случайным по P +0,21 / +0,24 / +0,23 и по
`R_incident` +0,28 / +0,29 / +0,26 (2023 / 2024 / отложенный).

**Оговорки.** Отложенный период использован для метрик пять раз, шестой — только для
калибровки «k из n» ([PROBABILITY_V11](experiments/sensor-failure/PROBABILITY_V11.md));
седьмое и восьмое — диагностические исследования
[последовательностей](experiments/sequence/README.md) и
[эпизодов](experiments/episodes/README.md) 28–29.09, без выбора для продукта. Числа v9 они
не меняют.
`R_incident` и снятие карточки в момент события приняты 27.09, после третьего и четвёртого
использования ([журнал решений](experiments/sensor-failure/DECISION_LOG.md)). По точности
список равен persistence; добавка — новые инциденты (0,72 против 0,11). «Оценка вероятности»
в карточке — доля сбывшихся карточек уровня за 2023–2024: уровней три, на отложенном периоде
проверены две группы (≈55–75% и ≈85%). Модель интенсивности v11 калибровку не прошла и в
продукт не вошла.

**Как воспроизвести.** Нужны выгрузка организаторов в `data/raw/` ([DATA](../docs/DATA.md)) и
curated-снимок `artifacts/curated-all-sensors-exclude-2021-q2` с окнами
`artifacts/alarm-spike-exclusions-v1` (команды — ниже в этом файле). Из корня, префикс
`uv run --locked --group train --group research`:

```sh
python data-science/scripts/measure_sensor_failure.py                  # таблицы эпизодов
python data-science/scripts/measure_sensor_failure.py --output data-science/artifacts/sensor-failure-v1 \
  --tables data-science/artifacts/sensor-failure-v1/tables --model-labels \
  --model-config configs/sensor_failure_model_v5.json --labels-dir model_labels_v5 \
  --label-months development                                           # метки 7 сут
python data-science/scripts/measure_sensor_failure.py --model-labels --label-months development \
  --model-config configs/sensor_failure_v5_exploration5_labels.json \
  --output data-science/artifacts/next-2026-09-27/ds-exploration5 --labels-dir labels \
  --tables data-science/artifacts/sensor-failure-v1/tables             # метки 14 и 21 сут
python data-science/scripts/final_sensor_failure_v9.py --stage dev     # фолды 2023/2024
python data-science/scripts/final_sensor_failure_v9.py --stage holdout --dry-run
# Только по отдельному решению: метки отложенного периода, заморозка и один прогон.
python data-science/scripts/measure_sensor_failure.py --output data-science/artifacts/sensor-failure-v1 \
  --tables data-science/artifacts/sensor-failure-v1/tables --model-labels \
  --model-config configs/sensor_failure_model_v5_holdout.json --labels-dir model_labels_v5_holdout \
  --label-months all
python data-science/scripts/final_sensor_failure_v9.py --stage freeze
python data-science/scripts/final_sensor_failure_v9.py --stage holdout
python data-science/scripts/posthoc_sensor_failure_v9_baselines.py    # ориентиры, после заморозки
python data-science/scripts/phase_probability_v11.py                  # калибровка «k из n»
python -m pytest data-science/tests/test_sensor_failure_release_v9.py \
  data-science/tests/test_core_research_parity.py tests/test_core_c03_rules.py
```

Выходы без идентификаторов — в ignored `artifacts/next-2026-09-27/v9` и `…/v11`,
агрегированные отчёты — в `reports/`. Повторный прогон на отложенном периоде блокирует запись
`holdout_finished` в `artifacts/next-2026-09-27/v9/freeze.log`; журнал лежит вне Git, поэтому
в свежем клоне эта защита не действует. Отложенный период исчерпан: новая независимая проверка
возможна только на данных после 06.2026. Команды выше в этом разделе не перезапускались
29.09; время этапов и входы — в разделе «Воспроизведение» FINAL_V9 (там же вызов через
`.venv/bin/python` с `PYTHONPATH`).

## Последовательности фазы: локальные эксперименты

[Код, протокол и запуск GRU/LSTM/TCN/Transformer Encoder](experiments/sequence/README.md).
Отдельная группа `sequence` (PyTorch, в стандартный `make check` и CI не входит),
синтетический smoke и локальное MLflow logging. Реальное обучение 28.09: 32 оценки,
прирост над списком не подтверждён ([отчёт](reports/phase-sequence-2026-09-28.md)).
Протокол строже v9, числа с v9 не сравниваются; действующий список v9 не меняется.

## Зарегистрированные эпизоды после ответов заказчика — 29.09.2026

[Протокол и запуск](experiments/episodes/README.md): таргеты A/B, фаза отдельно,
суточная история 90 дней, новые эпизоды в P и R, сравнение CatBoost/TCN/Transformer
с частотой и недавними событиями. Реальное обучение 29.09: 264 оценки, согласованный
критерий преимущества не выполнен ни одной моделью
([отчёт](reports/registered-episodes-2026-09-29.md)). Все запуски локальные; числа
с v9 и с последовательностями не сравниваются; текущий продукт v9 сохраняется.

## Окружение

```sh
uv sync --locked --group train --group research
uv run --locked --group train --group research python -m pytest data-science/tests
```

Данные, Parquet, веса, прогоны MLflow и локальные отчёты — только в игнорируемых
`data/`, `artifacts/`, `reports/local/`, `mlflow/store/` ([данные](data/README.md),
[артефакты](artifacts/README.md)). `make mlflow` / `make mlflow-smoke` запускают локальный
tracking ([MLflow](mlflow/README.md)); API он не нужен. LightGBM и XGBoost (группа
`legacy-boosting`) нужны только для повтора сравнения v6.

## Слой данных под v9

Все команды — из корня, префикс `uv run --locked --group train --group research`
(или `.venv/bin/python`). Сырьё — read-only каталог `data-science/data/raw`
([DATA](../docs/DATA.md)).

```sh
python data-science/scripts/prepare_maintenance.py \
  --ppr 'data-science/data/raw/maintenance/<график ППР АКМ 2026>.xlsx' \
  --to 'data-science/data/raw/maintenance/<график ТО АКМ и ДУ 2026>.xlsx' \
  --output data-science/artifacts/maintenance-2026-planned
python data-science/scripts/curate_journal.py --source data-science/data/raw \
  --output data-science/artifacts/curated-all-sensors-exclude-2021-q2 \
  --years 2019 2020 2021 2022 2023 2024 2025 2026 --policy exclude-2021-apr-jun-v1 \
  --maintenance-manifest data-science/artifacts/maintenance-2026-planned/manifest.json \
  --ingested-at 2026-09-25T00:00:00Z
python data-science/scripts/audit_prepared.py \
  --snapshot data-science/artifacts/curated-all-sensors-exclude-2021-q2 \
  --output data-science/artifacts/prepared-quality-audit.json \
  --summary-output data-science/reports/prepared-data-2026-09-25.json
python data-science/scripts/prepare_state_candidates.py \
  --source data-science/data/raw/справочник_состояний.csv \
  --output data-science/artifacts/state-candidates-v1
python data-science/scripts/apply_alarm_spike_policy.py \
  --snapshot data-science/artifacts/curated-all-sensors-exclude-2021-q2 \
  --output data-science/artifacts/alarm-spike-exclusions-v1
```

Проверки слоя: `verify_prepared_repeat.py` (повтор 453/453 SHA-256,
[отчёт](reports/prepared-repeat-2026-09-25.json)), `audit_alarm_spike_policy.py` и
`verify_alarm_spike_repeat.py` ([overlay](reports/alarm-spike-policy-2026-09-25.json)),
`plot_prepared_quality.py` (график месячных долей `alarm`), `audit_state_reference.py`,
`verify_source_parquet.py`, `verify_curated_repro.py` (DATA-03,
[протокол ETL](experiments/curated-etl/README.md)). Ленивый просмотр событий —
`infra_pulse_research.data.prepared.open_prepared(snapshot, policy_manifest=...)`,
обзор всех восьми лет — [08_MY_EDA.ipynb](notebooks/08_MY_EDA.ipynb).

## Версии и где их код

Путь к решению, ошибки и отрицательные результаты — [журнал решений](experiments/sensor-failure/DECISION_LOG.md);
версии и отчёты — [«Отказ датчика»](experiments/sensor-failure/README.md) и
[индекс отчётов](reports/README.md).

`final_sensor_failure_v9.py` загружает функции из `tune_sensor_failure_v6.py`
(Байес пары) и `explore_sensor_failure_v5_part2.py` (затухание), те — из
`explore_sensor_failure_v5.py` и `tune_sensor_failure_v5.py`; поэтому скрипты v5/v6
входят в репозиторий. Метки 14/21 сут фазы dev v9 строит `measure_sensor_failure.py` с
`configs/sensor_failure_v5_exploration5_labels.json`; разведку 5 на них выполняет
`explore_sensor_failure_v5_exploration5.py`. `phase_catboost_compact_v10.py` и
`phase_probability_v11.py` — последние отрицательные результаты ML, `check_core_parity_v9.py` —
сверка core с исследованием.

Код раннеров v1–v4, v7, v8 и проверки списка v5 на отложенном периоде в публичный
репозиторий не входит: их выводы отрицательные и зафиксированы отчётами `reports/` и
замороженными предрегистрациями `configs/sensor_failure_*`; имена скриптов в документах
версий описывают, что было запущено. Признаки v4 и v7 (`sensor_failure_fe_v4/v7`) и их
leakage-тесты остаются.

## Состав

```text
src/infra_pulse_research/
  data/prepared.py, data/audit.py      слой v4, аудит источников
  modeling/fire_source.py              рабочее представление журнала + overlay
  modeling/sensor_failure_*.py         метки, оценка, release/recall, маска ТО, признаки v4/v7
  modeling/sensor_technical_values.py  технические значения каналов
  modeling/subsystem_*.py              общая сетка cutoff, группы и интервалы
  modeling/target_audit.py             интервалы Уилсона
  sensor_failure_maintenance.py        сессии ТО пожарных извещателей
  sequence/, episodes/                 исследования 28–29.09: сети и эпизоды (группа sequence)
scripts/                               CLI слоя данных, меток и версий v5, v6, v9–v11
configs/                               метки и замороженные предрегистрации v1–v9
experiments/sensor-failure/            протоколы, результаты, журнал решений
experiments/curated-etl/               протокол ETL и проверки DATA-03
experiments/sequence/, episodes/       протоколы исследований 28–29.09 (диагностика)
reports/                               агрегаты без идентификаторов ([индекс](reports/README.md))
notebooks/08_MY_EDA.ipynb              обзор восьми лет (без outputs)
mlflow/                                локальный tracking ([README](mlflow/README.md))
tests/                                 синтетические тесты данных, меток, cutoff и leakage
sequence-tests/                        тесты PyTorch, только make check-sequence
```

`packages/core` не импортирует research, training и MLflow; `backend/` зависит от core
contracts, а не от этого пакета. Общий `make check` проверяет также backend и границы
пакетов. В корне один `uv.lock`; отдельная `.venv` внутри data-science не нужна.
