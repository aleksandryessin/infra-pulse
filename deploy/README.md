# Серверный стенд: GitHub Actions → SSH → Docker Compose

Нормативный маршрут: [OPS-01–OPS-06, SEC-04, DELIV](../docs/REQUIREMENTS_SPEC.md#ops--переносимость-и-эксплуатация);
параметры стенда — D12, копии — D09 в [реестре решений](../docs/REQUIREMENTS_DECISIONS.md).
Общий порядок запуска и проверок — [OPERATIONS](../docs/OPERATIONS.md#серверный-стенд-через-ssh-туннель).

**Актуально на 29.09.2026.**
- *Работает на сервере* (x86_64, Linux): публичный стенд https://infra-pulse.ru с 28.09 —
  Caddy с сертификатом Let's Encrypt, вход через lldap, Grafana (профиль `analytics`),
  ежедневные копии (профиль `backup`), `worker`, засев истории фазы и июньских сообщений
  пожарной сигнализации, газа, затопления и насосов. Версия от 29.09.2026, выпуск
  workflow-ом `Deploy stand`.
- *Проверено на стенде:* smoke деплоя, вход администратором и аналитиком, ручная копия
  197 МиБ за 137 с и `backup.sh --check`, нагрузка 20 пользователей на чтение
  ([STATUS](../docs/STATUS.md)).
- *Проверено на сервере 29.09:* восстановление из ночной копии
  `restore.sh --replace --public --skip-build` — 367 с (страховочная копия 153 с,
  `pg_restore` 166 с), строки 36 таблиц совпали с копией, миграции и smoke пройдены, api и
  worker подключаются рантайм-ролью `infra_pulse_app` ([ниже](#роли-postgresql)).
- *Выпущено на сервер 29.09, отдельно не проверялось:* Swagger `/api/docs`.
- *Не проверено на сервере:* пачка API до публикации, запуск на PostgreSQL 12.
- *Проверки этого репозитория 29.09.2026* (локально, macOS arm64): `make check` — 664 passed,
  110 skipped; `make check-db` на PostgreSQL 17.11 — 117 passed под рантайм-ролью; shellcheck
  скриптов `deploy/`, `docker compose config` и `make compose-server-config` — без ошибок
  ([STATUS](../docs/STATUS.md#проверки-этого-репозитория-29092026)).
- *Planned:* копия вне VPS и её шифрование, корпоративный AD вместо lldap
  ([этап 2](#этап-2--planned)).

Ниже — датированные записи по мере реализации; более поздняя запись и блок выше отменяют
пометки «не проверено» в ранних.

**26.09.2026.** *Implemented:* серверный override Compose, ручной workflow
деплоя, серверный скрипт сборки/запуска/smoke, контейнерные migrate/replay-loader/
received-watcher. Проверено локально на Docker Desktop (linux/arm64) в режимах
fixture, replay и received на **синтетических** данных. На сервере и в GitHub тогда не
запускалось (с 27.09 — запускается, см. выше).

**27.09.2026, загрузка CSV (B1).** Добавлен долгоживущий сервис `worker` (ingestion
CSV) и том `uploads`; `received-watcher` помечен устаревшим. У `db` на стенде заданы
`shared_buffers=512MB` и `max_wal_size=4GB`. Проверено: `docker compose config`.
**Не проверено:** сборка образа `worker` и запуск в Docker (Docker daemon на машине
проверки недоступен), запуск на сервере. См.
[раздел worker](#ingestion-worker-и-загрузка-csv).

**27.09.2026, публичный доступ (B3).** *Implemented в коде и конфигурации:* Caddy
с Let's Encrypt, тестовый каталог lldap, вход с ролями и audit —
[раздел 11](#11-публичный-доступ). *Не проверено:* запуск контейнеров Caddy/lldap,
выпуск сертификата и bind к настоящему lldap — Docker daemon на машине проверки
недоступен, сервер не подключался.

**27.09.2026, резервные копии (OPS-B).** *Implemented в скриптах и конфигурации:*
`backup.sh`, `restore.sh` и сервис `backup` (профиль `backup`) —
[раздел 12](#12-резервные-копии-и-восстановление). Полный цикл «копия → уничтожение →
восстановление → smoke» прогнан локально на PostgreSQL 14 с заглушкой `docker`.
*Не проверено:* контейнер `backup`, сборка образов и запуск в Docker, сервер. Копия вне
VPS — только инструкция; RPO и срок хранения не согласованы (D09).

**27.09.2026, Grafana (OPS-G, OPS-06).** *Implemented в конфигурации и миграции:*
профиль `analytics`, схема `analytics` и роль `analytics_read` (миграция 0016), два
дашборда, вход через lldap, путь `/grafana/` за Caddy — [раздел 13](#13-grafana-профиль-analytics).
Проверено локально в Docker на синтетике (PostgreSQL 17, lldap, Caddy, Grafana).
*Не проверено:* запуск на сервере.

**29.09.2026, роли PostgreSQL (SEC-03, SEC-04).** *Implemented:* api и worker подключаются
рантайм-ролью `infra_pulse_app` только с DML, владелец схемы `POSTGRES_USER` остаётся за
миграциями, копиями и разовыми загрузками. Пароль роли деплой генерирует сам, откат — одна
строка в `.env` ([раздел «Роли PostgreSQL»](#роли-postgresql)). Проверено локально в Docker
(PostgreSQL 17): старый стек под суперпользователем → новый деплой → откат переменной →
возврат, восстановление копии в новый кластер, откат кода на прежний ref.
*Не проверено:* запуск на сервере.

## Файлы

| Файл | Назначение |
| --- | --- |
| [`compose.server.yaml`](compose.server.yaml) | Override к корневому `compose.yaml`: порты только на 127.0.0.1, БД без публикации и с настройками `shared_buffers`/`max_wal_size`, `restart: unless-stopped`, лимиты CPU/RAM, ротация логов, долгоживущий `worker` (загрузка CSV), сервисы `migrate`/`replay-loader` (профиль `tools`), устаревший `received-watcher` (профиль `received`) и ежедневный `backup` (профиль `backup`) |
| [`.env.server.example`](.env.server.example) | Шаблон серверного `.env` без реальных значений; им же CI проверяет `config`, в том числе с `compose.public.yaml` (обязательные ключи публичного режима workflow подставляет заглушками, не секретами) |
| [`remote-deploy.sh`](remote-deploy.sh) | Выполняется на сервере: проверки `.env`, пароль рантайм-роли при его отсутствии, миграции и LOGIN роли `infra_pulse_app` в любом режиме (сервис `migrate`), `up -d --build --wait`, smoke (api и worker подключены рантайм-ролью); `--public` или `STAND_PUBLIC=true` — публичный режим и smoke через Caddy; при ошибке — состояние контейнеров и хвост логов, ненулевой код |
| [`app-db-password.sh`](app-db-password.sh) | Дописывает в серверный `.env` пароль `INFRA_DB_APP_PASSWORD` (`openssl rand -hex 32`), если его нет, и проверяет формат существующего; значение не печатается. Вызывают `remote-deploy.sh` и `restore.sh` ([роли PostgreSQL](#роли-postgresql)) |
| [`compose.public.yaml`](compose.public.yaml) | Третий override для публичного стенда: Caddy — единственный сервис с портами 80/443; lldap; `INFRA_AUTH_MODE=ldap` у API; порты web/api сняты |
| [`Caddyfile`](Caddyfile) | TLS 1.2+ и ACME (сначала staging), HSTS и заголовки безопасности, закрытые корневые `/docs` и `/openapi.json` (документация API — `/api/docs`, без исключения CSP), `X-Request-ID` для audit |
| [`lldap/bootstrap.sh`](lldap/bootstrap.sh), [`lldap/render-configs.sh`](lldap/render-configs.sh) | Группы `dispatcher`/`analyst`/`admin` и тестовые учётки из серверного файла; паролей в репозитории нет |
| [`grafana/`](grafana/) | Профиль `analytics` (OPS-06): источник данных и два дашборда (provisioning), `ldap.toml` для lldap, `create-reader.sh`/`.sql` — логин PostgreSQL для Grafana из серверного `.env` ([раздел 13](#13-grafana-профиль-analytics)) |
| [`backup.sh`](backup.sh), [`restore.sh`](restore.sh) | Копия БД, тома `uploads` и конфигурации без секретов с manifest и SHA-256; восстановление на чистый том со сверкой строк и smoke ([раздел 12](#12-резервные-копии-и-восстановление)) |
| [`rsync-filter`](rsync-filter) | Что уходит на сервер: код и шаблоны; без `.git`, данных, артефактов, `.env`, файлов учёток `lldap-users*.txt`, `node_modules`, `.venv` |
| [`../.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) | Ручной запуск `Deploy stand` с входом `ref` |

Образы собираются **на сервере** из доставленного checkout, поэтому серверу не нужен
доступ к репозиторию, а в registry ничего не публикуется. GHCR не выбран: он добавил бы
токен записи пакетов и хранение образов вне сервера без выигрыша для одного стенда.
Сборка требует исходящего доступа сервера к Docker Hub, ghcr.io, PyPI и npm.

## Модель доступа, этап 1

- Приложение слушает только `127.0.0.1` сервера: web `WEB_PORT` (8080), API `API_PORT`
  (8000, для `/api/docs` и диагностики). PostgreSQL не опубликован и доступен только в
  сети Compose. Пользователи работают через SSH-туннель.
- TLS, вход и публичный URL отсутствуют, поэтому порты не открываются наружу.
- Локальные заметки выключены (`INFRA_ENABLE_LOCAL_REVIEWS=false`): у них нет
  аутентификации и автора (D02/D03). Включение допустимо только при доступе через туннель.
- `Неисправен` остаётся technical_fault_state_proxy; исходный `alarm` не является
  подтверждённым отказом. Стенд показывает исходные записи, не прогноз.

## Раскладка на сервере

```text
<DEPLOY_PATH>/                     например /srv/infra-pulse; владелец deploy, 750
├── app/                           код; заменяется при каждом деплое (rsync --delete)
├── shared/.env                    секреты и режим, 600; деплой его не перезаписывает
├── shared/deploy-history.log      строка на каждую попытку: время, результат, SHA, ref, режим
├── data/curated/<snapshot>/       принятый curated snapshot, только чтение
├── inbox/                         JSON-партии для received (необязательно)
└── backups/<UTC-время>/           резервные копии, 700 (раздел 12); вне checkout
```

Данные PostgreSQL хранятся в Docker volume `infra-pulse-msk_postgres_data`,
загруженные через API файлы — в volume `infra-pulse-msk_uploads` (api пишет,
worker читает). Каталоги `data/` и `inbox/` монтируются в контейнеры read-only и не
входят ни в checkout, ни в образы.

## 1. Подготовка сервера (однократно)

Команды выполняются на сервере под `deploy`; `<DEPLOY_PATH>` далее `/srv/infra-pulse`.

```sh
docker compose version            # нужна >= 2.24.4 (используются теги !reset/!override)
command -v rsync curl             # rsync обязателен, curl для smoke (иначе python3)
sudo usermod -aG docker deploy    # затем перелогиниться; группа docker равна root-доступу
sudo install -d -o deploy -g deploy -m 750 /srv/infra-pulse
install -d -m 750 /srv/infra-pulse/app /srv/infra-pulse/shared
install -d -m 755 /srv/infra-pulse/data /srv/infra-pulse/inbox
```

Каталоги `data/` и `inbox/` читаются изнутри контейнера пользователем uid 10001,
поэтому им нужны права 755/644; снаружи их закрывает родительский каталог 750.

Серверный `.env` создаётся из шаблона с рабочей машины и заполняется на сервере:

```sh
scp deploy/.env.server.example deploy@<host>:/srv/infra-pulse/shared/.env
ssh deploy@<host> 'chmod 600 /srv/infra-pulse/shared/.env && openssl rand -hex 32'
# Вывод openssl вписать в POSTGRES_PASSWORD (редактором на сервере).
```

Скрипт деплоя отказывается работать, если в `.env` осталось `__SET_ON_SERVER__` или
файл читается группой/остальными. Исключение — `INFRA_DB_APP_PASSWORD`: пароль
рантайм-роли деплой генерирует сам ([роли PostgreSQL](#роли-postgresql)). Пароль
`POSTGRES_PASSWORD` меняется только до первого запуска БД: позже он уже записан в том
PostgreSQL. При другом `DEPLOY_PATH` исправить `INFRA_SERVER_DATA_DIR` и
`INFRA_SERVER_INBOX_DIR`.

Сетевой экран: входящим оставить только SSH (например, `sudo ufw allow OpenSSH`).
Порты 8080/8000/5432 наружу не открывать. Docker публикует порты в обход ufw,
поэтому защита обеспечивается привязкой к 127.0.0.1 в `compose.server.yaml`.

## 2. GitHub: environment и secrets

Workflow использует единственный environment `production` для публичного
демонстрационного стенда. Его следует создать заранее в Settings → Environments,
разрешить deployment только из `master` и при желании добавить required reviewers:
тогда каждый запуск ждёт ручного approve. Без настройки GitHub создаст environment
при первом запуске без защиты.

| Имя в GitHub environment `production` | Тип | Обязателен | Содержимое |
| --- | --- | --- | --- |
| `VPS_HOST` | variable | да | IP или DNS-имя сервера |
| `VPS_PORT` | variable | нет, 22 | SSH-порт |
| `VPS_USER` | variable | да | `deploy` |
| `VPS_DEPLOY_PATH` | variable | да | абсолютный путь не короче двух уровней, например `/srv/infra-pulse` |
| `VPS_SSH_KEY` | secret | да | приватный ключ, выданный только для Actions |
| `VPS_KNOWN_HOSTS` | secret | да | полная строка known_hosts сервера, сверенная по отпечатку |

Необязательная repository/environment **variable** `DEPLOY_LOG_LINES` (по умолчанию
150) задаёт число строк логов каждого сервиса при сбое; `0` отключает вывод.
В режиме replay access log API может содержать ID объектов из запросов, поэтому
для публичного репозитория уместно значение `0`.

Эти значения сопоставляются с внутренними `DEPLOY_*` только в блоке `env:` job
`deploy` в [`deploy.yml`](../.github/workflows/deploy.yml). Левые внутренние имена
используются шагами workflow и не меняются. `VPS_SSH_FINGERPRINT` можно хранить
отдельной variable для ручной сверки, но workflow нужен именно полный
`VPS_KNOWN_HOSTS`, а не отпечаток.

Отдельный ключ для Actions (на рабочей машине):

```sh
ssh-keygen -t ed25519 -N '' -C 'github-actions infra-pulse stand' -f ./stand_actions_key
ssh-copy-id -i ./stand_actions_key.pub deploy@<host>
# Содержимое ./stand_actions_key -> secret VPS_SSH_KEY; затем удалить оба файла.
```

Закреплённый host key: отпечаток берётся на самом сервере и сравнивается с тем, что
получено по сети; `StrictHostKeyChecking=no` не используется.

```sh
ssh deploy@<host> 'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub'   # эталон
ssh-keyscan -t ed25519 -p <port> <host> > stand_known_hosts
ssh-keygen -lf stand_known_hosts                                        # сравнить
# При совпадении содержимое stand_known_hosts -> secret VPS_KNOWN_HOSTS.
```

Для порта не 22 строка имеет вид `[<host>]:<port> ssh-ed25519 ...`; `ssh-keyscan -p`
выводит её в этом виде. Workflow проверяет, что запись для хоста и порта есть.

## 3. Первый запуск (fixture)

1. Сервер подготовлен, `shared/.env` заполнен, `INFRA_MODE=fixture`.
2. Workflow доступен для ручного запуска после попадания `deploy.yml` в ветку по
   умолчанию. Actions → **Deploy stand** → Run workflow → `ref` (ветка, тег или SHA).
3. Шаги: проверка входов и секретов → checkout ref → `docker compose config` с
   шаблоном (серверный набор и он же с `compose.public.yaml`) → SSH с закреплённым
   ключом → preflight на сервере → rsync по `rsync-filter` → `remote-deploy.sh`.
4. `remote-deploy.sh` при необходимости дописывает в `.env` пароль рантайм-роли, поднимает
   `db`, выполняет `migrate` (миграции и LOGIN роли `infra_pulse_app`), собирает образы,
   запускает стек с `--wait` и проверяет: api и worker подключены к PostgreSQL ролью
   `infra_pulse_app` без суперпользователя;
   порты web/API только на 127.0.0.1, БД не опубликована; `/health/live` API отвечает
   и сообщает режим из `.env`; web отдаёт SPA и проксирует `/health/live`; в fixture
   `/api/v1/attention` через web возвращает синтетические записи; в replay/received
   `/health/ready` должен вернуть 200. Любая ошибка даёт ненулевой код, `docker compose
   ps -a` и хвост логов в журнале run.

Тот же путь без GitHub, с рабочей машины из корня checkout:

```sh
rsync -rlptz --checksum --delete --filter='merge deploy/rsync-filter' ./ deploy@<host>:/srv/infra-pulse/app/
ssh deploy@<host> "bash /srv/infra-pulse/app/deploy/remote-deploy.sh --revision $(git rev-parse HEAD) --ref manual"
```

## 4. Доступ через SSH-туннель

```sh
ssh -N -L 8080:127.0.0.1:8080 deploy@<host>
# UI: http://127.0.0.1:8080/queue
ssh -N -L 8080:127.0.0.1:8080 -L 8000:127.0.0.1:8000 deploy@<host>
# дополнительно Swagger API: http://127.0.0.1:8000/api/docs
```

Правая часть — `WEB_PORT`/`API_PORT` из серверного `.env`, левая — любой свободный
локальный порт. Участникам без shell можно выдать отдельного пользователя с ключом
в `authorized_keys` вида `restrict,port-forwarding,permitopen="127.0.0.1:8080" ssh-ed25519 ...`
и подключением `ssh -N -L ...`. Туннель не пробрасывается дальше в интернет.

## 5. Обновление и откат

- **Перед деплоем** (на сервере, `dc` — из [раздела 6](#6-состояние-и-логи)):
  1. Узнать ревизию стенда: `tail -n 5 ../shared/deploy-history.log` или метка
     `infra-pulse.revision` контейнера api. Вместе с новым `ref` поедут все коммиты и
     миграции после неё, а не только последний diff: например, с первой версии стенда (28.09, 00:20 МСК) впервые
     поедут индексы 0020 по всему журналу.
  2. Свежая копия: деплоить после ночной копии в `BACKUP_AT` (03:30 МСК) или снять её
     вручную с меткой — `dc exec backup bash /app/deploy/backup.sh --once --label pre-final-fixes`
     (метка — `a-z`, `0-9` и `-`; [раздел 12](#12-резервные-копии-и-восстановление)).
- **Обновление:** запуск workflow с новым `ref`. Контейнер БД и его том не
  пересоздаются; api и web пересоздаются, nginx перезапускается вслед за api.
- **Откат:** запуск workflow с прежним `ref`/SHA. Предыдущие SHA есть в
  `shared/deploy-history.log`, в истории runs и в метке контейнера
  `infra-pulse.revision`. Пересборка использует кеш слоёв.
- Миграции применяются только вперёд и сейчас аддитивны (`IF NOT EXISTS`, новые
  таблицы/поля). Совместимость старого кода с более новой схемой не проверялась
  (OPS-03 planned); перед откатом через миграцию нужна свежая копия
  (`dc exec backup bash /app/deploy/backup.sh --once`, [раздел 12](#12-резервные-копии-и-восстановление)).
- Автоматического отката при провале smoke нет: стек остаётся в новом состоянии,
  а run завершается ошибкой с логами. Решение об откате принимает оператор.
- Ошибка `migrate` (миграция или проверка рантайм-роли) останавливает деплой до `up`:
  работающие api и worker не пересоздаются. Откат только роли, без отката кода, —
  одной строкой в `.env` ([ниже](#откат-на-владельца)).

## Роли PostgreSQL

Требования SEC-03 (журнал нельзя незаметно переписать) и SEC-04 (изолированная БД).
Статус 29.09.2026: *implemented*, проверено локально в Docker; на сервере не запускалось.

| Роль | Кто подключается | Права |
| --- | --- | --- |
| `POSTGRES_USER` (`infra_pulse`) | `migrate`, `backup`, `replay-loader`, засев истории, устаревший `received-watcher`, `psql` администратора | Суперпользователь образа postgres, владелец схемы: миграции, копии, разовые загрузки |
| `infra_pulse_app` | `api`, `worker`, CLI токенов в контейнере `api` | LOGIN; NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS; CONNECT и TEMPORARY на БД, USAGE на `public`; по таблицам — только DML из [матрицы 9000](../backend/migrations/9000_runtime_role.sql) |
| `analytics_read` + логин Grafana | `grafana` | Только SELECT на view схемы `analytics` ([раздел 13](#13-grafana-профиль-analytics)) |

Одна рантайм-роль на api и worker, а не две. Worker пишет только то, что приходит через
загрузки API, поэтому отдельная роль API не закрыла бы запись наблюдений. Две роли
удвоили бы матрицу прав и секреты без заметного выигрыша. Главное, что убрано у
процессов стенда: DDL, `COPY … PROGRAM` и чтение файлов сервера, `pg_authid`,
`SET ROLE`, отключение триггеров и `session_replication_role`, `TRUNCATE`.

- **Журнал только дописывается.** `audit_events`, `forecast_decisions`,
  `forecast_check_results`, `work_order_drafts`, `replay_review_audit` — INSERT и SELECT.
  Триггеры 0014/0019 остаются второй защитой. У `dispatch_observations` UPDATE есть только
  на столбец `alarm`: текст записи роль не меняет.
- **Выдача прав — в миграции** [`9000_runtime_role.sql`](../backend/migrations/9000_runtime_role.sql).
  Она идёт последней на каждом прогоне и заново применяет матрицу (REVOKE ALL, затем GRANT).
  Таблица без строки в матрице прав не получает, `backend/tests/test_db_roles.py` падает.
  Миграция создаёт роль NOLOGIN, если её нет, и никогда не меняет её LOGIN и пароль.
- **LOGIN и пароль ставит деплой.** `remote-deploy.sh` вызывает
  [`app-db-password.sh`](app-db-password.sh): если в `.env` нет `INFRA_DB_APP_PASSWORD`
  или там `__SET_ON_SERVER__`, он дописывает `openssl rand -hex 32` (права 600, значение
  не печатается). Затем `migrate` от владельца проверяет роль: нет повышенных атрибутов,
  членства в других ролях, собственных объектов, CREATE на БД и схему. Потом он ставит
  LOGIN с паролем и один раз входит ролью. На сервер уходит только SCRAM-verifier: пароля
  нет в `ps`, логах, истории shell и журнале PostgreSQL. В контейнер `migrate` он
  попадает через environment, как `POSTGRES_PASSWORD`.
- **api и worker** получают `INFRA_DB_DSN` с ролью `infra_pulse_app` и
  `application_name` `infra-pulse-api` / `infra-pulse-worker`. Worker под этой ролью
  миграции не применяет. Smoke деплоя открывает сессию из контейнеров `api` и `worker`
  и требует `infra_pulse_app` без суперпользователя.
- **Копии.** `backup` работает от владельца, `pg_dump` сохраняет и GRANT. `restore.sh`
  восстанавливает с `--no-owner --no-privileges`: роли общие для кластера и в дамп не
  входят. Этап `migrate` создаёт роль, если её нет, выдаёт права заново и ставит LOGIN.
  Smoke восстановления проверяет роль api и worker. Если в восстановленном `.env` нет
  пароля роли, `restore.sh` его сгенерирует.
- **Смена пароля:** удалить строку `INFRA_DB_APP_PASSWORD` из `.env` или вписать новое
  значение (24–128 символов `[A-Za-z0-9._~-]`), затем деплой. После `migrate` старые
  контейнеры не откроют новые соединения: ответы 503 идут до конца сборки и пересоздания
  api и worker (`up --build`). Без изменений кода это секунды, вместе с новым кодом —
  минуты сборки.

### Первый деплой на работающий стенд

Ручных шагов нет. Деплой дописывает `INFRA_DB_APP_PASSWORD` в `shared/.env`, `migrate`
создаёт роль и выдаёт права (секунды), `up` пересоздаёт api и worker с новым DSN. Пока
идёт `migrate`, работают прежние контейнеры под владельцем. Если `migrate` упал, они
продолжают работать, а run показывает причину.

### Проверка на сервере

```sh
cd /srv/infra-pulse/app
dcp() { docker compose -f compose.yaml -f deploy/compose.server.yaml \
  -f deploy/compose.public.yaml --env-file ../shared/.env "$@"; }   # публичный стенд
dcp exec db psql -U infra_pulse -d infra_pulse -c '\du'
# infra_pulse_app — без атрибутов (может входить), analytics_read — Cannot login
dcp exec db psql -U infra_pulse -d infra_pulse -c "SELECT usename, application_name, state
  FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
# worker держит соединение LISTEN: infra_pulse_app | infra-pulse-worker; api — на время запроса
dcp exec db psql -U infra_pulse -d infra_pulse -c '\dp audit_events'
# infra_pulse_app=ar/infra_pulse — только INSERT и SELECT
bash deploy/remote-deploy.sh --smoke-only --revision check   # smoke с проверкой роли
```

### Откат на владельца

Если после деплоя что-то не работает из-за прав, api и worker возвращаются к владельцу
одной строкой в `shared/.env`. Код остаётся тем же:

```sh
INFRA_DB_APP_DSN=postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
```

Затем деплой того же `ref`, workflow или `bash deploy/remote-deploy.sh --revision <sha>`.
Compose подставляет значения из того же `.env`. Деплой пишет WARNING, smoke проверяет
всё, кроме роли, worker снова применяет миграции при старте. Возврат к рантайм-роли —
удалить строку и повторить деплой. Откат кода на ref до 29.09 роль не требует: прежние
Compose-файлы подключают api и worker владельцем, а роль и её права остаются в БД
неиспользованными.

### Проверки 29.09.2026

macOS + Docker Desktop 28.3.2 (linux/arm64), Compose 2.38.2, PostgreSQL 17.11, синтетика;
отдельные Compose-проекты и `.env` вне checkout, после прогона всё удалено.

- `remote-deploy.sh` в режиме `received`, туннельный стенд. Версия от 28.09.2026 под
  суперпользователем, загрузка справочника и журнала, копия. Затем текущий код: пароль
  дописан в `.env` (в журнале деплоя его нет), `migrate` создал роль и проверил вход, api и
  worker подключены как `infra_pulse_app`, smoke OK. Новая загрузка дошла до `published`,
  worker пишет `import.status_changed`, CLI токенов работает в контейнере `api`.
- Откат строкой `INFRA_DB_APP_DSN`: api и worker под `infra_pulse`, WARNING, smoke OK;
  возврат — снова `infra_pulse_app`. Откат кода на версию от 28.09.2026 и обратно — smoke OK.
- Роль с членством в `infra_pulse`: `migrate` отказал (`not ready: member of infra_pulse`),
  деплой остановлен до `up`, контейнеры api и worker не пересоздавались, стек отвечал `ready`.
- `pg_dump` копии содержит GRANT роли. `restore.sh` в новый проект с `.env` без пароля роли:
  пароль сгенерирован, роль создана, 36 таблиц и суммы совпали, smoke с ролью OK, 29 с.
- Локальный `compose.yaml`: `migrate` → api и worker под `infra_pulse_app`, загрузка CSV
  через web — `published`.
- `make check-db` на свежей PostgreSQL 17 под ролью и владельцем, `make check`, shellcheck
  0.9.0 для скриптов `deploy/`, `docker compose … config` для серверного и публичного наборов
  со всеми профилями — пройдены. Прогон этого репозитория 29.09.2026: `make check-db` на
  PostgreSQL 17.11 — 117 passed под ролью, 116 passed и 1 skipped владельцем; `make check` —
  664 passed, 110 skipped; shellcheck 0.11.0 — без замечаний
  ([STATUS](../docs/STATUS.md#проверки-этого-репозитория-29092026)). В другом клоне или позже
  число тестов может быть другим — сверять код возврата и отсутствие failed.

Не проверено: сервер (x86_64), публичный режим с Caddy и lldap, smoke `/api/docs`.

## 6. Состояние и логи

```sh
cd /srv/infra-pulse/app
dc() { docker compose -f compose.yaml -f deploy/compose.server.yaml --env-file ../shared/.env "$@"; }
dc ps
dc logs -f --tail=200 api          # также web, db, worker, received-watcher
dc logs --since 1h web
docker inspect -f '{{index .Config.Labels "infra-pulse.revision"}}' "$(dc ps -q api)"
tail -n 20 ../shared/deploy-history.log
bash deploy/remote-deploy.sh --smoke-only --revision check   # повторить smoke без пересборки
```

Логи контейнеров ротируются: 5 файлов по 10 MB на контейнер.

## 7. Остановка без удаления данных

```sh
dc stop     # остановить; контейнеры и данные остаются
dc down     # удалить контейнеры и сеть; том PostgreSQL сохраняется
dc up -d    # поднять снова без пересборки
```

Нельзя: `dc down -v`, `docker volume rm|prune`, `docker system prune --volumes` —
они удаляют БД. Место от старых образов освобождает `docker image prune -f`
(только образы без тегов) и `docker builder prune --filter until=168h`.

## 8. Реальные данные: replay

Размещение выгрузки организаторов на этом сервере разрешено командой
26.09.2026 (D12 частично). Данные попадают на сервер только копированием в
`<DEPLOY_PATH>/data/`, никогда через Git или образ.

**Что нужно.** Исторический replay читает не сырые архивы, а **принятый curated
snapshot**, построенный локальным ETL (`data-science/`): `manifest.json` со статусом
`accepted`, `channels.parquet` и `curated/month=YYYY-MM/events.parquet` нужного месяца.
Загрузчик сверяет SHA-256 обоих Parquet с manifest и отказывается работать при
расхождении. Для ориентира: в локальном `curated-except-2021` месяц 2025-11 занимает
163 MB, весь snapshot — около 12 GB, поэтому копируются только нужные месяцы.
Сырые архивы и запуск ETL на сервере для этого не нужны и не подготовлены.

**Копирование** с рабочей машины, где лежит принятый snapshot (только нужный месяц):

```sh
SNAP=/path/to/accepted-snapshot     # локальный каталог snapshot
NAME=<snapshot-name>                # имя каталога на сервере
MONTH=2025-11
ssh deploy@<host> "install -d -m 755 /srv/infra-pulse/data/curated/$NAME"
rsync -av --partial \
  --include='/manifest.json' --include='/channels.parquet' \
  --include='/curated/' --include="/curated/month=$MONTH/" \
  --include="/curated/month=$MONTH/events.parquet" --exclude='*' \
  "$SNAP/" "deploy@<host>:/srv/infra-pulse/data/curated/$NAME/"
# Только чтение для всех: контейнер (uid 10001) читает, никто не пишет.
ssh deploy@<host> "chmod -R a=rX /srv/infra-pulse/data/curated/$NAME"
```

Фильтр проверен на макете snapshot в openrsync (macOS) и GNU rsync 3.5.0: переносятся
только три файла. Флаг `--chmod` не используется, так как openrsync его не поддерживает.
Для следующего месяца команда повторяется с новым `MONTH` (перед этим `chmod -R u+w`).

**Загрузка ограниченного окна** (на сервере, стек уже запущен в fixture). Окно
должно лежать внутри одного месяца по МСК; время доступности в replay симулировано
равным времени источника:

```sh
cd /srv/infra-pulse/app
dc run --build --rm replay-loader \
  --snapshot /data/curated/<snapshot-name> --month 2025-11 \
  --start 2025-11-06T08:30:00Z --end 2025-11-06T08:35:00Z
```

Контейнер `replay-loader` видит каталог данных только read-only (`/data`),
применяет идемпотентные миграции и печатает JSON-отчёт со `snapshot_id`, числами
исходных/загруженных строк и alarm. Повторный запуск того же окна дублей не создаёт.
Затем в `shared/.env`:

```sh
INFRA_MODE=replay
INFRA_REPLAY_SNAPSHOT_ID=<snapshot_id из отчёта>
```

и повторный запуск workflow с тем же `ref` (SPA пересобирается в режиме replay).
Smoke требует `/health/ready` = 200. Возврат к синтетике — `INFRA_MODE=fixture` и
повторный деплой; загруженные записи остаются в БД.

**Received.** Основной путь — загрузка CSV журнала и справочников через API и
`worker` ([ниже](#ingestion-worker-и-загрузка-csv)). Прежний путь JSON-партий
**кандидатного** формата ([backend](../backend/README.md)) через `received-watcher`
**устарел** и оставлен для совместимости: `COMPOSE_PROFILES=received`, затем атомарное
помещение файлов в `inbox/` (`cp x.json inbox/x.part && mv inbox/x.part inbox/x.json`).
Прямого канала заказчика нет (D07).

**Не реализовано:** запуск ETL/EDA/обучения на сервере (обучение только локально),
загрузка всей многолетней истории в PostgreSQL, монтирование model bundle.

## Ingestion worker и загрузка CSV

Статус 27.09.2026: код и тесты — implemented ([backend](../backend/README.md#загрузка-csv-и-ingestion-worker-b1));
после загрузки журнала worker пересчитывает и публикует прогноз (B2,
[backend](../backend/README.md#прогноз-обесточивание-фидеров-объекта-из-postgresql-b2-27092026));
с 28.09 `worker` работает на стенде (засев, режим `received`). Засев многолетней истории
(`backend/scripts/seed_forecast_history.py`, группа `data`) в деплой не встроен: без него
карточки появятся только после 365 сут загруженной истории объекта.

**Засев на стенде (выполнен 28.09.2026, до перехода в `received`).** Рабочий слой фазы
(только «Состояние фазы», справочники и окна `alarm-spike-exclusions-v1`, 145 МБ, вне
Git) копируется в `<DEPLOY_PATH>/data/stand-seed-v1/` (то есть в
`${INFRA_SERVER_DATA_DIR}/stand-seed-v1`, по умолчанию `/srv/infra-pulse/data`) с раскладкой
путей checkout: `replay-loader` монтирует `INFRA_SERVER_DATA_DIR` в `/data` только для чтения.
Засев идёт одноразовым контейнером образа `ops` (сервис `replay-loader`, данные — только
чтение), пока stream пуст; `-w /tmp` нужен DuckDB для временных файлов:

```sh
dc --profile tools run --rm -T -w /tmp --entrypoint sh replay-loader -c \
  'INFRA_DB_DSN="$INFRA_REPLAY_DSN" exec python /app/backend/scripts/seed_forecast_history.py \
     --data-root /data/stand-seed-v1 --namespace "$0" --stream "$1" --list-from 2023-01-01' \
  stand-received stand-received-1
```

Замер на стенде: 3 357 367 записей фазы 2019-01…2026-06, 95 объектов, 11 485 каналов —
загрузка 516 с, пересчёт 147 с, 1 727 карточек (как в локальной проверке). Затем
`INFRA_MODE=received` и деплой.

- Сервис `worker` — тот же образ backend, stage `worker`, команда
  `python -m infra_pulse_backend.worker`. Работает всегда, в любом `INFRA_MODE`,
  ровно один экземпляр (загрузки обрабатываются по порядку). Порты не публикует.
- API сохраняет файл в том `uploads` (`/app/var/uploads`), ставит задание в таблицу
  `jobs` и в той же транзакции пишет `import.uploaded` в `audit_events` (автор и роль —
  из сессии, в публичном режиме — пользователь каталога с ролью `admin`); отвечает 202.
  Worker разбирает CSV, пишет в received-scope
  `INFRA_RECEIVED_NAMESPACE`/`INFRA_RECEIVED_STREAM_ID` (те же значения у API) и
  ставит статус загрузки. Показ строк в очереди — при `INFRA_MODE=received`.
- Миграции до старта worker применяет сервис `migrate` от владельца; worker под
  рантайм-ролью `infra_pulse_app` их пропускает ([роли PostgreSQL](#роли-postgresql)).
  При старте он создаёт пустой received-scope, поэтому `/health/ready` в режиме received
  отвечает 200 и до первой загрузки.
- SIGTERM: завершение после текущего файла (`stop_grace_period: 30s`); при жёсткой
  остановке задание продолжается после истечения lease (300 с).
- Порядок демонстрации: `INFRA_MODE=received`, деплой, затем через UI или API
  загрузить `справочник_каналов_датчиков.csv` (`format=reference_channels_csv`), потом
  файлы журнала. Журнал до справочника каналов получает `failed/reference_missing`.
  С G1 те же файлы принимаются как `.xlsx` (первый лист), журнал — и в формате
  Приложения 1 ТЗ ([backend](../backend/README.md#xlsx-журнал-приложения-1-тз-audit-запросов-и-исследование-g1-27092026)).
- «Исследование» вне fixture: API читает `INFRA_RESEARCH_SUMMARY_PATH`, по умолчанию в
  `compose.server.yaml` — `/app/backend/research/research-summary.json`, файл из образа.
  После нового отчёта DS перед деплоем:
  `uv run --locked python backend/scripts/export_research_summary.py`, коммит JSON,
  деплой. Проверка на стенде: `curl -fsS http://127.0.0.1:8000/api/v1/research-summary`
  (роль analyst/admin) — версия `research-summary-v9-2026-09-27`, не 503.

```sh
dc logs -f --tail=100 worker
curl -fsS http://127.0.0.1:8000/api/v1/imports | head -c 800    # туннельный стенд, dev_stub
```

Лимит памяти 1.5 GiB: файл 50 MB (0.9 млн строк) локально дал пик RSS 1.05 GB.
Суточный CSV (170 тыс. строк) локально обработан за 4.3–4.6 с; при накоплении
истории скорость зависела от настроек PostgreSQL: с `shared_buffers=128MB`,
`max_wal_size=1GB` (по умолчанию образа) load рос до 9–17 с, с 512MB/4GB — 4.3–6.2 с.

**Настройки PostgreSQL на стенде.** В `compose.server.yaml` у `db` задано
`command: ["postgres", "-c", "shared_buffers=512MB", "-c", "max_wal_size=4GB"]`
(замер B1 выше, локальный PostgreSQL 14, не стенд). 512 MB укладываются в лимит `db`
3 GiB на сервере 6 vCPU / 11 GiB. WAL между checkpoint может занимать до ~4 GB диска в
томе `postgres_data` — учитывать при проверке свободного места. Действуют после
пересоздания контейнера `db` (деплой делает это сам при изменении конфигурации);
проверка: `dc exec db psql -U infra_pulse -d infra_pulse -c 'SHOW shared_buffers'`.
Замер p50/p95 на сервере не выполнялся; окончательная настройка — после профиля
нагрузки D08.

### Токены интеграции и поток через API (B1x)

Статус 29.09.2026: код и тесты — implemented; на стенде выпущен токен интеграции команды,
пачка до публикации на сервере не отправлялась; в Docker проверено локально (репетиция
приёма 29.09). Система-источник отправляет пачки JSON или XML (с 28.09) в
`POST /api/v1/observations` с `Authorization: Bearer ipk_…`. Дальше путь тот же, что у CSV:
том `uploads` → `import_files` (`journal_json`) → `worker`. Инструкция для заказчика — формат,
curl, лимиты и коды ответов — [docs/INTEGRATION_API.md](../docs/INTEGRATION_API.md).
Миграции `0015_integration.sql` и `0021_observation_xml.sql` (контейнер `xml` в отчёте
загрузки) применяет разовый сервис `migrate` от владельца схемы перед запуском api и worker;
worker под рантайм-ролью их пропускает ([роли PostgreSQL](#роли-postgresql)). Приём XML на
сервере ещё не проверялся.

Токен выдаёт администратор сервера в контейнере `api`: у него есть `INFRA_DB_DSN`.
Токен печатается один раз, в БД хранится только SHA-256. В `.env` и в репозиторий
токен не кладётся.

```sh
cd /srv/infra-pulse/app
dc exec api python -m infra_pulse_backend.admin --actor <администратор> token create --name scada-ods-1
dc exec api python -m infra_pulse_backend.admin --actor <администратор> token list [--all]
dc exec api python -m infra_pulse_backend.admin --actor <администратор> token revoke tok-…
dc exec api python -m infra_pulse_backend.admin --actor <администратор> token revoke --name scada-ods-1
```

В публичном режиме — то же через `dcp exec api …`. Каждое действие пишется в
`audit_events` (`integration_token.created` / `.listed` / `.revoked`), каждая принятая
пачка — `observations.received`. Отзыв действует со следующего запроса. Лимит
`INFRA_INTEGRATION_REQUESTS_PER_MINUTE` (60) — в серверном `.env`; счётчик живёт в
памяти процесса API.

Путь через Caddy не требует настройки: `Authorization` проходит через Caddy и nginx
без изменений, тело ограничено 55 MB на Caddy и 5 MiB в API.

## 9. Чего не делать

- Публиковать порты на `0.0.0.0` (кроме 80/443 Caddy в публичном режиме), открывать
  8080/8000/5432/3890/17170 в firewall, раздавать туннель через сторонние сервисы проброса.
- Класть данные, веса или `.env` в `app/`, в образ или в Git; `app/` перезаписывается
  `rsync --delete`, а сборочный контекст не должен содержать данных.
- Хранить секреты, IP, имя хоста или ключи в репозитории; только GitHub Secrets и
  серверный `.env`.
- Отключать проверку host key (`StrictHostKeyChecking=no`) или использовать личный
  ключ вместо отдельного ключа Actions.
- Включать `INFRA_ENABLE_LOCAL_REVIEWS` вне туннеля без входа (`INFRA_AUTH_MODE=dev_stub`);
  выдавать стенд за production.
- Открывать стенд наружу без `STAND_PUBLIC=true` или `--public`: без Caddy нет TLS и
  входа. Класть файл учёток lldap в `app/` или в репозиторий.
- Выполнять `down -v` и prune томов.
- Держать резервные копии только на этом VPS или в `app/` (его стирает `rsync --delete`);
  класть в копию серверный `.env` ([раздел 12](#12-резервные-копии-и-восстановление)).

## 10. Проверки этого набора

26.09.2026, macOS + Docker Desktop 28.3.2 (linux/arm64), Compose 2.38.2, отдельный
проект `infra-pulse-deploy-smoke`, порты 18080/18000, временный `.env` вне checkout:

- `docker compose -f compose.yaml -f deploy/compose.server.yaml --env-file deploy/.env.server.example config --quiet` — код 0; без `POSTGRES_PASSWORD` — ошибка интерполяции.
- `remote-deploy.sh`: отказ при `.env` с правами 644 и при плейсхолдере; fixture —
  сборка, `up --wait`, smoke OK; повторный деплой новой ревизии пересоздал api/web
  без пересоздания БД. Первая итерация smoke упала на собственной проверке
  публикации порта БД и вывела `ps -a` и логи с кодом 1; проверка исправлена.
- Replay на синтетическом accepted snapshot (3 записи): `run --build --rm replay-loader`
  с read-only `/data` загрузил 3/3 строки, 1/1 alarm, 3 канала справочника;
  деплой в replay: 11 миграций, readiness 200, через web 3 исходные записи с точными текстами.
- Received: watcher импортировал синтетическую партию из read-only `/inbox` (3 строки),
  readiness 200. `down` без `-v` и `up` сохранили 3 записи.
- GNU rsync 3.5.0 dry-run по `rsync-filter` на дереве с приманками: переданы только
  код и два шаблона `.env`; `.git`, `.github`, локальные каталоги инструментов, `.venv`, `.env*`,
  `node_modules`, `dist`, `data-science/*` кроме `pyproject.toml`, `var/`, `*.parquet` отфильтрованы.
- `actionlint` 1.7.12 для `deploy.yml` и `ci.yml`, `shellcheck` 0.11.0 для скрипта — без замечаний.

Это локальная проверка конфигурации и путей, не Linux x86_64 clean install (OPS-04),
не нагрузка OPS-01 и не restore OPS-02.

## 11. Публичный доступ

Требования: SEC-02 (вход через каталог), SEC-03 (audit), SEC-04 (TLS, секреты),
API-03/API-04 (решение); открытые D03 и D12 в
[реестре решений](../docs/REQUIREMENTS_DECISIONS.md). Территориальные scopes ролей
(SEC-01: район, ОДС, комплекс) — **planned**: роли действуют на весь стенд.

```text
интернет ──80/443──▶ caddy ──▶ web (nginx: SPA, /api, /health) ──▶ api ──▶ db
                     TLS, HSTS,                                     │
                     заголовки,                                     └──LDAP 3890──▶ lldap
                     /docs → 404
```

Порты на хосте публикует только `caddy`. У `web`, `api`, `db` и `lldap` портов нет,
в том числе на `127.0.0.1`: SSH-туннель к 8080/8000 в публичном режиме не работает.

**Документация API** — `https://<PUBLIC_HOST>/api/docs` (Swagger UI) и
`https://<PUBLIC_HOST>/api/openapi.json` при `INFRA_PUBLIC_DOCS=true` в серверном `.env`
(по умолчанию `false`). Публична только документация: каждый маршрут API по-прежнему
требует сессию или токен интеграции, без них — 401. Страницу и файлы Swagger UI отдаёт
сам API (swagger-ui-dist 5.33.0 из закреплённого пакета `fastapi-swagger`, без CDN),
запуск — внешним скриптом `/api/docs/swagger-init.js`, поэтому страница работает под
общей CSP сайта без исключения в Caddy. Корневые `/docs`, `/redoc`, `/openapi.json`
по-прежнему 404; ReDoc не отдаётся. «Authorize» и токен — в
[INTEGRATION_API](../docs/INTEGRATION_API.md#документация-api-swagger-ui). Выключение —
`INFRA_PUBLIC_DOCS=false` и деплой: API перестаёт отдавать `/api/docs*` и схему (404).

Отдельного доступа к документации только для команды нет: API отдаёт её лишь при
`INFRA_PUBLIC_DOCS=true`, и тогда она открыта и через Caddy.

Проверка после деплоя (с любой машины; ожидаемые коды — в комментариях):

```sh
H=https://<PUBLIC_HOST>
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' "$H/api/docs"           # 200 text/html
curl -sS "$H/api/openapi.json" | head -c 200; echo                                # {"openapi":"3.1.0",...
curl -sS -o /dev/null -w '%{http_code}\n' "$H/api/docs/swagger-ui-bundle.js"     # 200
curl -sS -D - -o /dev/null "$H/api/docs" | grep -i '^content-security-policy'     # script-src 'self', без unsafe-inline
curl -sS "$H/api/docs" | grep -Eo '(src|href)="[^"]*"'                           # только /api/docs/...
for p in /docs /redoc /openapi.json; do curl -sS -o /dev/null -w "$p %{http_code}\n" "$H$p"; done   # 404
for p in /api/v1/auth/me /api/v1/forecasts /api/v1/imports /api/v1/observations/x; do
  curl -sS -o /dev/null -w "$p %{http_code}\n" "$H$p"; done                        # 401
```
Режим включается `STAND_PUBLIC=true` в серверном `.env` (на него опирается workflow)
или флагом `remote-deploy.sh --public`. Без него скрипт откажется запускаться, если
на сервере уже работает публичный стек: иначе `--remove-orphans` молча остановил бы
Caddy и lldap.

### Однократная подготовка

1. **DNS.** Запись A (и AAAA, если есть IPv6) для выбранного имени указывает на сервер.
   Имя и адрес записываются только в серверный `.env`, не в репозиторий.
2. **Firewall.** Открыть наружу 80/tcp и 443/tcp. Порт 80 нужен для ACME HTTP-01 и
   перенаправления на HTTPS. Порты 8080, 8000, 5432, 3890 и 17170 не открывать.
3. **Серверный `.env`** (шаблон — блок «Public access» в
   [`.env.server.example`](.env.server.example)). Секреты генерируются на сервере
   (`openssl rand -hex 32`) и вписываются редактором:

   | Ключ | Значение |
   | --- | --- |
   | `STAND_PUBLIC` | `true` |
   | `PUBLIC_HOST`, `ACME_EMAIL` | имя стенда и контакт для Let's Encrypt |
   | `ACME_CA` | пусто — staging; production задаётся после успешного smoke |
   | `HSTS_MAX_AGE` | пусто — 300 с на время staging |
   | `LLDAP_JWT_SECRET`, `LLDAP_KEY_SEED`, `LLDAP_ADMIN_PASSWORD` | секреты каталога; пароль администратора lldap — не пароль пользователя приложения |
   | `LLDAP_BASE_DN` | необязательно, по умолчанию `dc=infrapulse,dc=local` |
   | `INFRA_SESSION_TTL_MINUTES` | необязательно, по умолчанию 480 |

4. **Деплой** — workflow с нужным `ref` или вручную
   `bash deploy/remote-deploy.sh --public --revision <sha>`. В публичном режиме
   миграции применяются в любом `INFRA_MODE`: таблицы сессий, решений и audit (0014).
5. **Учётки.** Файл `<DEPLOY_PATH>/shared/lldap-users.txt`, права 600, по строке на
   учётку — `имя:группы:отображаемое имя:пароль`. Группы — роли приложения:
   `dispatcher`, `analyst`, `admin`, несколько через запятую. Пароль — остаток строки,
   не короче 12 символов. Пример строки без настоящих значений:
   `jury-dispatcher:dispatcher:Жюри, диспетчер:<пароль>`.

   ```sh
   bash deploy/lldap/bootstrap.sh --check   # только проверка файла, без lldap
   bash deploy/lldap/bootstrap.sh           # группы и учётки в lldap
   ```

   Пароли идут через stdin в контейнер lldap, во время прогона лежат в его памяти
   (`/dev/shm`) и удаляются после него. На экран они не выводятся. Учётки, которых нет в файле,
   не удаляются. Изменение групп действует со следующего входа.
6. **Боевой сертификат.** После зелёного staging-smoke:
   `ACME_CA=https://acme-v02.api.letsencrypt.org/directory`, `HSTS_MAX_AGE=31536000`,
   повторный деплой. Smoke тогда проверяет цепочку сертификата без `--insecure`.
   HSTS с большим сроком включается только на боевом сертификате: браузер запомнит
   его на год.

### Smoke `--public`

Проверки идут с сервера через Caddy: `curl --resolve <PUBLIC_HOST>:443:127.0.0.1`.
В staging цепочка не проверяется (`--insecure`), остальное проверяется:

- порты публикует только `caddy`, 443/tcp опубликован;
- `/health/live` отвечает `alive` в режиме из `.env`; SPA отдаётся;
- `http://` → 308 на HTTPS; есть HSTS, `nosniff`, CSP и `X-Frame-Options: DENY`;
- TLS 1.0/1.1 не принимается;
- без сессии `/api/v1/auth/me`, `/api/v1/forecasts` и `/api/v1/imports` → 401;
  `/docs`, `/openapi.json` → 404; при `INFRA_PUBLIC_DOCS=true` `/api/docs` → 200;
- вход без `X-CSRF-Token` → 403; с заголовком и несуществующей учёткой `deploy-smoke-<pid>` →
  401. Это доказывает связь API с lldap: при недоступном каталоге ответ был бы 503.
  Попытка пишется в audit как `auth.login_failed`;
- не через Caddy, до этих проверок и в туннельном режиме тоже: api и worker (`compose exec`)
  открывают сессию PostgreSQL ролью `infra_pulse_app` без суперпользователя
  ([роли](#роли-postgresql)); при строке `INFRA_DB_APP_DSN` в `.env` — только WARNING.

### Вход, роли и сессии

| Роль (группа lldap) | Чтение: прогноз, журнал и его счётчики, схема, attention | «Исследование», P/R журнала | Решение по карточке, заметка | Загрузка и справочники |
| --- | --- | --- | --- | --- |
| `dispatcher` | да | нет | да | нет |
| `analyst` | да | да | нет | нет |
| `admin` | да | да | да | да |

Права проверяет сервер на каждом маршруте (`PERMISSIONS` в contract `auth.py`:
`read`, `research`, `decide`, `import`), тест —
[`test_rbac_matrix.py`](../backend/tests/test_rbac_matrix.py).

- Вход: `POST /api/v1/auth/login` с заголовком `X-CSRF-Token`; до входа подходит любое
  непустое значение. API выполняет bind в lldap от имени пользователя, группы
  становятся ролями.
- Ответ ставит две cookie со сроком сессии: `__Host-infrapulse-session`
  (HttpOnly, Secure, SameSite=Lax) и `__Host-infrapulse-csrf` (Secure, SameSite=Lax,
  читается SPA). Каждый POST передаёт значение второй в `X-CSRF-Token`, иначе 403
  `csrf_failed`.
- Коды ошибок: 401 `not_authenticated` / `session_expired` / `invalid_credentials`;
  403 `forbidden_role` / `no_role`; 429 `login_throttled` с `Retry-After` — 5 неудач
  на имя и 20 на адрес за 5 мин; 503 `directory_unavailable` или
  `auth_storage_unavailable`. Вход без проверки каталога невозможен.
- Сессии хранятся в PostgreSQL (`auth_sessions`, только SHA-256 токенов), выход их
  отзывает. Срок абсолютный, по умолчанию 8 ч.

### Audit и администрирование

```sh
dcp() { docker compose -f compose.yaml -f deploy/compose.server.yaml \
  -f deploy/compose.public.yaml --env-file ../shared/.env "$@"; }
dcp exec db psql -U infra_pulse -d infra_pulse -c "SELECT occurred_at, action, outcome,
  actor_id, actor_role, target_kind, target_id, request_id FROM audit_events
  ORDER BY occurred_at DESC LIMIT 20"
```

`audit_events` пополняется только вставкой; UPDATE и DELETE отклоняет триггер.
Сейчас в журнал пишутся вход, неудачный вход, отказ без роли, выход, решение,
черновик и загрузка файла (`import.uploaded`, B1). С B1x в журнал также попадают пачка API
(`observations.received`, автор `integration:<имя>`) и действия с токенами
(`integration_token.*`). С G1 — смены статуса загрузки (`import.status_changed`,
актор `worker:<id>`), просмотры и выгрузки XLSX (`forecast.card_viewed`,
`report.exported`…), отказы доступа (`request.rejected`) и сводки опросов раз в 15 мин
(`request.summary`); политика — [backend](../backend/README.md#xlsx-журнал-приложения-1-тз-audit-запросов-и-исследование-g1-27092026).
`X-Request-ID` ставит Caddy.

Веб-интерфейс lldap наружу не публикуется. Для администрирования — туннель к адресу
контейнера:

```sh
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(dcp ps -q lldap)"
ssh -N -L 17170:<адрес контейнера>:17170 deploy@<host>   # затем http://127.0.0.1:17170
```

Пароль администратора lldap меняется через `LLDAP_ADMIN_PASSWORD`, а не в UI: bootstrap
берёт его из окружения контейнера.

**Возврат к туннелю:** `STAND_PUBLIC=false`, затем `dcp stop caddy && dcp rm -f caddy`
и деплой. Контейнер lldap удалит `--remove-orphans`; тома `caddy_data` и `lldap_data`
сохраняются. Для публичного стенда стоит задать `DEPLOY_LOG_LINES=0` в переменных
workflow: хвост логов тогда не попадёт в журнал run.

### Проверки и ограничения (27.09.2026)

Проверено на macOS: `make check`; `make check-db` на одноразовом PostgreSQL 14
(миграции 0001–0014 дважды); `docker compose ... -f deploy/compose.public.yaml config`
с фиктивным env — порты опубликованы только у `caddy`; `shellcheck` 0.11.0 для трёх
скриптов. Ветвление `remote-deploy.sh` (отказы, выбор файлов, миграции в публичном
режиме) проверено с заглушкой `docker`. `lldap/bootstrap.sh --check` проверен на
8 случаях, `render-configs.sh` — вне Docker, с заглушкой вместо `/app/bootstrap.sh`.

Не проверено: запуск Caddy и lldap, `caddy validate`, выпуск сертификата, bind к
настоящему lldap, официальный `/app/bootstrap.sh`, CSP в браузере. Docker daemon на
машине проверки недоступен, к серверу подключения не было. Первый деплой — только
на staging. Ограничитель частоты входа живёт в памяти одного процесса API и
сбрасывается при рестарте.

**С 28.09.2026** публичный режим работает на стенде: Caddy и lldap запущены, сертификат
Let's Encrypt выпущен, вход администратором и аналитиком проверен в браузере
([STATUS](../docs/STATUS.md)). Каталог — тестовый lldap; раскладка каталога задана в
`backend/src/infra_pulse_backend/auth/ldap.py` (`uid=<логин>,ou=people,<base DN>`, группы
прямо в `ou=groups,<base DN>`, имена групп равны ролям). AD заказчика с этой раскладкой не
совместим без доработки — [этап 2](#этап-2--planned).

## 12. Резервные копии и восстановление

Требования: OPS-02 (MUST/P0: автоматические копии вне VPS, восстановление ≤ 4 ч) и
открытое D09 (RPO, срок хранения, площадка) в
[реестре решений](../docs/REQUIREMENTS_DECISIONS.md). Статус 29.09.2026:
*implemented* — [`backup.sh`](backup.sh), [`restore.sh`](restore.sh), сервис `backup`;
*на стенде* с 28.09 включён сервис ежедневной копии, проверены ручная копия 197 МиБ за
137 с и `backup.sh --check`; *проверено локально* — полный цикл без Docker
([замер](#замер-полного-цикла-27092026)) и `restore.sh` в Docker в новый проект
([проверки 29.09](#проверки-29092026)); *не проверено* — `restore.sh` на сервере и на объёме
стенда; *planned* — автоматическая копия вне VPS, шифрование копий, согласованный RPO.
`restore.sh` пересоздаёт БД командой `DROP DATABASE … WITH (FORCE)` (PostgreSQL 13+) и
рассчитан на поставляемый `postgres:17-alpine`.

### Что входит в копию

Одна копия — каталог `<DEPLOY_PATH>/backups/<YYYYMMDDTHHMMSSZ>[-метка]/`. Он пишется как
`<имя>.part` и переименовывается только после записи всех файлов.

| Файл | Содержимое |
| --- | --- |
| `db.dump` | `pg_dump -Fc` базы стенда: наблюдения, загрузки, справочники, решения, черновики, audit, сессии |
| `uploads.tar.gz` | файлы тома `uploads` (без недописанных `.incoming-*`) |
| `config.tar.gz` | `compose.yaml`, `deploy/compose.*.yaml`, `Caddyfile`, `.env.server.example`, `rsync-filter`, скрипты `deploy/` и `deploy/lldap/`; только из списка разрешённых файлов, без `.env` и `lldap-users*.txt` |
| `tables.tsv` | число строк каждой таблицы в дампе и SHA-256 отсортированных строк `audit_events`, `forecast_decisions`, `work_order_drafts`, `import_files` |
| `manifest.json` | ревизия кода (`app_revision`), версии PostgreSQL и `pg_dump`, размеры, SHA-256, длительность этапов |
| `SHA256SUMS` | контрольные суммы пяти файлов в формате `sha256sum -c` |

Счётчики `tables.tsv` читаются из самого дампа (`pg_restore --data-only`), поэтому
совпадают с ним точно. Это же доказывает, что архив читается. Файлы `uploads`
копируются после дампа: загрузки, пришедшие между ними, попадают в копию лишним файлом
без строки в БД.

**Не входит** и восстанавливается иначе:

- серверный `.env` и `lldap-users.txt` — хранит владелец стенда в менеджере секретов,
  не в копии и не в Git;
- учётки lldap (том `lldap_data`) — повторным `bash deploy/lldap/bootstrap.sh`;
- сертификаты (том `caddy_data`) — Caddy выпускает заново; лимит Let's Encrypt —
  5 одинаковых сертификатов в неделю;
- curated snapshot в `data/` — повторным копированием и сверкой manifest ([раздел 8](#8-реальные-данные-replay));
- образы — собираются из кода ревизии `app_revision`;
- роли PostgreSQL — общие для кластера, в дамп не входят. GRANT в дампе есть, но
  `restore.sh` их пропускает (`--no-privileges`). Роль `infra_pulse_app` и права заново
  создаёт этап `migrate`, он же ставит LOGIN с паролем из `.env`
  ([роли PostgreSQL](#роли-postgresql)).

**RPO.** При ежедневной копии теряется работа после последней копии: до 24 ч решений,
audit и загрузок. Допустимую потерю не согласовали (D09). Перед рискованной операцией
копия делается вручную.

### Включение (однократно)

```sh
install -d -m 700 /srv/infra-pulse/backups
id -u deploy; id -g deploy          # значения для BACKUP_UID / BACKUP_GID
```

В `shared/.env` (шаблон — блок «Backups» в [`.env.server.example`](.env.server.example)):

| Ключ | По умолчанию | Смысл |
| --- | --- | --- |
| `COMPOSE_PROFILES` | — | добавить `backup` (с received: `backup,received`) |
| `BACKUP_DIR` | `/srv/infra-pulse/backups` | каталог копий на хосте; должен существовать |
| `BACKUP_UID`, `BACKUP_GID` | `1000` | пользователь `deploy`: контейнер пишет копии от его имени |
| `BACKUP_AT` | `00:30` | время ежедневной копии, **UTC** (03:30 МСК) |
| `BACKUP_KEEP_DAYS` | `14` | копии старше удаляются... |
| `BACKUP_KEEP_MIN` | `3` | ...но последние N полных копий остаются всегда |

Затем обычный деплой. Сервис `backup` — образ `postgres:17-alpine`, как у `db`, поэтому
версия `pg_dump` совпадает с сервером. Сервис читает БД по сети Compose и том
`uploads` только на чтение; каталог копий — единственный том с записью. Если за
последние 24 ч копии нет, первая делается через 120 с после старта, дальше — ежедневно в
`BACKUP_AT`. Ошибка копии не останавливает цикл и не мешает `up --wait`: healthcheck у
сервиса нет. В логе каждой копии — время, размер и длительность этапов (строка из
локального замера ниже):

```text
[backup 2026-09-27T12:47:16Z] OK 20260927T124623Z: 293 MiB (307494118 bytes; dump 272860189, uploads 34608322 in 30 files), 53 s (dump 15 s, tables 34 s, uploads 3 s)
```

Ручная копия, например перед миграцией или откатом:

```sh
dc exec backup bash /app/deploy/backup.sh --once                  # сервис запущен
dc run --rm backup --once                                         # профиль не включён
```

### Ежедневная проверка

```sh
dc exec backup bash /app/deploy/backup.sh --check                 # код 0 — копия моложе 26 ч, SHA-256 верны
bash deploy/backup.sh --check --dir /srv/infra-pulse/backups      # то же с хоста, без БД
dc logs --since 26h backup | grep -E 'OK|ERROR|WARN'
df -h /srv/infra-pulse                                            # место под копии и WAL
```

На машине с копией вне VPS проверяется её свежесть и `sha256sum -c SHA256SUMS`. Раз в
месяц и после изменения схемы — учебное восстановление (ниже).

### Копия вне VPS (инструкция; скриптом не делается)

Копию забирает машина вне VPS (pull): сервер не хранит ключей к внешнему хранилищу, и
взлом стенда не удалит внешние копии. На внешней машине по cron, после `BACKUP_AT`:

```sh
rsync -a --partial --exclude='*.part' --exclude='.backup.lock*' \
  deploy@<host>:/srv/infra-pulse/backups/ /srv/offsite/infra-pulse/
for d in /srv/offsite/infra-pulse/*Z*/; do (cd "$d" && sha256sum -c --quiet SHA256SUMS) || echo "BAD $d"; done
```

Ключ для pull лучше ограничить чтением каталога копий: строка `authorized_keys` вида
`restrict,command="rrsync -ro /srv/infra-pulse/backups/" ssh-ed25519 ...` (`rrsync` из
пакета rsync; путь проверить `command -v rrsync`). Копии содержат решения, audit с
именами и адресами — внешний диск должен быть зашифрован. Шифрование самих копий
(`age`/`gpg`) и срок хранения вне VPS — решение D09. Пока pull не настроен, копия
лежит на том же диске, что и БД, и требование OPS-02 «вне VPS» не выполнено.

### Восстановление

```sh
bash deploy/restore.sh [--replace] [--public] [--skip-build] [--timeout SEC] [--revision SHA] <каталог копии>
```

Этапы и их время печатаются по ходу и в итоге; строка результата дописывается в
`shared/restore-history.log`.

1. `verify` — все файлы есть в `SHA256SUMS`, суммы совпадают; ревизия копии и кода
   сравниваются (при расхождении — предупреждение: миграции идут только вперёд).
2. `build` — образы api, worker, web, migrate (`--skip-build`, если уже собраны).
3. `db-start` — `db`; версия PostgreSQL не старше, чем в копии.
4. `target-check` — цель чистая: нет таблиц со строками и файлов в `uploads`. БД со
   схемой, но без строк, считается чистой и пересоздаётся. Иначе без `--replace` —
   отказ до остановки чего-либо: работающий стенд и его данные не меняются.
5. `stop` — api, worker, web, backup и received-watcher: во время замены никто не пишет;
   с `--replace` затем копия текущего состояния с меткой `pre-restore`.
6. `pg_restore` — одной транзакцией, `--exit-on-error --no-owner --no-privileges`, затем `ANALYZE`.
7. `verify-data` — число строк каждой таблицы равно `tables.tsv`; содержимое
   `audit_events`, решений, черновиков и загрузок совпадает по SHA-256. Проверка идёт
   до запуска api и worker, поэтому сравнение точное.
8. `uploads` — распаковка в том через контейнер worker (владелец файлов — пользователь приложения).
9. `migrate`, `start` — миграции этой ревизии, рантайм-роль `infra_pulse_app` (создаётся в
   новом кластере, получает права и LOGIN) и `up -d --wait` всего стека.
10. `smoke` — `/health/live` в режиме из `.env`; `/health/ready` = 200 в replay/received
    (в fixture он 503 по устройству); api и worker подключены ролью `infra_pulse_app`;
    `audit_events` после старта не меньше, чем в копии.

Ревизия контейнеров берётся из `--revision`, `APP_REVISION`, Git checkout или метки
работающего api; на новом сервере без Git её стоит передать явно (`--revision <app_revision>`).

**Потеря сервера (новый VPS).**

1. Подготовить сервер по [разделу 1](#1-подготовка-сервера-однократно) и создать `backups/`.
2. Восстановить `shared/.env` (и `lldap-users.txt`) из менеджера секретов, `chmod 600`.
   Пароль БД задаётся новому тому при первом старте, прежний не обязателен. То же с
   `INFRA_DB_APP_PASSWORD`: если строки нет, `restore.sh` сгенерирует новое значение.
3. Доставить код ревизии `app_revision` из `manifest.json` только rsync-командой
   [раздела 3](#3-первый-запуск-fixture), **без** `remote-deploy.sh`: первый деплой уже
   пишет строки (received-scope worker, audit smoke), и цель перестаёт быть чистой.
   Если workflow уже запускали — восстанавливать с `--replace`.
4. Скопировать копию с внешней машины:
   `rsync -a /srv/offsite/infra-pulse/<копия>/ deploy@<новый host>:/srv/infra-pulse/backups/<копия>/`.
5. `bash deploy/restore.sh --revision <app_revision> /srv/infra-pulse/backups/<копия>`.
   Скрипт сам собирает образы и поднимает стек; публичный режим берёт из `STAND_PUBLIC` в `.env`.
6. Публичный стенд: DNS на новый адрес, затем `bash deploy/lldap/bootstrap.sh`; Caddy
   выпустит сертификат сам.
7. Grafana (профиль `analytics`): логин `grafana_reader` не входит в дамп — создать его
   заново `bash deploy/grafana/create-reader.sh` ([раздел 13](#13-grafana-профиль-analytics)).
8. Справочники каналов, объектов и состояний восстанавливаются из копии вместе с БД. Если они
   не были загружены до копии — загрузить их под администратором до первой загрузки журнала
   или пачки API: без справочника каналов журнал получает `failed/reference_missing`.
9. Проверить UI и через сутки — `backup.sh --check`.

**Порча данных на работающем сервере:** `bash deploy/restore.sh --replace <копия>`.
Сначала делается копия `-pre-restore` текущего состояния, затем БД пересоздаётся, файлы
`uploads` распаковываются поверх. Во время восстановления сервис `backup` остановлен, а
копия `pre-restore` делается без ротации, поэтому исходная копия не удаляется.

**Учебное восстановление без остановки стенда** — отдельный проект Compose с новыми томами:

```sh
cp ../shared/.env ../shared/.env.drill && chmod 600 ../shared/.env.drill
# В .env.drill: WEB_PORT=18080, API_PORT=18000, STAND_PUBLIC=false, COMPOSE_PROFILES без backup.
COMPOSE_PROJECT_NAME=infra-pulse-drill INFRA_SERVER_ENV_FILE=../shared/.env.drill \
  bash deploy/restore.sh ../backups/<копия>
# Удаление учебного стека — только с явным именем проекта и томов:
COMPOSE_PROJECT_NAME=infra-pulse-drill docker compose -f compose.yaml -f deploy/compose.server.yaml \
  --env-file ../shared/.env.drill down
docker volume rm infra-pulse-drill_postgres_data infra-pulse-drill_uploads
rm ../shared/.env.drill
```

Учебный стек занимает те же лимиты CPU/RAM, что и основной (до 5 vCPU / 5.8 GiB), поэтому
его запускают вне показов.

### Замер полного цикла, 27.09.2026

**Среда.** macOS 26.6 arm64 (14 CPU, 36 GB), Homebrew PostgreSQL 14.20 на свободном
порту, `shared_buffers=512MB`, одноразовый каталог данных. Docker daemon недоступен,
поэтому `restore.sh` шёл через заглушку `docker`: вызовы `compose exec db` и
`compose run worker` выполнялись локально теми же командами (`psql`, `pg_restore`, `tar`),
`migrate` — `backend/scripts/migrate_operational_db.py`, `up` — локальный uvicorn в
режиме `received`. `backup.sh` запускался напрямую с переменными libpq.

**Данные (синтетика, объём месяца).** 5 100 000 строк `dispatch_observations`
(30 дней × 170 тыс., received-scope создан кодом B1), 30 файлов CSV в `uploads`
(200 MiB), 30 `import_files`, 6 000 `audit_events`, 600 решений, 172 черновика. БД на
диске — 5 457 MiB.

| Этап | Время, с |
| --- | ---: |
| `backup.sh --once`: dump 15, счётчики и суммы из дампа 34, uploads 3 | 53 |
| «Уничтожение»: остановка PostgreSQL, удаление каталога данных и `uploads` | 2 |
| `restore.sh` на чистый каталог: verify 0, db-start 1 (initdb + старт), target-check 0, stop 0, pg_restore + ANALYZE 40, verify-data 1, uploads 0, migrate 0, start 0, smoke 1 | 43 |
| **Цикл «копия → уничтожение → восстановление → smoke»** | **98** (цель ≤ 14 400) |

Размер копии — 293 MiB: `db.dump` 260 MiB, `uploads.tar.gz` 33 MiB, конфигурация 24 KiB.
После восстановления совпали строки всех 18 таблиц и SHA-256 содержимого четырёх
ключевых; `/health/ready` = 200 с 5,1 млн строк received-scope; `audit_events` 6 000.

Дополнительные прогоны на тех же данных:

- `--replace` поверх заполненной БД — 93 с (копия `pre-restore` 50 с, pg_restore 40 с).
  Строка audit, добавленная после копии, есть в `pre-restore` (6 001) и отсутствует в
  восстановленной БД (6 000).
- Цель со схемой без строк (после миграций) — БД пересоздана без `--replace`, 43 с.
- Отказы с кодом 1: заполненная БД без `--replace` — на `target-check`, API продолжал
  отвечать `ready`; `tables.tsv` с неверным числом строк audit (суммы пересчитаны) — на
  `verify-data`; малая копия с дописанным байтом в `uploads.tar.gz` — на `verify`, до
  вызовов, меняющих стек.
- `backup.sh`: ротация (14 дней, минимум 3 и 1), удаление `.part`, `--check` на
  повреждённой копии и пустом каталоге, ошибки подключения и отсутствующего `uploads`
  (код ≠ 0, `.part` удалён), цикл `--loop` с копией в `BACKUP_AT` и остановкой по SIGTERM.

**Не измерено:** сборка образов (`--skip-build`), запуск контейнеров и `compose run`,
ввод-вывод Docker volume, BusyBox-утилиты образа `postgres:17-alpine` в контейнере
`backup`, сервер 6 vCPU x86_64, передача копии по сети с внешней машины. Этапы с данными
заняли 98 с из 4 ч; время сборки и доставки на сервере нужно замерить при первом
восстановлении там. Это не проверка RPO: допустимую потерю данных задаёт D09.

## 13. Grafana (профиль `analytics`)

Требование OPS-06 (SHOULD/P2): аналитик смотрит сигналы датчиков во времени. Grafana
не заменяет журнал и карточки продукта (API-05) и действий не выполняет.

```text
браузер ──443──▶ caddy ──/grafana/*──▶ grafana ──LDAP 3890──▶ lldap
                                          │
                                          └──5432, логин grafana_reader ⊂ analytics_read──▶ db: схема analytics (view)
```

**Что показывает.** Оба дашборда лежат в папке InfraPulse. Стартовый дашборд Grafana
(`GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH`) — «Сигналы: текстовые состояния»: на стенде
числовых каналов нет, числовой дашборд пишет это в верхней панели. Окно по умолчанию у обоих —
21.05.2026 00:00 … 01.06.2026 23:59:59 МСК (в нём на стенде видны переходы «Обесточен» /
«Норма»; замечание владельца 29.09), время — Москва. Выбор: область данных и поток, объект, тип датчика, канал. ID в JSON
не зашиты. В фильтрах только объекты и каналы, у которых есть записи в выбранной
области. На числовом дашборде тип датчика и канал дополнительно отбираются по числовым
записям в окне. Если подходящих нет, в фильтре стоит строка «— нет … —», а не ошибка.
По умолчанию выбраны все типы и каналы первого объекта.

- Строка «Область данных» показывает первую и последнюю запись области и их число.
  Там же — число записей выбранных каналов в окне. Если оно выше лимита панели, стоит
  «больше 20 000» (на числовом дашборде — 50 000).
- Ссылка в дате последней записи открывает последние 7 сут данных области (на стенде —
  23.06–30.06.2026).
- «Сигналы: числовые значения» — `time series`: канал и время → значение (газ,
  температура и другие числа). Отдельная панель на каждый тип датчика, потому что у
  типов разные шкалы. Ниже таблица последних 500 записей с исходным текстом значения.
- «Сигналы: текстовые состояния» — `state timeline`: канал и время → текст значения
  дословно. Ниже таблица текстов с порядком по справочнику состояний и сам справочник
  выбранных типов. Цвет помогает различать частые тексты; «Неисправен» — оранжевый,
  с подписью «признак, не подтверждённая поломка». Прочие тексты серые, подпись та же.
- На обоих — аннотации «alarm источника»: записи с `тревожное = true`. Это сообщение
  источника, а не подтверждённый отказ.
- Пустая панель пишет причину: нет записей выбранных каналов в окне, у каналов только
  текст или справочник состояний не загружен. На `state timeline` вместо этого одна
  серая строка «— нет текстовых записей в окне —».
- Объём ответа ограничен последними записями окна: 20 000 на `state timeline`,
  50 000 на панель `time series`, 2 000 аннотаций. Ограничение указано в заголовке или
  описании панели.

**Данные и права** (миграции [0016](../backend/migrations/0016_analytics.sql) и
[0020](../backend/migrations/0020_analytics_dashboards.sql)).

| View `analytics.*` | Содержание |
| --- | --- |
| `signals` | Все записи `dispatch_observations` в длинном формате: область, время, объект, тип, канал, `value_raw` дословно, `value_numeric`, `source_alarm`, флаги качества |
| `signal_numeric`, `signal_state`, `source_alarms` | Числа (без заглушки 1970); тексты с `state_order` по справочнику состояний; записи с `alarm = true` |
| `channels`, `objects` | Каналы и объекты для фильтров и названий: сначала действующий справочник (0012), для остальных — раскладка прогноза `forecast_channel_layout` и `forecast_objects` (0013) |
| `states`, `channel_versions`, `scopes` | Действующий справочник состояний, версии справочника каналов, области данных |

- Pivot не нужен: серии по каналам Grafana строит сама.
- Засев истории на стенде (`seed_forecast_history.py`) справочники B1 не загружает, а
  заполняет только раскладку прогноза. Поэтому 0020 берёт каналы и объекты и из неё.
  Worker пересобирает раскладку из каждого нового справочника каналов, так что после
  загрузки справочника источники совпадают. Правила неоднозначного канала — как в 0016.
- 0020 добавляет два индекса `dispatch_observations`: по времени события внутри области
  (`$__timeFilter`, последние записи окна, первая и последняя запись области) и частичный
  по числовым записям (числовой дашборд и его фильтры не читают текстовые записи).
  Индексы строятся один раз при применении миграции и на это время блокируют запись в
  таблицу. Локально на 3,36 млн записей построение заняло около 3 с, индекс по времени —
  101 МБ.
- Файлы миграций применяются каждый раз по порядку в одной транзакции. 0016 заново
  создаёт свои `channels` и `objects`, и 0020 сразу их заменяет. Столбцы и права те же.
- Роль `analytics_read` (NOLOGIN) получает USAGE и SELECT только на схему `analytics`.
  К `dispatch_observations`, импорту, справочникам, сессиям, решениям, черновикам,
  audit и прогнозам доступа у неё нет. View выполняются с правами владельца.
- Миграция идемпотентна: создаёт роль, если её нет, и каждый раз заново выдаёт права.
  Поэтому после восстановления копии без ролей (`pg_restore` без глобальных объектов)
  достаточно применить миграции и запустить `create-reader.sh`.
- Если у пользователя миграций нет права CREATEROLE, миграция создаёт view, оставляет
  предупреждение, и роль создаёт DBA. На стенде миграции применяет `POSTGRES_USER` —
  суперпользователь образа postgres, поэтому роль создаётся сразу. api и worker этим
  пользователем не подключаются ([роли PostgreSQL](#роли-postgresql)).

**Запуск.**

1. В серверном `.env` (шаблон — блок «Grafana» в [`.env.server.example`](.env.server.example)):
   `COMPOSE_PROFILES=analytics`, `GRAFANA_DB_PASSWORD` — вывод `openssl rand -hex 32`,
   `GRAFANA_DB_USER` — необязательно, по умолчанию `grafana_reader`.
2. Деплой в публичном режиме. Миграции, в том числе 0016 и 0020, применяются в публичном
   режиме всегда (сервис `migrate` до `up`). Первое применение 0020 строит два индекса по
   `dispatch_observations`; пока они строятся, запись в таблицу ждёт.
3. Логин PostgreSQL для Grafana (повторный запуск меняет пароль на значение из `.env`):

   ```sh
   bash deploy/grafana/create-reader.sh --check   # только проверка .env
   bash deploy/grafana/create-reader.sh           # роль-член analytics_read в db
   dcp restart grafana                            # если пароль изменился
   ```

   Скрипт отказывается работать с ролью с лишними правами, членством в других ролях
   или собственными объектами. На логине заданы режим «только чтение», `statement_timeout`
   10 с, `idle_in_transaction_session_timeout` 60 с и лимит 5 соединений. Настройки
   групповой роли не наследуются, поэтому они заданы на самом логине. Пароль передаётся
   в `psql` через stdin и не печатается.
4. Вход: `https://<PUBLIC_HOST>/grafana/`, учётка lldap. Ссылка «Графики сигналов» в меню
   интерфейса (аналитик и администратор) появляется, если в серверном `.env` задано
   `INFRA_GRAFANA_LINK=1`: флаг передаётся в сборку SPA (`VITE_GRAFANA_LINK`) при деплое.
   Ссылка открывает `/grafana/d/infrapulse-signals-state` за 21.05–01.06.2026 МСК
   (`GRAFANA_SIGNALS_URL` в `frontend/src/shared/config/routes.ts`).

**Доступ.**

| Группа lldap | Grafana |
| --- | --- |
| `analyst` | Viewer: дашборды и фильтры, без правки |
| `admin` | Admin |
| только `dispatcher` | вход отклоняется |

- Решение по диспетчеру: Grafana — инструмент аналитика, диспетчер работает в журнале
  и карточках продукта.
- Нет анонимного доступа, локального администратора, регистрации, Basic auth, Explore,
  snapshots (в том числе внешних), публичных дашбордов и alerting.
- Выключены телеметрия, проверка обновлений, лента новостей, каталог и предустановка
  плагинов.
- Порт наружу не публикуется: только Caddy `/grafana/` → `grafana:3000`, префикс
  снимает `handle_path`.
- Для `/grafana/*` Caddy ставит свою CSP: страница Grafana требует inline-скрипты и
  web workers. У остального сайта CSP прежняя.
- Дашборды и источник данных берутся только из Git (provisioning, правка в UI не
  сохраняется). Состояние Grafana (учётки из LDAP, сессии) лежит в томе `grafana_data`;
  данных сигналов в нём нет.
- Без профиля `analytics` путь `/grafana/` отвечает 502.

**Ограничения.**

- Viewer может отправить произвольный SQL через API источника данных. Защита — права
  БД: только `analytics`, транзакции только чтения, таймаут. Проверено: запросы к
  `dispatch_observations` и `audit_events` отклоняются, `CREATE TABLE` тоже.
- `default_transaction_read_only` пользователь может сменить в сессии. На PostgreSQL
  15+ (стенд — 17) права CREATE в схеме `public` у PUBLIC нет. На PostgreSQL 14 оно
  есть по умолчанию.
- Состояние до начала окна в `state timeline` не показывается.
- Если в окне записей больше лимита панели, видны только последние. Начало окна на
  `state timeline` тогда пустое — нужно сузить окно или выбрать меньше каналов.
- Таблица «Тексты за период» считает все записи окна без лимита. За всю историю
  области она читает всю таблицу: локально на 3,36 млн записей 0,7–0,8 с для 1–3 объектов
  и 1,7 с для всех 19 объектов с записями. Потолок — `statement_timeout` 10 с.
- В фильтрах только каналы, известные справочнику или раскладке прогноза. Записи
  канала, которого нет ни там, ни там, на дашбордах не выбираются.
- В фильтре типов текстового дашборда есть типы со всеми записями, включая только
  числовые. Для такого типа `state timeline` покажет строку «— нет текстовых записей
  в окне —».
- Названия из раскладки прогноза хранятся с нормализованными пробелами, не дословно.
  Тексты значений всегда дословные.
- Совпадение текста со справочником состояний — по типу датчика и тексту: связи
  набора состояний с каналом в справочнике нет.
- Агрегаты `events_daily`, качество прогноза и решения — planned.

**Проверки 27.09.2026** — синтетика, macOS + Docker Desktop 28.3.2 (linux/arm64),
отдельный Compose project, env вне checkout.

- `make check-db` на одноразовом PostgreSQL 14: миграции применяются дважды;
  [`test_analytics_db.py`](../backend/tests/test_analytics_db.py) проверяет view,
  отказ `analytics_read` на всех операционных таблицах и логин Grafana.
- `docker compose … -f deploy/compose.public.yaml --profile analytics config` проходит;
  без профиля и без `GRAFANA_*` тоже.
- Стек `db` (PostgreSQL 17) + `lldap` + `grafana` + `caddy`:
  - учётки созданы `lldap/bootstrap.sh`, логин — `create-reader.sh` (дважды);
  - через Caddy: analyst → Viewer, admin → Admin, dispatcher и неверный пароль → 401;
  - `/api/snapshots` и публичные дашборды → 404;
  - все панели и аннотации обоих дашбордов → 200;
  - CSP Grafana только на `/grafana/*`.
- `EXPLAIN ANALYZE` от логина Grafana на 1,44 млн записей и справочнике 11 508 каналов
  в двух версиях:

  | Запрос | Время |
  | --- | --- |
  | один канал за 30 сут, index scan `dispatch_observations_channel_idx` | 1,6 мс |
  | 20 каналов за 30 сут | 59 мс |
  | фильтр типов датчиков объекта | 22 мс |

  До перестройки view фильтр типов занимал 4,4 с. Всё укладывается в `statement_timeout` 10 с.

**Исправления 28.09.2026 по стенду.** На стенде засеяно только «Состояние фазы»:
3 357 367 записей, справочники B1 не загружены.

- У фильтров «Объект», «Тип датчика», «Канал» были предупреждения. Справочник каналов
  был пуст, запрос объектов возвращал 0 строк. Источник PostgreSQL в Grafana 12 отдаёт
  пустой результат без полей, и фильтр падает с «at least one field expected for
  variable».
- Пустой фильтр с несколькими значениями подставлялся как `IN ()`. Отсюда `syntax error
  at or near ")"` (42601) в таблице «Тексты за период» и в запросах фильтров.
- Числовой дашборд показывал «No data»: числовых каналов на стенде нет, была только
  пустая панель.
- Что изменено:
  - списки подставляются как `= ANY(ARRAY[...]::text[])`, пустой выбор — это
    `ARRAY[]::text[]`;
  - фильтр без вариантов возвращает строку-заглушку «— нет … —»;
  - каналы и объекты берутся и из раскладки прогноза (0020);
  - у панелей есть текст причины и лимиты строк;
  - добавлены индексы 0020.

**Проверки 28.09.2026** — macOS (Apple silicon), PostgreSQL 14, одноразовые БД.

- Засев той же истории (`seed_forecast_history.py`, 3 357 367 записей, 131 с).
  Grafana 12.4.11 того же digest в Docker, provisioning из репозитория, логин из
  `create-reader.sql`.
- До исправления воспроизведены предупреждения фильтров и ошибка 42601.
- После исправления все запросы фильтров, панелей и аннотаций прошли через
  `/api/ds/query` без ошибок: 2 дашборда × 3 окна × 3 выбора (все, один, пусто).
- В браузере: предупреждений у фильтров нет, пустые панели пишут причину. Ссылка
  «Последние 7 сут данных области» открывает 23.06–30.06.2026 и сохраняет объект.
  Префикс `/grafana/` Grafana добавляет сама: проверено с `GF_SERVER_ROOT_URL=…/grafana/`
  и `serve_from_sub_path=true`.
- `EXPLAIN (ANALYZE, BUFFERS)` от логина Grafana, повторный прогон с тёплым кэшем.
  «До» — те же каналы, взятые из раскладки, потому что прежние фильтры на этих данных
  пусты. Один объект — 109 каналов с записями, 828 632 записи; три объекта — 241 канал.

  | Запрос | До | После |
  | --- | --- | --- |
  | `state timeline`, 1 объект, 30 сут | 3,5 мс, 1 306 строк | 7,5 мс, 1 306 строк |
  | `state timeline`, 1 объект, 365 сут | 183 мс, 21 479 строк | 101 мс, 20 000 строк |
  | `state timeline`, 1 объект, вся история | 620 мс, 828 632 строки | 96 мс, 20 000 строк |
  | `state timeline`, 3 объекта, вся история | 976 мс, 1 539 805 строк | 20 мс, 20 000 строк |
  | «Тексты за период», 1 объект, 365 сут | 209 мс | 18 мс |
  | «Тексты за период», 3 объекта, вся история | 2 369 мс | 802 мс |
  | числовые панели, окно 365 сут и больше | 161–175 мс (чтение всей таблицы) | < 1 мс |
  | фильтры объекта, типа, канала | 0 строк и предупреждение | 12, 6–8, 6–7 мс |
  | «Область данных» (первая и последняя запись, записи в окне) | — | 2–12 мс |

- Прогон по всем 19 объектам с записями (окна 30, 365 сут и вся история, текстовые
  панели): медиана 18 мс. Максимум 830 мс — на холодном кэше, повторно 158 мс.
- PostgreSQL 17.11 (`postgres:17-alpine` в Docker, `shared_buffers=512MB`, та же
  история) даёт ту же картину:
  - `state timeline` за всю историю: 1 объект 1 024 → 81 мс, 3 объекта 1 440 → 25 мс;
  - «Тексты за период», 3 объекта, вся история: 2 706 → 965 мс;
  - числовые панели: 193–204 мс → < 1 мс;
  - «Область данных» — до 20 мс;
  - индексы 0020 на 3,36 млн записей строятся за 1,7 с;
  - Grafana через `/api/ds/query` — 0 ошибок;
  - `test_analytics_db.py` — 13 passed.
- `make check` — 812 passed, 89 skipped (DB-тесты без DSN; прогон 28.09 в репозитории
  команды, [как читать числа](../docs/STATUS.md#как-читать-числа-проверок)).
- `make check-db` — 96 passed. В нём
  [`test_analytics_db.py`](../backend/tests/test_analytics_db.py): статическая проверка
  JSON (нет `IN (${…})`, у фильтров есть заглушка, у панелей — текст причины) и
  выполнение каждого запроса дашбордов от логина Grafana. Выборы: все, один, пусто,
  несуществующая область, окно без чисел.
- `make compose-server-config` проходит.

Не проверено: запуск на сервере, выпуск сертификата, корпоративный LDAP. Исправления
28.09 не проверены на стенде и за Caddy. На стенде `serve_from_sub_path=false`, префикс
снимает Caddy.

## Этап 2 — planned

**Публичный доступ** реализован в конфигурации 27.09 — [раздел 11](#11-публичный-доступ).
Остаются planned: территориальные scopes ролей (SEC-01), корпоративный LDAP/AD вместо
тестового lldap, внешнее сканирование TLS после выпуска сертификата. Для AD в настройки
нужно вынести шаблон bind (UPN `user@domain` или `DOMAIN\user` вместо
`uid=<логин>,ou=people,<base DN>`), фильтр поиска пользователя (`sAMAccountName`), базу групп
и соответствие «группа AD → роль»; сейчас это задано в коде.

**Резервные копии (D09, OPS-02)** реализованы 27.09 — [раздел 12](#12-резервные-копии-и-восстановление).
Остаются planned: автоматическая копия вне VPS с проверкой, шифрование, согласованные
RPO и срок хранения, замер восстановления на сервере, вариант `restore.sh` для
PostgreSQL 12 (без `WITH (FORCE)`, через `pg_terminate_backend`).

**Grafana (OPS-06, SHOULD/P2).** Сигналы во времени реализованы 27.09 —
[раздел 13](#13-grafana-профиль-analytics). Остаются planned агрегаты
`events_daily`, `coverage_daily`, качество прогноза и решения из раздела «Аналитический
слой и Grafana» плана выдачи прогноза (R18, OPS-06).

**Прочее.** Inference worker и read-only mount bundle `/models` (отдельного
inference-сервиса нет, прогноз пересчитывает worker); release manifest с digest образов (DELIV-01, D10);
проверка `compose.server.yaml` в CI; мониторинг диска/памяти хоста; окончательная
настройка PostgreSQL после профиля нагрузки D08.

### Бюджет ресурсов (6 vCPU / 11 GiB)

| Сервис | CPU limit | RAM limit | Когда работает |
| --- | --- | --- | --- |
| db | 2.0 | 3 GiB | всегда |
| api | 1.5 | 1 GiB | всегда |
| web | 0.5 | 256 MiB | всегда |
| worker | 1.0 | 1.5 GiB | всегда (загрузка CSV, B1) |
| caddy | 0.5 | 256 MiB | публичный режим (B3) |
| lldap | 0.5 | 256 MiB | публичный режим (B3) |
| received-watcher | 0.5 | 512 MiB | профиль `received`, устарел |
| migrate | 1.0 | 512 MiB | однократно при каждом деплое и восстановлении: миграции и LOGIN рантайм-роли |
| replay-loader | 2.0 | 2 GiB | однократно по команде (DuckDB ограничен 1 GB, 2 потока) |
| backup | 0.5 | 512 MiB | профиль `backup`; работает раз в сутки, остальное время ждёт |
| grafana | 0.5 | 512 MiB | профиль `analytics` в публичном режиме (OPS-G) |

Постоянные лимиты — 5 vCPU и около 5.8 GiB, в публичном режиме 6 vCPU и около
6.3 GiB, с профилями `backup` и `analytics` — до 7 vCPU и около 7.3 GiB; это потолки, а
не измеренное потребление (CPU-лимиты больше числа ядер допустимы). У PostgreSQL заданы
`shared_buffers=512MB` и `max_wal_size=4GB` ([выше](#ingestion-worker-и-загрузка-csv)),
остальное — настройки образа по умолчанию.
