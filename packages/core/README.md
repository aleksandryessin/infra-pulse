# Общий core

Нормативный маршрут: [API-01](../../docs/REQUIREMENTS_SPEC.md#api--операционная-история)
и [ML-03/ML-05/ML-06](../../docs/REQUIREMENTS_SPEC.md#ml--постановка-и-допуск).
В передаче: ID → generation/boundary/cutoff/parity evidence. Окончательные target,
eligibility и policy зависят от D01/D05/D06 в [реестре](../../docs/REQUIREMENTS_DECISIONS.md);
гипотезы не вшиваются в общий контракт как утверждённая норма.

Workspace package `infra-pulse-core`, import `infra_pulse_core`.

- `src/infra_pulse_core/contracts/` — единственный canonical Pydantic contract.
  Generated OpenAPI/fixture находятся в корневом `contracts/`, TypeScript — во frontend.
  `attention.py` отдельно описывает исходное наблюдение и предварительную
  очередность разбора, а также кандидат локального `ReceivedBatch`; это не
  `Risk.risk_level`, не вероятность отказа и не утверждённый D07 формат.
  `Capabilities.received_status_checked_at` — момент чтения локального
  received-состояния по часам PostgreSQL; сравнение с временем последнего
  просмотра папки не задаёт порог устаревания или доступность источника.
  `received_after_watermark` и счётчики после него показывают новые исходные
  записи, подмножество с `alarm=true` и предварительных кандидатов (`alarm=true`
  или точная пара типа и исходного текста) во всём локальном stream; это не
  шкала тяжести, подтверждённый инцидент или результат модели.
  `received_candidate_groups_after_watermark` считает различные пары
  указанного во входе `object_id × channel_id` среди новых кандидатов.
  Повторные строки не удаляются; привязка объекта остаётся неподтверждённой.
  `AttentionList.received_after_watermark` обозначает нижнюю границу списка
  поступивших записей; `received_watermark` остаётся верхней границей.
  `ObjectAttentionSummary.attention_band` отражает наличие в выбранном срезе
  исходного `alarm=true`, только точных текстовых кандидатов либо отсутствие
  кандидатов; это не текущее состояние объекта и не оценка тяжести.
  Для объектной и канальной групп `last_source_alarm_at` и
  `last_watch_text_at` отдельно обозначают последнюю доступность исходного
  alarm и точного текста при `alarm=false`; `last_available_at`/последняя
  запись могут относиться к другому сообщению.
  `ChannelAttentionList` задаёт пагинированную детализацию объекта/системы/
  канала по доступным сообщениям; последняя группа не является состоянием.
  `multiple_object_ids_seen` означает только, что этот `channel_id` встречается
  с другим значением `object_id` в активном scope до выбранного `as_of`;
  отсутствие ID тоже считается отдельным значением. Причина и правильная
  историческая принадлежность из этого признака не следуют.
  `ReplayCoverageObjectList`/`ReplayCoverageChannelList` отдельно описывают
  наличие записей у каналов принятого текущего справочника в ограниченном
  историческом окне. Отсутствие записи не кодирует норму или отказ.
  `forecast.py` — прогноз `sensor-failure`. `ForecastCard` — неизменяемая
  выданная карточка (объект × тип датчика × горизонт `24h|48h|168h` × cutoff):
  каналы с пикетами, факты с периодами до cutoff, версии. Метка —
  зарегистрированное техническое событие канала, не поломка. Overlay «уже
  наблюдается» живёт в `ForecastCardView` рядом с карточкой и её не меняет;
  журнал хранит голую карточку. По шлюзу A score — только `risk_estimate`
  («оценка риска»): калиброванная оценка в [0; 1] и рядом эмпирическая доля
  событий ранговой корзины (числитель, знаменатель, период, отчёт). Корзины
  и уровень риска каждой корзины задаёт versioned policy по цели × горизонту
  (`policy_id`, `bucket_starts`, `bucket_levels`); карточка в пределах бюджета
  не бывает `low`. `probability` отклоняется без `calibration_version` и без
  `PROBABILITY_DISPLAY_APPROVED` (ML-05 partial). Scored-карточка требует
  `object_id`; без привязки допустим только abstained `no_object_binding`.
  Abstained-карточка перечисляет каналы, которые покрыла бы, а её исход —
  `event_without_forecast` / `no_event_without_forecast` / `unknown`, всегда
  вне метрик качества. Realized — только через каналы-кандидаты карточки;
  прочие события объекта — `other_events_on_object_count`. Тексты фактов и
  `abstention_detail` проверяются на запрещённые формулировки (диагноз,
  причина, «норма»); O1–O5 допустимы только с отметкой «симуляция».
  `ForecastList.publication_token` покрывает все опубликованные прогоны
  цель × горизонт (`runs` с собственными `generation`).
  **C0, 27.09.2026.** Добавлены:
  - цель `phase-feeders/phase_loss_all` («обесточивание фидеров объекта», C0.1: все
    эпизоды «Неисправен» каналов «Состояния фазы»; 99,7% событий — только фидеры),
    `incident_type = feeder_power_loss` и подпись события по цели. Карточка — на объект:
    в ней 3–5 фидеров с наибольшим числом эпизодов (`feeder_kind`, пикет, последняя дата)
    и `channels_total`;
  - горизонт `336h`, scorer `static_list`;
  - `ListPolicy` у `PublishedRun`: `rolling_release`, `max_open`, `window_days`,
    освобождение при первом событии. Открытые карточки могут быть выданы прежними
    cutoff;
  - в `ForecastCardView`: `list_state` (open / released / expired), `released_at`,
    «день k из n», `recommendation` (текст версионированной политики);
  - `ForecastScore.kind = frequency_share` («доля k из n»): корзина по числу событий
    пары за 365 сут, k, n, интервал Уилсона, период;
  - `recurrence` (chronic / fresh) с версией правила, факты `events_365d` и
    `power_off_followup`;
  - в журнале: `released_at` (исход `realized` разрешается при событии) и события
    окна с `while_open`. Названия recall не зафиксированы: сводка
    `ForecastQualitySummary` считает «k из n» по карточкам и несёт именованные
    `RatioMetric` с версией определения;
  - `ForecastState` для опроса, `RecurringPlaceList` — реестр хронических мест,
    `ForecastDecisionCreate` и `ForecastDecisionList` — решение и его ревизии.

  `imports.py` — загрузка CSV журнала и трёх справочников: статусы, отчёт о
  строках, причины quarantine, тайминги, ID новых и снятых карточек.
  `auth.py` — роли `dispatcher` / `analyst` / `admin` и права `read` / `decide` /
  `import` / `research` («Исследование» и P/R — только analyst и admin).
  `research.py` — агрегаты страницы «Исследование»: области, лестница (в том числе
  `random_list`), гибкие блоки-таблицы. `scheme.py` (C0.1) — «линейки пикетов» объекта:
  ориентиры (вводы, АВР), фидеры с пикетом или диапазоном, колонка «ПК ?», открытые
  карточки, текущие тревоги и снятые за 7 сут; без координат и связей. У `ObservedMessage`
  — необязательный пикет (`picket_basis`, в том числе `channel_name`). Решение несёт
  «кому и когда сообщено» (`notified_to`, `notified_at`); рекомендация — версия политики и
  `regulation_confirmed`. Журнал диспетчера — `ForecastJournalCounts` (выдано / снята по
  событию / без события / неизвестно / открыто), без P и R.
  **C0.2.** `imports.py`: формат `journal_json` и `ObservationBatch` / `ObservationRecord` —
  поток через API с полями журнала (ключи как в CSV или английские; время — `date` +
  `time` по МСК или `event_at`; значение — строка дословно; `batch_id` — ключ
  идемпотентности; до 5 000 записей и 5 MiB). `auth.py`: роль `integration`
  (`auth_source = token`), права `ingest` (integration, admin) и `report` (analyst,
  admin). `notifications.py` — колокольчик: новые карточки и критические исходные
  тревоги по версионированной политике. `reports.py` — месячная сводка для руководства.
  `forecast.py`: цель пилота `fire-detectors/failure_pilot` (`fire_detector_failure`).
  **C0.3.** Подпись цели `phase-feeders/phase_loss_all` — «обесточивание электрооборудования
  объекта», подпись события — «Линии питания освещения, вентиляции и насосов: сначала потеря
  связи („Неисправен“), через ~10 мин — „Обесточен“. Причина неизвестна». В пользовательских
  текстах «фидер» заменён на «линия электропитания»; ID и имена полей не менялись.
  `SchemeFeeder.named_link` — метка из названия канала («ФВ2 (В23)» → «В23»); разбор —
  `features/channel_names.parse_named_link` (`named-link-from-channel-name-v1`, словарь
  `phase-feeder-kinds-v2`). В `features/incident_list` паре без событий за 365 сут карточка
  не выдаётся (`MIN_EVENTS_FOR_CARD = 1`, политика `…-min1-v2`); список может быть короче K.
  **C0.4.** Подписи кодов решений — `DECISION_LABELS` (R3 «Сообщено энергетику», R1 «Под
  наблюдением», R7 «Нет оснований»; коды не переименованы). R3 требует `notified_to`,
  `notified_at` и `awaiting_result_until`, R1 — `watch_until`; способы проверки обязательны
  для всех кодов. Результат проверки — `ForecastCheckResultCreate` / `ForecastCheckResult`
  (`awaiting` | `fixed` | `no_violation` | `not_done`, «что устранено» — справочник, не
  подтверждён заказчиком; `event_cause` только для сбывшейся карточки). В журнале —
  последний результат проверки, у исхода и событий окна — `power_off_at` (первое
  «Обесточен»). `ObservedMessage.object_name`; `ForecastState.forecast_data_as_of` и
  `source_messages_as_of` — два среза данных.
  **C0.5.** Строка колокольчика (`NotificationItem`, политика `critical-alarms-v1`):
  `row_kind` (`single` | `test_series` | `calibration_series` | `chatter`),
  `collapsed_count`, `channels_count`, `first_at` и `members` — до 50 новейших записей
  строки (row_uid, канал, время, значение) для раскрытия без скрытия; `priority` (0 —
  одиночный «Обнаружен газ» первым, затем новые первыми). `NotificationSummary.policy_caption`
  — подпись политики для заказчика.
- `src/infra_pulse_core/features/` — причинные функции прогноза; offline-метки и оценка — в
  data-science (`sensor_failure_target.py`, `sensor_failure_release.py`).
  **B2, 27.09.2026 — прод «обесточивание фидеров объекта»** (`prod_phase` v9). Три модуля на
  чистом Python, без pandas и numpy. Один код используют воркер, засев истории и сверка с
  research.
  - `phase_feeder_episodes.py` — инкрементальный причинный детектор эпизодов «Неисправен»
    каналов «Состояние фазы» по LABELS.md, `DETECTOR_VERSION`:
    - Q = 24 ч; кандидат / чистая / нейтральная отметка; записи одной секунды не
      упорядочиваются;
    - конец — первая чистая отметка; «Обесточен» ≤ 10 мин после начала;
    - события — цепочки начал объекта с W = 10 мин.

    Состояние канала — JSON между загрузками; прогон по частям равен прогону целиком. Запись
    раньше watermark канала требует пересборки канала из истории (`late_channels`).
  - `incident_list.py` — прод-список:
    - score — события объекта за `[t − 365 сут, t)`; кандидаты канала на cutoff;
    - причинная допустимость; шаг списка 14 сут / K = 10 с освобождением в момент события;
    - «доля k из n»: 3 крупных бина dev-таблицы v9 с интервалом Уилсона;
      `FREQUENCY_TABLE_VERSION` — нижняя оценка, не вероятность;
    - recurrence: событие за 14 сут; инциденты — пауза < 24 ч.
  - `channel_names.py` — пикет из названия (`ПК28`, `ПК86-85`, `ПК451-ПК469`, иначе
    `unknown`) и версионированный словарь `phase-feeder-kinds-v1`:
    - фидеры: освещение — РО/ГРО/ФРО/ФАО; вентиляция — ФВ; насосы — ФАНС; ОЗК; прочее;
    - ориентиры: АВР, ЩАП, вводы, межсекционные.

    Названия из «прочего» — в отчёте сверки для технолога.

  Совпадение с research на истории фазы (эпизоды, score, открытые наборы фолда 2024) —
  `data-science/reports/core-research-parity-v9-2026-09-27.json`.

  **Contract (B2, одобрено 27.09 при слиянии).** В `ForecastJournalEntry`
  событие исхода может быть на неперечисленном канале-кандидате, если `channels_total` больше
  числа перечисленных каналов: карточка показывает только топ-5 фидеров. OpenAPI не меняется.

Base зависимость — Pydantic. Extra `features` добавляет numpy/pandas только
потребителям вычисления признаков. Backend использует base; research — extra.
Core не зависит от HTTP, research, MLflow и estimator-библиотек.

Тесты core — `tests/test_core_phase_feeders.py`, `tests/test_core_c03_rules.py`; сверка с
research — `data-science/tests/test_core_research_parity.py`.

После изменения контракта из корня выполнить `make contracts`,
`cd frontend && npm run generate:api`, затем полный `make check` и frontend build.
Схемы/TS обновляются вместе; feature API меняется по согласованию двух DS и lead.
