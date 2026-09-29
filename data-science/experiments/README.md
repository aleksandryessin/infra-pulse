# Эксперименты

- [«Отказ датчика» `sensor-failure`](sensor-failure/README.md) — постановка, метки,
  версии v1–v11 и их отчёты; итог — [FINAL_V9](sensor-failure/FINAL_V9.md), путь к
  решению — [журнал решений](sensor-failure/DECISION_LOG.md).
- [Сырой журнал → curated](curated-etl/README.md) — протокол и проверки DATA-03 для
  слоя, на котором построены метки.
- [Последовательности фазы `sequence`](sequence/README.md) — GRU, LSTM, TCN, Transformer
  Encoder и CatBoost против статичного списка, 28.09; диагностика на просмотренных
  периодах, прирост не подтверждён ([отчёт](../reports/phase-sequence-2026-09-28.md)).
- [Зарегистрированные эпизоды `episodes`](episodes/README.md) — новые эпизоды по типам
  датчиков ТЗ (A/B, фаза отдельно), 29.09; критерий преимущества над частотой не выполнен
  ([отчёт](../reports/registered-episodes-2026-09-29.md)).

Исследования до выбора направления (EDA и когорты, газовая постановка, дымовой
baseline, alarm подсистемы) в публичный репозиторий не входят; их выводы, повлиявшие на
итоговое решение, сведены в [журнал решений](sensor-failure/DECISION_LOG.md) и
[выбор направления](../../docs/DIRECTION_DECISION_2026-09-26.md).
