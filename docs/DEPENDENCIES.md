# Окружения и зависимости

Python 3.12, `uv` (проверяемая стартовая версия 0.8.4), uv workspace с одной `.venv` в корне и отдельными Python-пакетами компонентов.
Node 24 LTS, npm lockfile отдельно для frontend. GPU не нужен для baseline.
Точные транзитивные версии и хэши фиксирует `uv.lock`, frontend — `package-lock.json`.

Установка инструментов и clone — [README](../README.md#установка); порты, режимы и признаки
успешного старта — [OPERATIONS](OPERATIONS.md#локальный-старт).
Разработка — macOS/Linux; CI и deployment — Linux. `uv.lock` общий, `.venv` на каждой ОС своя;
для архивов дополнительно нужен системный `bsdtar`, не Python dependency.

## Установка по части проекта

```sh
# Минимум: API + dev проверки
uv sync --locked

# Backend с PostgreSQL-тестами и исследование (полная проверка, make check)
uv sync --locked --group platform --group train --group research

# Исследование: ETL, метки, MLflow
uv sync --locked --group train --group research

# Frontend; для локального fixture API дополнительно uv sync --locked
cd frontend
npm ci
```

Установщик uv — [официальная инструкция](https://docs.astral.sh/uv/getting-started/installation/);
версия 0.8.4 закреплена для окружения и CI, это не заявление о latest. Python 3.12 ставит
`uv python install 3.12`, отдельная установка Python не нужна. `frontend/.nvmrc` задаёт major
Node (24), но сам Node не устанавливает. Чужие `.venv` и `node_modules` не копируются:
`.venv` создаётся на каждой машине и в каждом клоне заново. Кэш пакетов по `uv.toml` — в
`.uv-cache` внутри клона; `UV_CACHE_DIR` переносит его в общий каталог. Полное окружение
(`platform`, `train`, `research`) — около 1,8 ГБ в `.venv` и `.uv-cache`, первая установка
с пустым кэшем — 15–20 мин (проверка 29.09 на macOS arm64: 17 мин); для API достаточно
`uv sync --locked`.

Проверка окружения из корня, без активации:

```sh
uv run --locked python -c "import sys; print(sys.version); print(sys.executable)"  # 3.12 из .venv
uv run --locked ruff check .
make check     # полный набор: Ruff, зависимости ML и PostgreSQL, pytest
```

Минимальное окружение (`uv sync --locked`) пропускает необязательные ML-тесты — это не
полная проверка. IDE использует `.venv/bin/python`; кодировка UTF-8 и перевод строки LF
заданы `.gitattributes`. `.env` для локального запуска не нужен, пример —
[`.env.example`](../.env.example); секреты в Git не добавляются.

Активация `source .venv/bin/activate` необязательна: `uv run` использует `.venv`.
`uv sync` приводит окружение к выбранному набору, поэтому повторный sync с меньшим
набором может убрать ранее установленные группы. Повторяйте нужные `--group` при
sync. Обычный `uv run` в проверенной версии не удаляет лишние пакеты (в отличие от
`uv run --exact`); `uv run --group train …` также установит train при необходимости.
Для запуска строго из уже подготовленного окружения: `uv run --no-sync …` или
`.venv/bin/python`.

| Группа | Прямые зависимости | Назначение |
| --- | --- | --- |
| root base → backend | infra-pulse-backend → infra-pulse-core, FastAPI/Uvicorn/settings, psycopg, python-multipart, ldap3, openpyxl, defusedxml (пачки XML без DTD и сущностей), fastapi-swagger 0.4.60 (только файлы Swagger UI для `/api/docs`, без CDN) | HTTP и локальное чтение PostgreSQL replay без research/training |
| dev | pytest, httpx, Ruff | Tests и lint/format |
| data | DuckDB, PyArrow | Out-of-core SQL, Parquet |
| platform | SQLAlchemy, Alembic, psycopg | Дополнительные platform зависимости; текущие replay SQL-миграции запускает отдельный loader |
| inference | core[features] (NumPy/Pandas) + CatBoost | Библиотеки будущего worker, не готовый worker |
| train | infra-pulse-research → core[features], DuckDB/PyArrow, NumPy, sklearn/CatBoost, MLflow | Локальный ETL, labels/train/evaluate |
| research | JupyterLab, Matplotlib | Ноутбуки и графики |
| sequence | infra-pulse-research + PyTorch (`torch>=2.6,<3`, в lock 2.14.0) | Только локальные исследования последовательностей и эпизодов; вне CI, Docker и `default-groups` |

Локальные скрипты загрузки и полной сверки replay с Parquet запускаются с
`--group platform --group data`. Минимальная установка API и её CI job не
устанавливают DuckDB; он загружается только при полной сверке Parquet.

22.09: NumPy объявлен прямой зависимостью `infra-pulse-research` — прежде он
приходил транзитивно через PyArrow/scikit-learn, а блочный бутстрэп аудита
импортирует его напрямую. Новых пакетов не добавлено, `uv.lock` изменился на две
строки.

Pandas не предназначен для загрузки всех архивов сразу. SHAP, TensorFlow, Airflow,
Celery, Redis и CUDA сейчас не устанавливаем; PyTorch — только в локальной группе
`sequence` (раздел ниже). LightGBM и XGBoost (сравнения
v6/v7) с 27.09 — в отдельной группе `legacy-boosting`: продукт и итоговый список v9 их
не используют, CI их не ставит (на Linux xgboost тянет `nvidia-nccl-cu13`, около 305 МБ).
Объяснения CatBoost доступны через native feature contributions; отдельный пакет
добавляется только если нужен выбранному методу.

## MLflow

MLflow уже входит в группу `train`, JupyterLab/Matplotlib — в `research`;
дополнительный requirements.txt не нужен. [Launcher, config и smoke](../data-science/mlflow/README.md)
используют локальную SQLite и artifacts под `data-science/mlflow/store/`.
Parquet находится отдельно: `data-science/data/parquet/`.

Trainer должен явно указывать tracking URI своего сервера. У каждого DS собственный
store; синхронизировать активную SQLite через Git/Drive нельзя. Код v1 пока не
логирует runs автоматически — это следующий этап pipeline, не готовая интеграция.
MLflow не нужен для запуска API или будущего экспортированного model bundle.

## Обновления и воспроизводимость

Автор изменения меняет зависимости в pyproject соответствующего member (или root group),
выполняет `uv lock` из корня, затем sync/tests. Workspace members — backend, packages/core,
data-science. Backend/core/research имеют собственные wheel, общий uv.lock фиксирует их вместе.
Коммитятся manifest и lock вместе. CI использует `--locked`, не обновляет версии.
Без общего сервера каждый участник скачивает одинаковый исходный набор и сверяет
SHA-256 из manifest. Датасет и `data-science/artifacts/` не распространяются через публичный Git.

Текущий [Dockerfile API](../backend/Dockerfile) устанавливает backend и base core.
При реализации БД API получит необходимые DB dependencies из `platform`.
Группа `inference` не используется образами: прогноз пересчитывает worker backend через
`infra_pulse_core` ([ARCHITECTURE](ARCHITECTURE.md)); образ API она не расширяет.
Тот же Dockerfile содержит отдельный target `ops` (base + группа `data`, SQL-миграции и
`backend/scripts`) для одноразовых серверных операций: миграций, ограниченного replay
loader и received watcher ([runbook стенда](../deploy/README.md)). Он собирается только
по явному `target: ops`; последний stage и образ API не меняются. Frontend image
принимает build arg `VITE_DATA_MODE` (по умолчанию `fixture`).
Состав пакетов и механизм изоляции описаны в [ARCHITECTURE](ARCHITECTURE.md),
проверяемая граница — API-01/ML-06 в [SPEC](REQUIREMENTS_SPEC.md).

При сборке выпуска сверить lock и digests системных images с DELIV-01;
Python lock сам по себе не закрепляет digest Docker image. Формат manifest
остаётся D10 в [реестре](REQUIREMENTS_DECISIONS.md).

Источники решений: [uv dependency groups](https://docs.astral.sh/uv/concepts/projects/dependencies/),
[locked sync](https://docs.astral.sh/uv/concepts/projects/sync/),
[uv в Docker](https://docs.astral.sh/uv/guides/integration/docker/),
[MLflow backend stores](https://mlflow.org/docs/latest/self-hosting/architecture/backend-store/).

## Локальные sequence-эксперименты

Группа `sequence` в корневом `pyproject.toml` добавляет PyTorch и research-пакет;
версии зафиксированы общим `uv.lock`. Она не входит в API/worker и стандартный
`make check`. `make check-sequence` явно устанавливает её и исполняет тесты
без skips; `make sequence-smoke` обучает только синтетические модели.
[Протокол, устройства и команды](../data-science/experiments/sequence/README.md).
Стандартный Linux wheel может скачать CUDA runtime; CUDA/MPS и Linux-запуск
этого контура пока не проверены. Для Mac исходный режим — CPU.

С 29.09 группа есть в общем lock вместе с релизом: `torch` 2.14.0, `triton`, 15 пакетов
`nvidia-*` и 3 `cuda-*` (все с маркером Linux). `nvidia-nccl-cu13` в lock — 2.30.7 вместо
прежних 2.32.3: одна версия общая для `torch` и `xgboost` из `legacy-boosting`. Без
`--group sequence` эти пакеты не ставятся: CI (`platform`/`train`/`research`), Docker
(`data`) и `uv sync --locked` по умолчанию их не устанавливают. `uv sync --group sequence`
на Linux скачает CUDA-библиотеки объёмом в гигабайты.
