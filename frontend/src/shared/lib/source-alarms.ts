/**
 * «Сейчас» на экране «Прогноз»: тревожные сообщения СМВУ за сутки до среза сообщений.
 *
 * Сервер (`/attention/source-alarms`) считает всё окно сам: сообщения, объекты, линии и сообщения
 * на линиях схемы объекта — и отдаёт последние сообщения по времени события. Прежде браузер читал
 * окно страницами по времени приёма: на копии стенда файл дыма, газа и насосов, принятый 29.09,
 * шёл раньше засеянных июньских сообщений фазы, и 108 сообщений объекта с открытой карточкой
 * прогноза не попадали ни в первые 100, ни в первые 1 000 строк (обкатка 29.09.2026).
 */

import type { ObservedMessage, SourceAlarmObject, SourceAlarmWindow } from '../../api/attention';

/** Объект окна «Сейчас»: с сервера или посчитанный на клиенте из сообщений по тексту. */
export type NowObject = Pick<
  SourceAlarmObject,
  'object_id' | 'object_name' | 'record_count' | 'channel_count' | 'first_event_at' | 'last_event_at' | 'last_message'
> & { scheme_record_count?: number | null };

export interface NowWindow {
  records: number;
  objectCount: number;
  withoutObject: number;
  objects: NowObject[];
  latest: ObservedMessage[];
}

export function fromServer(window: SourceAlarmWindow): NowWindow {
  return {
    records: window.record_count,
    objectCount: window.object_count,
    withoutObject: window.without_object_count,
    objects: window.objects,
    latest: window.latest,
  };
}

const newestFirst = (a: ObservedMessage, b: ObservedMessage) =>
  Date.parse(b.event_at) - Date.parse(a.event_at) || (a.row_uid < b.row_uid ? 1 : a.row_uid > b.row_uid ? -1 : 0);

/**
 * Окно из уже прочитанных сообщений — только для режима «нет отметки «тревожное»» (сообщения
 * отобраны по тексту на клиенте, их не больше окна чтения); правила те же, что на сервере.
 */
export function fromMessages(messages: ObservedMessage[], latestLimit: number): NowWindow {
  const sorted = [...messages].sort(newestFirst);
  const groups = new Map<string, ObservedMessage[]>();
  for (const message of sorted) {
    if (!message.object_id) continue;
    groups.set(message.object_id, [...(groups.get(message.object_id) ?? []), message]);
  }
  const objects: NowObject[] = [...groups.entries()].map(([objectId, items]) => ({
    object_id: objectId,
    object_name: items[0].object_name ?? null,
    record_count: items.length,
    channel_count: new Set(items.map((item) => item.channel_id)).size,
    first_event_at: items[items.length - 1].event_at,
    last_event_at: items[0].event_at,
    last_message: items[0],
    scheme_record_count: null,
  }));
  return {
    records: sorted.length,
    objectCount: objects.length,
    withoutObject: sorted.filter((message) => !message.object_id).length,
    objects,
    latest: sorted.slice(0, latestLimit),
  };
}

/**
 * Порядок объектов «Сейчас»: сначала объекты с открытой карточкой прогноза, затем по времени
 * последнего сообщения (новые сверху), при равенстве — больше сообщений выше.
 */
export function orderNowObjects<T extends NowObject>(objects: T[], openObjects: ReadonlySet<string>): T[] {
  return [...objects].sort((a, b) => Number(openObjects.has(b.object_id)) - Number(openObjects.has(a.object_id))
    || Date.parse(b.last_event_at) - Date.parse(a.last_event_at)
    || b.record_count - a.record_count
    || a.object_id.localeCompare(b.object_id));
}

/** «сообщений: 1263 · объектов: 18» — всё окно; сообщения каналов вне справочника — отдельно. */
export function nowCountsText(window: Pick<NowWindow, 'records' | 'objectCount' | 'withoutObject'>): string {
  const parts = [`сообщений: ${window.records}`];
  if (window.objectCount) parts.push(`объектов: ${window.objectCount}`);
  if (window.withoutObject) parts.push(`без объекта: ${window.withoutObject}`);
  return parts.join(' · ');
}

/**
 * Куда ведёт строка «Сейчас» на «Схеме»: объект со схемой (`schemeObjects`; null — список ещё не
 * получен), выбор сообщения — только если оно на линии электропитания схемы (`feeder_kind`), иначе
 * схема показала бы «Сообщение не найдено». Объект без схемы — null: строка не ссылка.
 */
export function schemeQuery(
  message: Pick<ObservedMessage, 'object_id' | 'row_uid' | 'feeder_kind'>,
  schemeObjects: ReadonlySet<string> | null,
  select = true,
): URLSearchParams | null {
  if (!message.object_id) return null;
  if (schemeObjects && !schemeObjects.has(message.object_id)) return null;
  const query = new URLSearchParams({ object: message.object_id });
  // Схема показывает только сообщения с отметкой «тревожное»: отобранные по тексту не выбираются.
  if (select && message.feeder_kind) query.set('sel', `alarm:${message.row_uid}`);
  return query;
}
