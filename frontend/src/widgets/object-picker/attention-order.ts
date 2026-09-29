/**
 * Порядок «Требуют внимания» на схеме: сначала объекты с тревожными сообщениями СМВУ за сутки,
 * затем с открытым прогнозом — по близости срока, затем снятые по событию за 7 сут.
 * Это порядок просмотра, не тяжесть. Чистые функции — проверяются `npm test`.
 */

export interface SummaryLike {
  object_id: string;
  object_name?: string | null;
  current_alarms: number;
  open_cards: number;
  released_7d: number;
}

export interface OpenCardLike {
  id: string;
  object_id?: string | null;
  window_end: string;
  day_index?: number | null;
  days_total?: number | null;
  /** `risk_level` карточки: метка уровня в строке (ТЗ §10); на порядок не влияет. */
  risk_level?: string | null;
}

export interface ReleasedCardLike {
  id: string;
  object_id?: string | null;
  released_at?: string | null;
}

export type AttentionGroup = 'alarm' | 'forecast' | 'released' | 'none';

export interface AttentionEntry {
  objectId: string;
  name: string;
  group: AttentionGroup;
  alarms: number;
  forecast: OpenCardLike | null;
  released: ReleasedCardLike | null;
}

const GROUP_ORDER: Record<AttentionGroup, number> = { alarm: 0, forecast: 1, released: 2, none: 3 };

function time(value: string | null | undefined, fallback: number): number {
  const parsed = value ? Date.parse(value) : Number.NaN;
  return Number.isNaN(parsed) ? fallback : parsed;
}

export function attentionOrder(
  summaries: SummaryLike[],
  open: OpenCardLike[],
  released: ReleasedCardLike[],
): AttentionEntry[] {
  const openBy = new Map<string, OpenCardLike>();
  for (const card of open) {
    if (!card.object_id) continue;
    const prev = openBy.get(card.object_id);
    if (!prev || time(card.window_end, Infinity) < time(prev.window_end, Infinity)) openBy.set(card.object_id, card);
  }
  const releasedBy = new Map<string, ReleasedCardLike>();
  for (const card of released) {
    if (!card.object_id) continue;
    const prev = releasedBy.get(card.object_id);
    if (!prev || time(card.released_at, -Infinity) > time(prev.released_at, -Infinity)) releasedBy.set(card.object_id, card);
  }
  const entries = summaries.map((summary): AttentionEntry => {
    const forecast = openBy.get(summary.object_id) ?? null;
    const release = summary.released_7d > 0 ? releasedBy.get(summary.object_id) ?? null : null;
    const group: AttentionGroup = summary.current_alarms > 0 ? 'alarm'
      : forecast ? 'forecast'
        : summary.released_7d > 0 ? 'released' : 'none';
    return {
      objectId: summary.object_id,
      name: summary.object_name ?? summary.object_id,
      group,
      alarms: summary.current_alarms,
      forecast,
      released: release,
    };
  });
  return entries.sort((a, b) => {
    const byGroup = GROUP_ORDER[a.group] - GROUP_ORDER[b.group];
    if (byGroup) return byGroup;
    if (a.group === 'released') return time(b.released?.released_at, -Infinity) - time(a.released?.released_at, -Infinity);
    const byTerm = time(a.forecast?.window_end, Infinity) - time(b.forecast?.window_end, Infinity);
    if (byTerm && Number.isFinite(byTerm)) return byTerm;
    if (a.forecast && !b.forecast) return -1;
    if (!a.forecast && b.forecast) return 1;
    return a.name.localeCompare(b.name, 'ru', { numeric: true });
  });
}

/** Объект по умолчанию: первый с тревогой, иначе с ближайшим сроком прогноза, иначе первый в списке. */
export function defaultObject(entries: AttentionEntry[]): string | null {
  return entries[0]?.objectId ?? null;
}
