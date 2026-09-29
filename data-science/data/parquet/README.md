# Parquet для исследований

Это канонический каталог входных snapshots для новых EDA/ML. Сами Parquet и
manifests с приватными ID не коммитятся. Исходные 7z/CSV остаются во внешней
read-only папке участника; ETL не изменяет их.

```text
baseline/snapshot-<id>/             сохранённый прежний исследовательский snapshot
year=YYYY/*.parquet                 скачанная готовая full-history выгрузка для EDA
<dataset-version>/events/           предложенная новая структура ETL
<dataset-version>/references/
<dataset-version>/manifest.json
<dataset-version>/quality-summary.json
```

Скачанная full-history выгрузка сейчас размещается непосредственно по годам:

```text
year=2019/*.parquet
...
year=2026/*.parquet
```

Versioned `<dataset-version>/events/` остаётся форматом публикации нашего
экспериментального CSV → Parquet bridge, потому что рядом с данными он сохраняет
manifest и quality summary. Это два допустимых входных layout, а не требование
перекладывать уже готовую выгрузку.

Этот Parquet проверял предварительный full-history EDA 22.09 (скрипт в публичный репозиторий
не входит): schema/partitions, SHA-256 и coverage с audit в локальном artifact run. Итоговый
слой `journal-curated-v4` строится из исходных 7z — `curate_journal.py`
([слой данных под v9](../../README.md#слой-данных-под-v9)). Сам факт загрузки каталогов из Drive не устанавливает
provenance исходных CSV и не делает snapshot production curated.
Audit описывает весь вход; EDA по умолчанию выбирает каналы дым/тепло/температура
и исключает 2021. Scope и exclusion version записываются в run manifest.
Данные во время расчёта неизменяемы; смена содержимого инвалидирует checkpoints.
Производные canonical timestamps/transitions хранятся в игнорируемом artifact
cache, не рядом с исходными файлами.

Будущую схему, partition и типы фиксирует исследование вместе с backend. Наличие каталога
не означает, что production ETL уже готов. Подробная приёмка —
[DATA-01–DATA-07](../../../docs/REQUIREMENTS_SPEC.md#data--вход-и-происхождение),
схема и accounting — [DATA](../../../docs/DATA.md#контракт-следующего-etl).
Открытые scope/режимы — [D01/D07](../../../docs/REQUIREMENTS_DECISIONS.md).

Датасет не кладут в `mlflow/store`. В MLflow фиксируют source/snapshot SHA-256,
версии import/normalization/coverage и пути; это provenance, не копирование raw.
Проверки чтения/публикации — DATA-02/DATA-03; признаки/labels разделены и имеют
собственные версии по ML-03. Здесь хранится вход исследования, а не его результаты.

Отдельный **исторический sizing snapshot** all-types находится в
`data-science/reports/local/storage-sizing-2026-09-20/`. Они перенесены без изменения содержимого, не
перемещены автоматически в curated и не становятся утверждённым входом обучения.
