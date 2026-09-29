/**
 * Чистые функции прогноза: порядок списка, тексты частоты и пикета, факты карточки.
 * Без React и без import.meta — проверяются `npm test`.
 */
import type {
  ForecastCard, ForecastCardView, ForecastChannel, ForecastJournalEntry, ForecastList,
} from '../../api/forecast.ts';
import {
  FREQUENCY_WORDING, LINES_TRACKED_LABEL, LINES_UNTRACKED_NOTE, LINE_NO_NAME, LISTED_LINES_FOOT, NO_PICKET_TEXT,
  fillTemplate,
} from '../config/wording.ts';

export interface PicketLike {
  form: 'point' | 'range' | 'unknown';
  from?: number | null;
  to?: number | null;
}

function pk(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toLocaleString('ru-RU', { maximumFractionDigits: 1 });
}

/** «ПК 12–18», «ПК 21», «пикет не указан» — без метров: длина объекта неизвестна. */
export function picketText(picket: PicketLike): string {
  if (picket.form === 'unknown' || picket.from === null || picket.from === undefined) return NO_PICKET_TEXT;
  if (picket.form === 'range' && picket.to !== null && picket.to !== undefined && picket.to !== picket.from) {
    return `ПК ${pk(picket.from)}–${pk(picket.to)}`;
  }
  return `ПК ${pk(picket.from)}`;
}

export interface FrequencyTexts {
  k: number;
  n: number;
  card: string;
  list: string;
  note: string;
  /** «≈ 70%» — крупно в карточке. */
  big: string;
  /** «для карточек этого уровня (сбылось 255 из 300)». */
  detail: string;
  /** «80,5–88,6%» — интервал Уилсона, для «Подробнее». */
  interval: string;
}

export function frequencyTexts(card: Pick<ForecastCard, 'score'>): FrequencyTexts | null {
  const frequency = card.score?.frequency;
  if (!frequency) return null;
  const k = frequency.positive_cards;
  const n = frequency.cards;
  const share = n ? k / n : 0;
  // Округление до 5 п. п. (решение 27.09, DECISION_LOG): точнее эта доля по уровню не известна.
  const pct = Math.round(share * 20) * 5;
  const fmt = (value: number) => (value * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 });
  return {
    k,
    n,
    card: fillTemplate(FREQUENCY_WORDING.card, { k, n, pct }),
    list: fillTemplate(FREQUENCY_WORDING.list, { k, n, pct }),
    note: FREQUENCY_WORDING.note,
    big: fillTemplate(FREQUENCY_WORDING.big, { pct }),
    detail: fillTemplate(FREQUENCY_WORDING.detail, { k, n }),
    interval: `${fmt(frequency.wilson_low)}–${fmt(frequency.wilson_high)}%`,
  };
}

/** Карточка выдана последним расчётом своего списка → метка «новая». */
export function isNewCard(view: ForecastCardView, runs: ForecastList['runs']): boolean {
  const run = runs.find((item) => item.target_spec_id === view.card.target_spec_id && item.horizon === view.card.horizon);
  return run !== undefined && Date.parse(run.issued_at) === Date.parse(view.card.issued_at);
}

export type DecisionMark = 'decided' | 'none' | 'unknown';

export interface OrderedRow<T> {
  item: T;
  /** Место в ответе сервера — «место в списке». */
  place: number;
  isNew: boolean;
  decision: DecisionMark;
  /** Без решения дольше суток после публикации — «без решения > 1 сут». */
  overdue: boolean;
}

function group(row: OrderedRow<unknown>): number {
  if (row.decision === 'decided') return 2;
  return row.overdue ? 0 : 1;
}

/**
 * Порядок прогноза: без решения > 1 сут → без решения → с решением; внутри — новые,
 * затем по месту в списке. Неизвестное решение (ошибка чтения) считается «без решения»,
 * чтобы не спрятать карточку.
 */
export function orderForecastRows<T>(rows: OrderedRow<T>[]): OrderedRow<T>[] {
  return [...rows].sort((a, b) => {
    const byGroup = group(a) - group(b);
    if (byGroup) return byGroup;
    if (a.isNew !== b.isNew) return a.isNew ? -1 : 1;
    return a.place - b.place;
  });
}

/** Карточка без решения дольше `hours` после публикации на момент `asOf` (время данных, не часы браузера). */
export function isDecisionOverdue(publishedAt: string, asOf: string, hours: number): boolean {
  return Date.parse(asOf) - Date.parse(publishedAt) > hours * 3600 * 1000;
}

export interface EpisodeFacts {
  /** События объекта за 365 сут (факт `events_365d` карточки; не сумма потерь связи по линиям). */
  events365: number | null;
  /** Последнее начало потери связи среди перечисленных линий. */
  lastEpisodeAt: string | null;
  /** «После потери связи линия „Обесточен“ …» — текст факта `power_off_followup`. */
  powerOffText: string | null;
}

export function episodeFacts(card: ForecastCard): EpisodeFacts {
  const facts = card.facts ?? [];
  const events = facts.find((fact) => fact.kind === 'events_365d');
  const powerOff = facts.find((fact) => fact.kind === 'power_off_followup');
  const times = [
    ...facts,
    ...(card.channels ?? []).flatMap((channel) => channel.reason_facts ?? []),
  ]
    .filter((fact) => fact.kind === 'last_connection_loss_at' && fact.value_at)
    .map((fact) => fact.value_at as string)
    .sort((a, b) => Date.parse(b) - Date.parse(a));
  return {
    events365: typeof events?.value_number === 'number' ? events.value_number : null,
    lastEpisodeAt: times[0] ?? null,
    powerOffText: powerOff?.text ?? null,
  };
}

export function channelEpisodes(channel: ForecastChannel): number | null {
  const fact = (channel.reason_facts ?? []).find((item) => item.kind === 'events_365d');
  return typeof fact?.value_number === 'number' ? fact.value_number : null;
}

export function channelLastEpisode(channel: ForecastChannel): string | null {
  return (channel.reason_facts ?? []).find((item) => item.kind === 'last_connection_loss_at')?.value_at ?? null;
}

export interface RepeatSummary {
  /** Порядковый номер этой карточки по объекту за 30 сут до её выдачи (включительно). */
  ordinal: number;
  previous: ForecastJournalEntry | null;
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** «m-я карточка за 30 сут; прошлое решение и результат» по записям журнала объекта. */
export function repeatSummary(card: ForecastCard, entries: ForecastJournalEntry[]): RepeatSummary {
  const issued = Date.parse(card.issued_at);
  const same = entries
    .filter((entry) => entry.card.object_id === card.object_id && entry.card.target_spec_id === card.target_spec_id)
    .filter((entry) => {
      const at = Date.parse(entry.card.issued_at);
      return at <= issued && at >= issued - 30 * DAY_MS;
    });
  const earlier = same
    .filter((entry) => entry.card.id !== card.id)
    .sort((a, b) => Date.parse(b.card.issued_at) - Date.parse(a.card.issued_at));
  return { ordinal: earlier.length + 1, previous: earlier[0] ?? null };
}

export function ordinalText(ordinal: number): string {
  return `${ordinal}-я карточка по объекту за 30 сут`;
}

/** «ПК 12–30» по известным пикетам линий карточки; null — ни у одной линии пикета нет. */
export function channelsPicketSpan(channels: Pick<ForecastChannel, 'picket_form' | 'picket_from' | 'picket_to'>[]): string | null {
  const values: number[] = [];
  for (const channel of channels) {
    if (channel.picket_form === 'unknown' || channel.picket_from === null || channel.picket_from === undefined) continue;
    values.push(channel.picket_from);
    if (channel.picket_to !== null && channel.picket_to !== undefined) values.push(channel.picket_to);
  }
  if (!values.length) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  return picketText(min === max ? { form: 'point', from: min } : { form: 'range', from: min, to: max });
}

export interface LinesFact {
  /** «Линий под наблюдением карточки». */
  label: string;
  /** «37 из 41» (из всех линий схемы объекта) или «37», если схема не загружена. */
  value: string;
  /** «остальные 4 при выдаче не отслеживались: …»; null — отслеживаются все. */
  note: string | null;
  /** Все отслеживаемые линии перечислены в карточке. */
  complete: boolean;
  listed: number;
}

/**
 * Третий фактор карточки (F-05). `channels_total` — линии под наблюдением карточки при выдаче
 * (без активной потери связи и без записи-кандидата за сутки до выдачи); `schemeTotal` — все
 * линии объекта на схеме сейчас. Это разные множества: разница подписывается, не прячется.
 */
export function linesFact(
  card: Pick<ForecastCard, 'channels' | 'channels_total'>,
  schemeTotal: number | null = null,
): LinesFact | null {
  const channels = card.channels ?? [];
  const tracked = card.channels_total ?? channels.length;
  if (!tracked && !channels.length) return null;
  const whole = schemeTotal !== null && schemeTotal >= tracked ? schemeTotal : null;
  const untracked = whole !== null ? whole - tracked : 0;
  return {
    label: LINES_TRACKED_LABEL,
    value: whole !== null ? `${tracked} из ${whole}` : String(tracked),
    note: untracked > 0 ? fillTemplate(LINES_UNTRACKED_NOTE, { n: untracked }) : null,
    complete: tracked <= channels.length,
    listed: channels.length,
  };
}

/**
 * Сноска под факторами карточки: какие линии перечислены. Фактор «Последняя потеря связи на
 * линиях из списка» считается только по ним; пустая строка — перечислены все отслеживаемые линии.
 */
export function listedLinesFoot(lines: Pick<LinesFact, 'complete' | 'listed'> | null): string {
  if (!lines || lines.complete) return '';
  const word = lines.listed % 10 === 1 && lines.listed % 100 !== 11 ? 'линии' : 'линий';
  return fillTemplate(LISTED_LINES_FOOT, { n: lines.listed, word });
}

/**
 * Название линии по номеру канала (F-03): из линий карточки, затем из схемы объекта; номер
 * без названия не показывается — «линия без названия в справочнике» (номер — в подсказке).
 */
export function lineNamer(
  channels: Pick<ForecastChannel, 'channel_id' | 'channel_name'>[] | null | undefined,
  schemeNames: Record<string, string> | null = null,
): (channelId: string) => string {
  return (channelId) => (channels ?? []).find((channel) => channel.channel_id === channelId)?.channel_name
    ?? schemeNames?.[channelId]
    ?? LINE_NO_NAME;
}

/** Кавычки интерфейса — «ёлочки»: тексты API с „…“ и “…” приводятся к «…». */
export function ruQuotes(text: string): string {
  return text.replace(/„/g, '«').replace(/[“”]/g, (mark, offset: number, whole: string) => {
    const before = whole.slice(0, offset);
    return before.lastIndexOf('«') > before.lastIndexOf('»') ? '»' : '«';
  });
}

/** Названия линий и узлов схемы объекта по номеру канала (для `lineNamer`). */
export function schemeLineNames(
  scheme: { feeders?: { channel_id: string; name: string }[] | null; landmarks?: { channel_id?: string | null; name: string }[] | null } | null | undefined,
): Record<string, string> | null {
  if (!scheme) return null;
  const names: Record<string, string> = {};
  for (const feeder of scheme.feeders ?? []) names[feeder.channel_id] = feeder.name;
  for (const landmark of scheme.landmarks ?? []) if (landmark.channel_id) names[landmark.channel_id] = landmark.name;
  return names;
}
