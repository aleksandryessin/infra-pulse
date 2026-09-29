import { FORECAST_NO_DATA, fillTemplate } from '../config/wording.ts';

/** «01.07» из даты API `2026-07-01` (сутки МСК). */
function dayMonth(day: string): string {
  const [, month, date] = day.split('-');
  return `${date}.${month}`;
}

/**
 * P1-2: почему «Прогноз» пуст. `no_data_from`…`no_data_to` — сутки без данных перед последней
 * выдачей; без них (или при неполном периоде) — null, и остаётся «Открытых прогнозов нет».
 */
export function noDataText(from?: string | null, to?: string | null): string | null {
  if (!from || !to) return null;
  const period = from === to ? dayMonth(from) : `${dayMonth(from)}–${dayMonth(to)}`;
  return fillTemplate(FORECAST_NO_DATA, { period });
}
