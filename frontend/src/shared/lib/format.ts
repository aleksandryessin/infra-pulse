/** Время интерфейса — Europe/Moscow; API отдаёт aware ISO 8601. */
const TZ = 'Europe/Moscow';

const dateTime = new Intl.DateTimeFormat('ru-RU', {
  timeZone: TZ, day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
});
const shortDateTime = new Intl.DateTimeFormat('ru-RU', {
  timeZone: TZ, day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
});
const date = new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, day: '2-digit', month: '2-digit', year: 'numeric' });
const shortDate = new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, day: '2-digit', month: '2-digit' });
const time = new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, hour: '2-digit', minute: '2-digit' });

function parse(value: string | Date | null | undefined): Date | null {
  if (!value) return null;
  const parsed = value instanceof Date ? value : new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function using(format: Intl.DateTimeFormat, value: string | Date | null | undefined, fallback = '—'): string {
  const parsed = parse(value);
  return parsed ? format.format(parsed) : fallback;
}

/** 23.09.2026, 23:59 */
export const fmtDateTime = (value: string | Date | null | undefined) => using(dateTime, value);
/** 23.09, 23:59 */
export const fmtShortDateTime = (value: string | Date | null | undefined) => using(shortDateTime, value);
/** 23.09.2026 */
export const fmtDate = (value: string | Date | null | undefined) => using(date, value);
/** 23.09 */
export const fmtShortDate = (value: string | Date | null | undefined) => using(shortDate, value);
/** 23:59 */
export const fmtTime = (value: string | Date | null | undefined) => using(time, value);

/** Календарная дата `YYYY-MM-DD` (contract `date`) без сдвига часового пояса: 07.09.2026. */
export function fmtIsoDay(value: string | null | undefined): string {
  const match = value ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(value) : null;
  return match ? `${match[3]}.${match[2]}.${match[1]}` : value ?? '—';
}

/** Дата `YYYY-MM-DD` в Москве для фильтров журнала. */
export function mskIsoDay(value: string | Date): string {
  const parsed = parse(value);
  if (!parsed) return '';
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' })
    .formatToParts(parsed);
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')}`;
}

/** Доля 0..1 → «85» (целые проценты, для частоты в карточке). */
export function pct(share: number): string {
  return String(Math.round(share * 100));
}

/** 0,773 */
export function decimal(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Длительность в часах → «10 сут 6 ч» / «5 ч». */
export function fmtHours(hours: number | null | undefined): string {
  if (hours === null || hours === undefined || Number.isNaN(hours)) return '—';
  const whole = Math.max(0, Math.round(hours));
  const days = Math.floor(whole / 24);
  const rest = whole % 24;
  if (days === 0) return `${rest} ч`;
  return rest ? `${days} сут ${rest} ч` : `${days} сут`;
}

/** Длительность в секундах → «3,8 с» / «2 мин 5 с». */
export function fmtSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—';
  if (seconds < 60) return `${seconds.toLocaleString('ru-RU', { maximumFractionDigits: 1 })} с`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return rest ? `${minutes} мин ${rest} с` : `${minutes} мин`;
}

export function fmtBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} КБ`;
  return `${(bytes / 1024 / 1024).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} МБ`;
}

export function fmtCount(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toLocaleString('ru-RU');
}

/** Склонение: plural(3, ['линия', 'линии', 'линий']). */
export function plural(count: number, forms: [string, string, string]): string {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return forms[0];
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
  return forms[2];
}

/**
 * Точка в конце предложения без удвоения: название линии может кончаться сокращением
 * («ФВ2 ПК254 щит.»), и «Линии события: … щит..» выглядело опечаткой (P3 репетиции 29.09).
 */
export function endSentence(text: string): string {
  return /[.!?…]$/u.test(text.trimEnd()) ? text : `${text}.`;
}
