# Миграции операционной БД

Локальные SQL-миграции `0001`–`0021` (без `0017`) создают таблицы ограниченного
replay/received сценария, исходных сообщений, локальных заметок с audit, загрузки CSV, публикации
прогноза, сессий, решений, общего audit и токенов интеграции, а также view аналитики
для Grafana. Номер `0017` закреплён за пакетом B4. Последним на каждом прогоне идёт
`9000_runtime_role.sql` — права рантайм-роли `infra_pulse_app` ([ниже](#9000-рантайм-роль-api-и-worker)).
`0001`–`0011` — replay/received сценарий и заметки.
`0009` нумерует принятые received-записи, `0010` — заметки в каждом
stream/snapshot для стабильной пагинации. `0011` добавляет границу
received-списка, увиденную при сохранении заметки. Для прежних заметок она
остаётся `NULL`; их исходный срез не восстанавливается. `0012` (B1) добавляет
загрузку CSV: `import_files`, `import_quarantine`, версии справочников
`ref_versions`/`ref_channels`/`ref_objects`/`ref_states` и очередь `jobs` воркера;
внешних ключей на таблицы B2/B3 нет; загрузка пишет `import.uploaded` в
`audit_events` из `0014`. `0013` (B2) — прогноз «обесточивание фидеров объекта»:
состояние детектора, эпизоды и события фазы, раскладка каналов (пикет, вид фидера),
дни покрытия, runs, cutoffs, неизменяемые снимки карточек (триггер), исходы и события
окна; все таблицы привязаны к `(namespace_id, snapshot_id)`, внешних ключей на B1/B3
нет. Все файлы применяются в порядке номеров одной транзакцией владельцем схемы
(`POSTGRES_USER`): сервис `migrate`, loader'ы и засев истории. `migrate` и worker берут
общий advisory lock. Ingestion-воркер применяет файлы при старте, только если его роль
может создавать объекты в схеме, то есть при подключении владельцем. Под рантайм-ролью
он их пропускает. Для существующей БД до запуска нового API выполните из корня:

```sh
INFRA_DB_DSN="$INFRA_RECEIVED_DSN" uv run --locked python backend/scripts/migrate_operational_db.py
```

С `INFRA_DB_APP_PASSWORD` та же команда после миграций включает LOGIN рантайм-роли.

`0014` (B3) создаёт `auth_sessions` (только SHA-256 токенов), `forecast_decisions`
(ревизии решений с «кому и когда сообщено»: оба поля или ни одного, не позже
решения), `work_order_drafts` (всегда `not_sent`) и `audit_events`.
Для `audit_events` и `forecast_decisions` UPDATE и DELETE запрещены триггером.
Внешних ключей на таблицы других пакетов (0012, 0013) нет.

`0015` (B1x) добавляет приём пачек через API. Таблица `integration_tokens` хранит
только SHA-256 токена, имя, автора, время выдачи, отзыва и последнего использования.
Таблица `observation_batches` — реестр пачек: ключ `(client_id, batch_id)`, SHA-256
тела, ссылка на `import_files`. Миграция расширяет CHECK `import_files.format`
значением `journal_json` и CHECK `audit_events.actor_role` значением `integration`.
Ограничение пересоздаётся, только если нужного значения ещё нет, поэтому повторный
прогон ничего не меняет.

`0016` (OPS-G, OPS-06) создаёт схему `analytics` только из view: записи
`dispatch_observations` в длинном формате (`signals`, `signal_numeric`,
`signal_state`, `source_alarms`) и действующие справочники для фильтров Grafana.
Текст значения не меняется, `alarm` остаётся флагом источника. Роль `analytics_read`
(NOLOGIN) создаётся идемпотентно: USAGE и SELECT только на `analytics`, к операционным
таблицам доступа нет. Паролей в миграции нет; логин Grafana создаёт
[`deploy/grafana/create-reader.sh`](../../deploy/grafana/create-reader.sh). Без права
CREATEROLE миграция создаёт view и оставляет роль DBA с предупреждением. API
продукта схему не читает.

`0019` (C0.4) добавляет к `forecast_decisions` сроки `awaiting_result_until` (только R3
«Сообщено энергетику») и `watch_until` (только R1 «Под наблюдением»), оба позже решения,
и создаёт `forecast_check_results` — ревизии результата проверки (ждём / устранено /
нарушений нет / не проведена, «что устранено» по справочнику, причина события). Таблица
только для добавления (триггер 0014), ключи идемпотентности как у решений. Номер 0017
зарезервирован за B4, 0018 занят G1.

`0018` (G1) — журнал в формате Приложения 1 ТЗ, отчёт загрузки XLSX и audit запросов:
`dispatch_observations.alarm` становится nullable (NULL — «не передан источником», не
false; читатели трактуют NULL как «не true»), новый столбец `source_channel_type_id`
(«ИД типа канала данных» дословно; без перезаписи таблицы), в `import_files` —
`source_layout`, `source_container`, `alarm_not_provided`, `notes` (строки отчёта),
причина карантина `channel_type_conflict` (CHECK пересоздаётся, только если значения
нет) и индекс `audit_events (action, occurred_at)`. Новых таблиц audit нет: просмотры,
сводки опросов и смены статуса загрузки пишутся в `audit_events` (0014).

`0020` (OPS-G, 28.09) — дашборды Grafana на данных стенда.

- `analytics.channels` и `analytics.objects` берут сначала действующий справочник
  (0012), для остальных каналов и объектов — раскладку прогноза `forecast_channel_layout`
  и `forecast_objects` (0013). Засев истории заполняет только раскладку.
- Индексы `dispatch_observations`:
  - `(namespace_id, snapshot_id, event_at)` — окно и последние записи;
  - частичный `(namespace_id, snapshot_id, channel_id, event_at)` с условием
    `value_numeric IS NOT NULL`.

Файлы применяются каждый раз по порядку: 0016 снова создаёт свои определения этих двух
view, 0020 их заменяет. Столбцы и права те же. Новое определение view — в 0020, не в 0016
([deploy §13](../../deploy/README.md#13-grafana-профиль-analytics)).

`0021` (28.09) — пачки XML в `POST /api/v1/observations` (ТЗ §7): CHECK
`import_files.source_container` из 0018 расширяется значением `xml`. Ограничение
пересоздаётся, только если значения ещё нет; формат импорта остаётся `journal_json`,
тело XML хранится как есть (`<import_id>.xml`).

Это не полный production storage: territorial scopes, workflow D02, scoring и
прогнозы остаются следующими задачами. Пустой `docker compose up db` сам по
себе не загружает данные.

## 9000: рантайм-роль API и worker

`9000_runtime_role.sql` (29.09, SEC-03, SEC-04) сортируется после всех миграций схемы и
на каждом прогоне заново применяет матрицу прав: сначала `REVOKE ALL`, затем `GRANT`.

- Роль `infra_pulse_app` создаётся идемпотентно: NOLOGIN, без пароля, NOSUPERUSER,
  NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS. LOGIN и пароль миграция не
  меняет и роль не удаляет: роли общие для кластера.
- На БД роль получает CONNECT и TEMPORARY: worker кладёт файл во временную таблицу. На
  схему — только USAGE, у `PUBLIC` отнимается CREATE; на PostgreSQL 15+ так и было.
- На каждую таблицу схемы в файле одна строка с явным набором прав, только из SELECT,
  INSERT, UPDATE, DELETE. TRUNCATE, REFERENCES, TRIGGER и MAINTAIN роль не получает
  нигде. Последовательности `serial` получают USAGE, если у таблицы есть INSERT.
- Журнал действий и работа диспетчера только дописываются: `audit_events`,
  `forecast_decisions`, `forecast_check_results`, `work_order_drafts` и
  `replay_review_audit` — INSERT и SELECT. Триггеры 0014/0019 остаются второй защитой.
- В `dispatch_observations` UPDATE разрешён только на столбец `alarm`: worker
  дописывает признак «не передан» (0018). Текст записи роль изменить не может.
- `dispatch_received_batches` роль не получает: её пишет устаревший `received-watcher`
  от владельца.
- Схема `analytics` остаётся только за `analytics_read` (0016).

Таблица без строки в матрице не получает прав, миграция пишет WARNING.
`backend/tests/test_db_roles.py` падает на такой таблице. Новая таблица добавляется в
миграцию и в матрицу одним diff. Без CREATEROLE у пользователя миграций таблицы всё
равно создаются, а роль остаётся DBA с предупреждением, как `analytics_read` в 0016.

Включение LOGIN, проверка роли и один вход — `infra_pulse_backend.storage.migrations_pg`,
его вызывает `backend/scripts/migrate_operational_db.py` при заданном
`INFRA_DB_APP_PASSWORD`. На сервер уходит только SCRAM-verifier пароля. Проверка
отказывает, если у роли есть повышенный атрибут, членство в другой роли, собственные
объекты или CREATE на БД и схеме. Порядок на стенде — [deploy §«Роли PostgreSQL»](../../deploy/README.md#роли-postgresql).
