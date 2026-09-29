# Notebooks

[08_MY_EDA.ipynb](08_MY_EDA.ipynb) — обзор всех восьми лет 2019–2026: ленивые
DuckDB-датафреймы годовых Parquet и исходных CSV, сверка счётчиков по годам,
справочники объектов, подсистем и датчиков, покрытие соединений. Первые ячейки
открывают рабочее представление `current_enriched_df` — слой `journal-curated-v4`
с overlay `alarm-spike-exclusions-v1`; тот же источник использует
`infra_pulse_research.modeling.fire_source`. Полные таблицы в pandas не переносятся.

Notebook хранится без outputs (проверяет `data-science/tests/test_notebooks_have_no_outputs.py`):
перед коммитом
`jupyter nbconvert --clear-output --inplace data-science/notebooks/08_MY_EDA.ipynb`.
Для VS Code выбрать ядро `.venv/bin/python` из корня проекта. Входы — локальные
`data-science/data/` и `data-science/artifacts/` ([данные](../data/README.md)); данные
организаторов в репозиторий не входят. Запуск Jupyter — `make research`.
