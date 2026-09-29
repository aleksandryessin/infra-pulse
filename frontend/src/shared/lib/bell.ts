/**
 * Строки колокольчика из серверного `/notifications` (политика critical-alarms-v1, C0.5).
 * Чистые функции: `npm test`.
 *
 * В колокольчик входят только критические сообщения источника (`critical_alarm`); новые
 * карточки прогноза — отдельный счётчик у пункта меню. Порядок — строго как в ответе
 * сервера (газ первым по `priority`); клиент ничего не сортирует и не группирует сам.
 * Вид строки — по `row_kind`: одиночная запись, серия извещателей или газоанализаторов,
 * похожая на ППР или ТО, повторы одного датчика за час (`chatter`: «… ×N», под строкой «с ЧЧ:ММ
 * по ЧЧ:ММ» — сворачивает сервер). Метка группы («Пожар», «Газ», «Затопление») — по `rule_id`.
 */
import { alarmGroupLabel } from '../config/labels.ts';
import { fmtShortDate, fmtTime } from './format.ts';


export interface BellMember {
  row_uid: string;
  channel_id: string;
  /** Название канала из справочника; нет — показывается ИД (QA 29.09, D3). */
  channel_name?: string | null;
  event_at: string;
  value_raw: string;
}

export interface BellSource {
  kind: string;
  ref_id: string;
  object_id?: string | null;
  object_name?: string | null;
  title: string;
  at: string;
  rule_id?: string | null;
  rule_text?: string | null;
  row_kind?: string | null;
  collapsed_count?: number | null;
  channels_count?: number | null;
  first_at?: string | null;
  members?: BellMember[];
}

export interface BellRow<T extends BellSource = BellSource> {
  key: string;
  item: T;
  /** Заголовок свёрнутой строки; null — одиночная запись. */
  summary: string | null;
  /** Группа по классификации заказчика (по `rule_id`); null — правило неизвестно. */
  group: string | null;
  /** Записей в строке (для «показаны 50 из N»). */
  count: number;
  members: BellMember[];
  /** Все записи строки — для «прочитано». */
  refIds: string[];
}

/** Сервер отдаёт не больше этого числа `members` в строке. */
export const BELL_MEMBERS_LIMIT = 50;

function plural(count: number, forms: [string, string, string]): string {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return forms[0];
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
  return forms[2];
}

/**
 * Серии — словами заказчика (ответ 28.09): «похоже на ППР или ТО: 6 извещателей — сверить с
 * графиком» / «похоже на ППР или ТО: 5 газоанализаторов — сверить с графиком»; графика работ в
 * сервисе нет. Повторы одного датчика за час — заголовок сервера как есть: «Датчик дыма:
 * «Обнаружен дым» ×5» (обкатка 29.09); интервал — `repeatSpan`.
 */
export function bellSummary(item: BellSource): string | null {
  const records = item.collapsed_count ?? item.members?.length ?? 1;
  const channels = item.channels_count ?? records;
  switch (item.row_kind) {
    case 'test_series':
      return `похоже на ППР или ТО: ${channels} ${plural(channels, ['извещатель', 'извещателя', 'извещателей'])} — сверить с графиком`;
    case 'calibration_series':
      return `похоже на ППР или ТО: ${channels} ${plural(channels, ['газоанализатор', 'газоанализатора', 'газоанализаторов'])} — сверить с графиком`;
    case 'chatter':
      return item.title;
    default:
      return null;
  }
}

/**
 * Интервал повторов одного датчика под строкой: «30.06, с 16:30 по 16:56» (МСК). Повторы
 * сворачиваются в пределах календарного часа, поэтому дата у начала и конца одна.
 */
export function repeatSpan(item: BellSource): string {
  const first = item.first_at ?? item.at;
  return `${fmtShortDate(item.at)}, с ${fmtTime(first)} по ${fmtTime(item.at)}`;
}

/**
 * Объект строки, чью «Схему» можно открыть, или null. Схема строится по линиям электропитания
 * (`/schemes`); у объектов пожарной сигнализации без них схемы нет, и ссылка вела на «HTTP 404»
 * (обкатка 29.09). `schemeObjects` null — список схем ещё не получен: ссылка остаётся.
 */
export function schemeObject(item: BellSource, schemeObjects: ReadonlySet<string> | null): string | null {
  if (!item.object_id) return null;
  return schemeObjects === null || schemeObjects.has(item.object_id) ? item.object_id : null;
}

export function recordsText(count: number): string {
  return `${count} ${plural(count, ['запись', 'записи', 'записей'])}`;
}

export function bellRows<T extends BellSource>(items: T[]): BellRow<T>[] {
  return items
    .filter((item) => item.kind === 'critical_alarm')
    .map((item) => {
      const members = item.members ?? [];
      return {
        key: `${item.row_kind ?? 'single'}:${item.ref_id}`,
        item,
        summary: bellSummary(item),
        group: alarmGroupLabel(item.rule_id),
        count: item.collapsed_count ?? Math.max(1, members.length),
        members,
        refIds: [item.ref_id, ...members.map((member) => member.row_uid)],
      };
    });
}
