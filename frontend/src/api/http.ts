/**
 * Единственный HTTP-клиент SPA.
 *
 * - Cookie-сессия (`credentials: 'same-origin'`), ответ 401 передаётся сессии → экран входа.
 * - Каждый POST несёт заголовок `X-CSRF-Token` (B3). Значение берётся из cookie
 *   `__Host-infrapulse-csrf`, которую B3 ставит при входе, или из заголовка ответа сервера;
 *   до входа — любое непустое значение (см. frontend/README.md).
 * - Ошибка API всегда поднимается как `ApiError` и никогда не подменяется fixture.
 */

export const CSRF_HEADER = 'X-CSRF-Token';
/** Читаемая cookie с CSRF-токеном сессии (B3, `auth/sessions.py`); прочие имена — запасные. */
export const CSRF_COOKIE_NAMES = ['__Host-infrapulse-csrf', 'infra_csrf', 'csrf_token', 'XSRF-TOKEN'] as const;

export type ApiErrorKind = 'http' | 'network' | 'aborted';

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string | null;
  readonly kind: ApiErrorKind;
  readonly retryAfter: number | null;

  constructor(kind: ApiErrorKind, status: number, detail: string | null, retryAfter: number | null = null) {
    // Прежние страницы исходных сообщений разбирают текст `HTTP <код>`.
    super(kind === 'http' ? `HTTP ${status}` : kind === 'aborted' ? 'Запрос отменён' : 'Нет связи с сервером');
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
  }
}

export function isAbort(error: unknown): boolean {
  return (error instanceof ApiError && error.kind === 'aborted')
    || (error instanceof DOMException && error.name === 'AbortError');
}

let unauthorizedHandler: (() => void) | null = null;
let csrfFromResponse: string | null = null;

/** Сессия регистрирует обработчик 401; login/me сами обрабатывают свой 401. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const match = document.cookie.split('; ').find((part) => part.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null;
}

export function csrfToken(): string {
  for (const name of CSRF_COOKIE_NAMES) {
    const value = readCookie(name);
    if (value) return value;
  }
  // Без выданного токена заголовок всё равно отправляется: B3 может проверять его наличие.
  return csrfFromResponse ?? '1';
}

async function readDetail(response: Response): Promise<string | null> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === 'string') return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((item) => {
          const entry = item as { loc?: unknown[]; msg?: string };
          const field = Array.isArray(entry.loc) ? entry.loc.filter((part) => part !== 'body').join('.') : '';
          return field ? `${field}: ${entry.msg ?? ''}` : entry.msg ?? '';
        })
        .filter(Boolean)
        .join('; ');
    }
    return null;
  } catch {
    return null;
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST';
  json?: unknown;
  form?: FormData;
  signal?: AbortSignal;
  /** Для /auth/*: 401 означает «нужен вход», а не истёкшую сессию. */
  skipUnauthorizedHandler?: boolean;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  let body: BodyInit | undefined;
  if (options.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(options.json);
  } else if (options.form) {
    body = options.form;
  }
  if (method !== 'GET') headers[CSRF_HEADER] = csrfToken();

  let response: Response;
  try {
    response = await fetch(path, { method, headers, body, signal: options.signal, credentials: 'same-origin' });
  } catch (error) {
    if (isAbort(error) || options.signal?.aborted) throw new ApiError('aborted', 0, null);
    throw new ApiError('network', 0, null);
  }
  const issued = response.headers.get(CSRF_HEADER);
  if (issued) csrfFromResponse = issued;

  if (!response.ok) {
    const detail = await readDetail(response);
    const retry = Number(response.headers.get('Retry-After'));
    const error = new ApiError('http', response.status, detail, Number.isFinite(retry) && retry > 0 ? retry : null);
    if (response.status === 401 && !options.skipUnauthorizedHandler) unauthorizedHandler?.();
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

const DETAIL_TEXT: Record<string, string> = {
  forecast_not_implemented: 'Прогноз на сервере ещё не подключён',
  decisions_not_implemented: 'Сохранение решений на сервере ещё не подключено',
  imports_not_implemented: 'Загрузка файлов на сервере ещё не подключена',
  research_summary_missing: 'Сводка исследования на сервере не подключена',
  research_summary_invalid: 'Сводка исследования на сервере не прошла проверку',
  auth_not_implemented: 'Вход на сервере ещё не настроен',
  observations_not_configured: 'Исходные сообщения на сервере не настроены',
  forecast_not_found: 'Карточка не найдена',
  object_not_found: 'Объект не найден',
  import_not_found: 'Загрузка не найдена',
  forbidden_role: 'Нет доступа для вашей роли',
  csrf_failed: 'Сессия устарела: обновите страницу и повторите',
  reason_code_mismatch: 'Причина не относится к выбранному решению',
  idempotency_key_reused: 'Повтор запроса с другими данными: обновите карточку',
  imports_not_configured: 'Загрузка файлов на сервере не настроена',
  decisions_storage_not_configured: 'Сохранение решений на сервере не настроено',
  decision_revision_conflict: 'Решение по карточке уже изменено в другом окне',
  notified_after_decision: 'Время уведомления позже времени решения',
  file_too_large: 'Файл больше 50 МБ',
  forecast_cursor_stale: 'Список обновился во время просмотра',
  issued_range_reversed: 'Дата «с» позже даты «по»',
  aware_as_of_required: 'Для исторического повтора нужен момент данных, а опубликованного прогноза нет',
};

/** Понятный текст ошибки для экрана; код и detail остаются рядом для диагностики. */
export function describeApiError(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Неизвестная ошибка интерфейса';
  if (error.kind === 'network') return 'Нет связи с сервером';
  if (error.kind === 'aborted') return 'Запрос отменён';
  const known = error.detail ? DETAIL_TEXT[error.detail] : undefined;
  switch (error.status) {
    case 401:
      return 'Нужен вход: сессия отсутствует или истекла';
    case 403:
      return known ?? 'Нет доступа для вашей роли';
    case 404:
      return known ?? 'Не найдено';
    case 409:
      return known ?? 'Конфликт версии данных';
    case 413:
      return 'Файл больше 50 МБ';
    case 422:
      return known ?? `Сервер не принял данные${error.detail ? `: ${error.detail}` : ''}`;
    case 429:
      return `Слишком много запросов${error.retryAfter ? `, повторите через ${error.retryAfter} с` : ', повторите позже'}`;
    case 503:
      return known ?? 'Сервис временно недоступен';
    default:
      return error.status >= 500 ? 'Ошибка сервера' : known ?? 'Запрос не выполнен';
  }
}

/** Короткий технический хвост «HTTP 503 · forecast_not_implemented». */
export function errorCode(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.kind !== 'http') return null;
  return error.detail && !error.detail.includes(' ') ? `HTTP ${error.status} · ${error.detail}` : `HTTP ${error.status}`;
}
