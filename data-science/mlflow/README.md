# Локальный MLflow

Запуск из корня репозитория: `make mlflow` (цель `Makefile` запускает
`data-science/mlflow/server.py`, адрес из `config.toml` — http://127.0.0.1:5000); проверка —
`make mlflow-smoke`. Итоговая модель v9 — статичный список без обучения; её числа — в
[отчёте v9](../reports/sensor-failure-final-v9-2026-09-27.json) и
[FINAL_V9](../experiments/sensor-failure/FINAL_V9.md).

## Последовательности фазы (28.09.2026)

Новый [sequence runner](../experiments/sequence/README.md) использует этот launcher:
`python -m infra_pulse_research.sequence train --tracking-uri http://127.0.0.1:5000`
через `uv run --locked --group sequence`. Runs вложены: исследование → fold → модель/seed.
В MLflow попадают config/data/code hashes, seed, агрегированные метрики и отчёт;
raw ID, датасеты, словарь состояний и веса остаются в локальных ignored artifacts.
Без `--tracking-uri` сохраняется тот же агрегированный `run.json`. Это отдельный
исследовательский runner; исторические trainers и HTTP runtime не изменены.

Проверено 28.09 на CPU/синтетике: 7 вложенных runs FINISHED на временном
loopback5078, root aggregate скачан и совпал с локальным `run.json`. Временный
store — ignored `artifacts/sequence-v1/mlflow-smoke-store`; рабочий store не менялся.
Реальный прогон 28.09 записан только в локальный store; передаваемый
результат — [агрегированный отчёт](../reports/phase-sequence-2026-09-28.md), run ID
без доступного сервера его не заменяет.

MLflow входит в dependency group `train`; отдельного окружения/requirements нет.
Server metadata и artifacts лежат в игнорируемом `store/`, рядом с config,
независимо от cwd. SQLite — для одного локального владельца; не запускать несколько
серверов с одним store. Общий tracking server — отдельная будущая задача.

```sh
uv sync --locked --group train --group research
uv run --locked --group train python data-science/mlflow/server.py
```

UI: `http://127.0.0.1:5000`; остановка Ctrl+C. Launcher разрешает только loopback.
При занятом порте: `--port 5059`; smoke тогда запускается с
`--tracking-uri http://127.0.0.1:5059`. Изменения `config.toml` читаются при старте.
`--print-command` показывает команду без запуска/создания БД.
`--store-dir /absolute/path` задаёт другое локальное хранилище для smoke/резервной копии.

В отдельном терминале:

```sh
uv run --locked --group train python data-science/mlflow/smoke.py
```

Проверка создаёт `infra-connectivity-smoke`: параметр, числовой marker, JSON artifact,
чтение сохранённого run и скачивание artifact. Все значения synthetic, models_fitted=0.
Это не метрики модели. Для повторов используются новые run ID.

```text
mlflow/config.toml        конфигурация в Git
mlflow/server.py          launcher в Git
mlflow/smoke.py           воспроизводимая connectivity проверка в Git
mlflow/store/mlflow.db    локальная SQLite metadata, вне Git
mlflow/store/artifacts/   artifacts runs, вне Git
../data/parquet/          датасеты отдельно, не MLflow artifact store
```

Состав evidence эксперимента — [ML-06/ML-07](../../docs/REQUIREMENTS_SPEC.md#ml--постановка-и-допуск)
и [формат передачи](../../CONTRIBUTING.md#как-фиксируется-результат).
При подключении trainer к tracking также сохраняются lock hash, seed, периоды
и coverage version: текущий launcher сам эти поля эксперимента не заполняет.
MLflow хранит ссылки/hashes входного snapshot и агрегаты;
приватные raw IDs, секреты и весь dataset в artifacts не загружаются.

E0–E5 run готовит локальный `mlflow-handoff.json`: dataset/config/feature/target
versions и hashes, quality/censoring aggregates и имена локальных manifests.
Это передача для будущего E6 logging, не созданный MLflow run. `feature-batch.parquet`,
`label-table.parquet` и combined cutoff dataset в store не переносятся.
Final4 manifest также фиксирует current events/checkpoint hashes, новый builder/
runner code identity и schema hashes. Handoff остаётся candidate/no-go: реальные
MLflow run, datasets и model metrics не создавались.

**Текущий исторический trainer не логирует MLflow автоматически.** Launcher и
smoke проверяют работающий tracking, но не registry promotion или production
exporter. Новые эксперименты могут использовать `mlflow.set_tracking_uri(...)`
явно; существующий baseline не изменяет результаты из-за появления сервера.
Inference использует фиксированный bundle и работает без доступа к этому MLflow.

Проверено 21.09.2026 на текущем Mac через штатный `uv`/Python3.12.11:
launcher поднял сервер на loopback5059 с отдельным временным SQLite store;
`smoke.py` записал/прочитал run и скачал JSON с совпадающим payload. Обучения и
чтения реального dataset не было. Временный сервер остановлен. Порт5000 остаётся
стандартной конфигурацией команды; он отдельно не запускался этой проверкой.

## Новое исследование эпизодов

`registered-episode-research-v1` — отдельный эксперимент существующего сервера
`http://127.0.0.1:5059`. [Команды и протокол](../experiments/episodes/README.md).
Метки и веса остаются локально; записываются агрегаты, версии и обезличенные блоки
объектов. Эксперимент `phase-sequence-research-v1` сохраняется. Значения AP,
card_precision и incident_recall не взаимозаменяемы. Для 24 ч используется
фиксированное ранжирование модели 14 суток, отдельного обучения на 24 ч нет.
Результат 29.09 — [агрегированный отчёт](../reports/registered-episodes-2026-09-29.md);
локальный run ID в нём не означает доступности store команде.
