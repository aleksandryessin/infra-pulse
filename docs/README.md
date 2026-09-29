# Как читать документацию

Требования и подтверждения их выполнения — [REQUIREMENTS_SPEC](REQUIREMENTS_SPEC.md), готовность —
[STATUS](STATUS.md), устройство — [ARCHITECTURE](ARCHITECTURE.md). Датированные записи
остаются историей проверки и не переписываются под новый результат.

| Вопрос | Документ |
| --- | --- |
| Как проверить решение за 10 минут и что видно на стенде? | [README: как проверить за 10 минут](../README.md#как-проверить-за-10-минут) |
| Что реально работает и что проверено? | [STATUS](STATUS.md) |
| Как работать в интерфейсе диспетчеру, аналитику и администратору? | [Руководство пользователя](USER_GUIDE.md) |
| Как установить и запустить? | [README: установка и запуск](../README.md#установка), [DEPENDENCIES](DEPENDENCIES.md), [OPERATIONS](OPERATIONS.md#локальный-старт) |
| Как развернуть стенд: вход, копии, Grafana? | [Runbook стенда](../deploy/README.md), [копии и восстановление](../deploy/README.md#12-резервные-копии-и-восстановление) |
| Какая нагрузка проверена? | [OPERATIONS](OPERATIONS.md#нагрузка-с-опросом-сейчас-на-локальной-копии-стенда-29092026), [STATUS](STATUS.md) |
| Как передать данные: история CSV/XLSX и поток через API с токеном? | [INTEGRATION_API](INTEGRATION_API.md): столбцы справочников и журнала, синтетические примеры [`examples/`](examples/), [сквозная проверка за 5 минут](INTEGRATION_API.md#сквозная-проверка-за-5-минут), формат пачки, коды ответов, токен; интерактивная документация API на стенде — `/api/docs` |
| Как разделены API, worker, core и исследование? | [ARCHITECTURE](ARCHITECTURE.md) |
| Какие персональные данные хранятся (149-ФЗ, 152-ФЗ)? | [ARCHITECTURE](ARCHITECTURE.md#персональные-данные-и-защита-информации-149-фз-152-фз) |
| Какие входные данные и ограничения? | [DATA](DATA.md) |
| Как устроен интерфейс? | [UI](UI.md), [дизайн-система](design/SYSTEM.md) |
| Почему прогноз обесточивания (в исследовании — «Отказ датчика») и почему в продукте статичный список? | [Выбор направления](DIRECTION_DECISION_2026-09-26.md), [журнал решений](../data-science/experiments/sensor-failure/DECISION_LOG.md), [итог v9](../data-science/experiments/sensor-failure/FINAL_V9.md) |
| Какие требования выполнять и чем подтверждать? | [REQUIREMENTS_SPEC](REQUIREMENTS_SPEC.md); источники R/Q — [REQUIREMENTS_REVIEW](REQUIREMENTS_REVIEW_2026-09-20.md) |
| Что ещё нужно решить? | [REQUIREMENTS_DECISIONS](REQUIREMENTS_DECISIONS.md) |
| Где исследование, метки и отчёты? | [data-science](../data-science/README.md) |
| Как вносить изменения? | [CONTRIBUTING](../CONTRIBUTING.md) |

Сырые данные организаторов, корпоративные скриншоты, учётные данные и секреты не
публикуются.
