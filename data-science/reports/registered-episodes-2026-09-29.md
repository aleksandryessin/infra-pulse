# Исследование зарегистрированных эпизодов

Просмотренная история; зарегистрированные состояния, не физические отказы.

Основной горизонт 14 суток, маска предполагаемых работ переводит исход в unknown.

| Таргет | Период | Модель | Precision | Recall | Unknown-карточки |
|---|---|---|---:|---:|---:|
| A | dev2024 | catboost | 0.248 | 0.394 | 23.333 |
| A | dev2024 | frequency | 0.188 | 0.290 | 29.000 |
| A | dev2024 | persistence | 0.165 | 0.258 | 7.000 |
| A | dev2024 | tcn | 0.233 | 0.362 | 23.000 |
| A | dev2024 | transformer | 0.238 | 0.366 | 20.667 |
| A | viewed2025_2026 | catboost | 0.417 | 0.520 | 83.333 |
| A | viewed2025_2026 | frequency | 0.391 | 0.468 | 94.000 |
| A | viewed2025_2026 | persistence | 0.333 | 0.424 | 70.000 |
| A | viewed2025_2026 | tcn | 0.346 | 0.415 | 66.000 |
| A | viewed2025_2026 | transformer | 0.390 | 0.489 | 81.333 |
| B | dev2024 | catboost | 0.189 | 0.817 | 26.000 |
| B | dev2024 | frequency | 0.204 | 0.839 | 30.000 |
| B | dev2024 | persistence | 0.093 | 0.419 | 18.000 |
| B | dev2024 | tcn | 0.159 | 0.753 | 27.667 |
| B | dev2024 | transformer | 0.175 | 0.785 | 27.667 |
| B | viewed2025_2026 | catboost | 0.503 | 0.793 | 114.333 |
| B | viewed2025_2026 | frequency | 0.477 | 0.737 | 115.000 |
| B | viewed2025_2026 | persistence | 0.374 | 0.576 | 89.000 |
| B | viewed2025_2026 | tcn | 0.480 | 0.759 | 113.667 |
| B | viewed2025_2026 | transformer | 0.486 | 0.759 | 111.000 |
| phase | dev2024 | catboost | 0.758 | 0.907 | 0.000 |
| phase | dev2024 | frequency | 0.754 | 0.904 | 0.000 |
| phase | dev2024 | persistence | 0.685 | 0.772 | 0.000 |
| phase | dev2024 | tcn | 0.705 | 0.810 | 0.000 |
| phase | dev2024 | transformer | 0.745 | 0.880 | 0.000 |
| phase | viewed2025_2026 | catboost | 0.773 | 0.880 | 19.333 |
| phase | viewed2025_2026 | frequency | 0.768 | 0.874 | 21.000 |
| phase | viewed2025_2026 | persistence | 0.703 | 0.765 | 20.000 |
| phase | viewed2025_2026 | tcn | 0.762 | 0.858 | 19.333 |
| phase | viewed2025_2026 | transformer | 0.761 | 0.862 | 21.333 |

## Вердикты

- A / catboost: advantage_not_established
- A / tcn: advantage_not_established
- A / transformer: advantage_not_established
- B / catboost: advantage_not_established
- B / tcn: advantage_not_established
- B / transformer: advantage_not_established
- phase / catboost: advantage_not_established
- phase / tcn: advantage_not_established
- phase / transformer: advantage_not_established

Межмодельные критерии и интервалы — в JSON рядом. Порог P≥0,7/R≥0,5 отдельно от прироста.
Все семейства сохранены; продолжения показаны отдельно от новых эпизодов.
Precision исключает неизвестные окна карточек; recall считает известные начала,
в том числе пойманные карточкой с неопределённым остатком окна. Поэтому числители
могут различаться при сопоставлении один-к-одному. Lead включает все совпадения,
включая неопределённые исходы; это не подтверждение заблаговременности инцидентов.
Подтверждение требует новых данных или проверенных эксплуатационных исходов.
