# Generated contracts: граница backend ↔ frontend

Источник истины: `packages/core/src/infra_pulse_core/contracts/` (Pydantic).
Сами HTTP routes определяет backend. Эти файлы генерируются, не правятся вручную:

- `openapi.json`: endpoints, request/response types, validation schema.
- `risk.fixture.json`: синтетический пример для интеграции и объяснения semantics.
- `forecast.fixture.json`: ответы fixture для `GET /api/v1/forecasts`,
  `/forecast-journal`, `/forecast-journal/summary` (счётчики),
  `/forecast-journal/quality`, `/forecast-state`, `/registries/recurring`, `/schemes` и
  пример `/schemes/{object_id}` (synthetic ID; доли k/n и счётчики — не метрики).
  Схемы — `infra_pulse_core.contracts.forecast` и `scheme`.
- `product.fixture.json`: fixture загрузок (`/imports`), страницы «Исследование»
  (`/research-summary`), dev-идентичности (`/auth/me`), пачки потока
  (`/observations/{id}`), уведомлений (`/notifications`) и месячного отчёта
  (`/reports/monthly`). Схемы — `imports.py`, `research.py`, `auth.py`,
  `notifications.py`, `reports.py`.

Из корня: `make contracts`, затем `npm --prefix frontend run generate:api`.
Проверить `make check` и frontend build. Схема и generated TS включаются в один diff.
Никаких данных заказчика, моделей или весов здесь нет. Эта папка относится ко всем
компонентам, поэтому остаётся в корне, а не в исследовательской области.
