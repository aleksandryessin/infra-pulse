import { LINE_KINDS, lineKind, type LineKind } from './mnemo-layout.ts';

/**
 * Выбор на мнемосхеме объекта; хранится в URL: `object` — показанный объект, `sel` —
 * выбранный элемент, `groups` — видимые группы линий (параметра нет — все группы).
 * «← К схеме» из карточки возвращает к тому же объекту, выбору и фильтру.
 */
export type MnemoSelection =
  | { kind: 'forecast'; cardId: string }
  | { kind: 'released'; cardId: string }
  | { kind: 'feeder'; channelId: string }
  | { kind: 'alarm'; rowUid: string }
  | { kind: 'landmark'; index: number }
  /** Знак «N линий · ПК a–b»: линии группы на отрезке ПК (не зависит от масштаба). */
  | { kind: 'range'; group: string; from: number; to: number }
  /** Сводка тревожных сообщений группы за сутки (F6): таблица линий группы с записями. */
  | { kind: 'alarms'; group: string }
  | { kind: 'unknown' };

export function selectionToParam(selection: MnemoSelection | null): string | null {
  if (!selection) return null;
  switch (selection.kind) {
    case 'forecast':
    case 'released':
      return `${selection.kind}:${selection.cardId}`;
    case 'feeder':
      return `feeder:${selection.channelId}`;
    case 'alarm':
      return `alarm:${selection.rowUid}`;
    case 'landmark':
      return `landmark:${selection.index}`;
    case 'range':
      return `range:${selection.group}:${selection.from}-${selection.to}`;
    case 'alarms':
      return `alarms:${selection.group}`;
    default:
      return 'unknown';
  }
}

export function selectionFromParam(raw: string | null): MnemoSelection | null {
  if (!raw) return null;
  const split = raw.indexOf(':');
  const kind = split < 0 ? raw : raw.slice(0, split);
  const ref = split < 0 ? '' : raw.slice(split + 1);
  switch (kind) {
    case 'forecast':
    case 'released':
      return ref ? { kind, cardId: ref } : null;
    case 'feeder':
      return ref ? { kind, channelId: ref } : null;
    case 'alarm':
      return ref ? { kind, rowUid: ref } : null;
    case 'landmark':
      return ref !== '' && Number.isInteger(Number(ref)) ? { kind, index: Number(ref) } : null;
    case 'range': {
      const match = /^([a-z]+):(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$/.exec(ref);
      return match ? { kind, group: match[1], from: Number(match[2]), to: Number(match[3]) } : null;
    }
    case 'alarms':
      return /^[a-z]+$/.test(ref) ? { kind, group: ref } : null;
    case 'unknown':
      return { kind };
    default:
      return null;
  }
}

/** Видимые группы из URL: null — все; пустая строка — ни одной; чужие коды отбрасываются. */
export function groupsFromParam(raw: string | null): LineKind[] | null {
  if (raw === null) return null;
  const wanted = new Set(raw.split(',').map((item) => item.trim()));
  const groups = LINE_KINDS.filter((kind) => wanted.has(kind));
  return groups.length === LINE_KINDS.length ? null : groups;
}

export function groupsToParam(groups: readonly LineKind[] | null): string | null {
  if (groups === null || LINE_KINDS.every((kind) => groups.includes(kind))) return null;
  return LINE_KINDS.filter((kind) => groups.includes(kind)).join(',');
}

/** Параметры URL схемы: объект, (необязательно) выбранный элемент и видимые группы. */
export function schemeParams(
  objectId: string, selection: MnemoSelection | null, groups: readonly LineKind[] | null = null,
): Record<string, string> {
  const params: Record<string, string> = { object: objectId };
  const sel = selectionToParam(selection);
  if (sel) params.sel = sel;
  const visible = groupsToParam(groups);
  if (visible !== null) params.groups = visible;
  return params;
}

interface FeederLike {
  channel_id: string;
  feeder_kind: string;
  current_alarms?: { row_uid: string }[] | null;
}

/** Группа, которую выбор должен показать: линия, тревога линии или знак нескольких линий. */
export function selectionGroup(selection: MnemoSelection | null, feeders: readonly FeederLike[]): LineKind | null {
  if (!selection) return null;
  if (selection.kind === 'range' || selection.kind === 'alarms') return lineKind(selection.group);
  let feeder: FeederLike | undefined;
  if (selection.kind === 'feeder') feeder = feeders.find((item) => item.channel_id === selection.channelId);
  if (selection.kind === 'alarm') {
    feeder = feeders.find((item) => (item.current_alarms ?? []).some((alarm) => alarm.row_uid === selection.rowUid));
  }
  return feeder ? lineKind(feeder.feeder_kind) : null;
}

/** Видимые группы с учётом выбора: выбранная линия или тревога всегда видна. */
export function withSelectionGroup(groups: readonly LineKind[] | null, group: LineKind | null): LineKind[] | null {
  if (groups === null) return null;
  if (group === null || groups.includes(group)) return [...groups];
  return groupsFromParam([...groups, group].join(','));
}

export function sameSelection(a: MnemoSelection | null, b: MnemoSelection | null): boolean {
  return selectionToParam(a) === selectionToParam(b);
}

/**
 * Линия на схеме объекта — ссылка из «Журнала» у сбывшейся карточки (I4 ТЗ-аудита 29.09):
 * место линии, её потери связи за 365 сут и сообщения за сутки. Прежняя ссылка вела на
 * внутреннюю страницу исходных сообщений `/queue` с отладочными полями. Путь `/scheme` —
 * `ROUTES.scheme` (здесь без импорта конфигурации сборки, чтобы функция проверялась `npm test`).
 */
export function schemeLineHref(objectId: string, channelId: string): string {
  return `/scheme?${new URLSearchParams(schemeParams(objectId, { kind: 'feeder', channelId }))}`;
}
