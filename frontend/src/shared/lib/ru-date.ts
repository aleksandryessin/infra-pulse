/**
 * Поле даты в формате `дд.мм.гггг` (M3 аудита 29.09): нативный `<input type="date">` показывает
 * формат языка браузера (в английском Chrome — mm/dd/yyyy). Значение фильтра — ISO `YYYY-MM-DD`.
 * Сетка календаря (`calendarDays`) — для своего всплывающего календаря поля, не системного.
 */

/** ISO-день → `дд.мм.гггг`; пусто или не ISO — пустая строка. */
export function formatRuDate(iso: string | null | undefined): string {
  const match = iso ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso) : null;
  return match ? `${match[3]}.${match[2]}.${match[1]}` : '';
}

/**
 * Ввод → ISO-день. Пустая строка — `null` (фильтр снят); неполная или несуществующая дата —
 * `undefined` (фильтр не меняется). Принимает `1.6.2026`, `01.06.2026`, `01062026`, `01/06/2026`.
 */
export function parseRuDate(text: string): string | null | undefined {
  const value = text.trim();
  if (!value) return null;
  const match = /^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$/.exec(value) ?? /^(\d{2})(\d{2})(\d{4})$/.exec(value);
  if (!match) return undefined;
  const day = Number(match[1]);
  const month = Number(match[2]);
  const year = Number(match[3]);
  if (year < 1900 || month < 1 || month > 12 || day < 1) return undefined;
  const probe = new Date(Date.UTC(year, month - 1, day));
  if (probe.getUTCMonth() !== month - 1 || probe.getUTCDate() !== day) return undefined;
  return `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
}

/** Названия месяцев для заголовка календаря `RuDateInput` («Июнь 2026»). */
export const RU_MONTHS = [
  'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
];
/** Родительный падеж — для подписи дня («1 июня 2026»). */
const RU_MONTHS_GENITIVE = [
  'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
  'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
];
/** Неделя календаря начинается с понедельника. */
export const RU_WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];

function isoOf(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function utcDay(iso: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  return match ? new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))) : null;
}

/** ISO-день ± `days`; не ISO — `null`. */
export function shiftIsoDay(iso: string, days: number): string | null {
  const date = utcDay(iso);
  if (!date) return null;
  date.setUTCDate(date.getUTCDate() + days);
  return isoOf(date);
}

/** Месяц `YYYY-MM` ± `months`. */
export function shiftMonth(month: string, months: number): string {
  const [year, index] = month.split('-').map(Number);
  const date = new Date(Date.UTC(year, index - 1 + months, 1));
  return isoOf(date).slice(0, 7);
}

/** 42 дня сетки месяца `YYYY-MM` с понедельника: `inMonth` — день этого месяца. */
export function calendarDays(month: string): { iso: string; day: number; inMonth: boolean }[] {
  const first = utcDay(`${month}-01`);
  if (!first) return [];
  const offset = (first.getUTCDay() + 6) % 7;
  const start = new Date(first);
  start.setUTCDate(1 - offset);
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(start);
    date.setUTCDate(start.getUTCDate() + index);
    const iso = isoOf(date);
    return { iso, day: date.getUTCDate(), inMonth: iso.startsWith(month) };
  });
}

/** «1 июня 2026» — подпись кнопки дня для экранного диктора. */
export function ruDayLabel(iso: string): string {
  const date = utcDay(iso);
  return date ? `${date.getUTCDate()} ${RU_MONTHS_GENITIVE[date.getUTCMonth()]} ${date.getUTCFullYear()}` : iso;
}
