import type { ForecastJournalEntry } from '../../api/forecast';
import { ABSTENTION_LABELS, LIST_STATE_LABELS, OUTCOME_LABELS, UNKNOWN_REASON_LABELS } from '../../shared/config/labels';
import { lineNamer } from '../../shared/lib/forecast';
import { fmtHours, fmtShortDateTime } from '../../shared/lib/format';
import { schemeLineHref } from '../../widgets/object-mnemo/selection';

/** Состояние записи журнала: открыта / снята по событию / срок истёк / прогноз не выдан. */
export function entryState(entry: ForecastJournalEntry): string {
  if (entry.card.status === 'abstained') {
    const reason = entry.card.abstention_reason ? ABSTENTION_LABELS[entry.card.abstention_reason] ?? entry.card.abstention_reason : '';
    return `прогноз не выдан${reason ? `: ${reason}` : ''}`;
  }
  if (!entry.list_state) return '—';
  const label = LIST_STATE_LABELS[entry.list_state] ?? entry.list_state;
  return entry.list_state === 'released' && entry.released_at ? `${label} ${fmtShortDateTime(entry.released_at)}` : label;
}

export interface OutcomeText {
  /** «сбылась» / «истекла без события» / «исход неизвестен» / «срок не истёк». */
  label: string;
  /** Для «сбылась»: линии электропитания и время «Неисправен» → «Обесточен». */
  detail: string | null;
  /**
   * Линия события на схеме объекта — для всех ролей и режимов. Прежде — ссылка «исходные записи
   * линии» на внутреннюю страницу `/queue` (кандидаты, `alarm=true`, версии правил), I4 ТЗ-аудита.
   */
  lineHref: string | null;
}

/** Факт: итог по автоматически зарегистрированному событию, не вердикт человека. */
export function entryOutcome(entry: ForecastJournalEntry, lineName = lineNamer(entry.card.channels)): OutcomeText {
  const outcome = entry.outcome;
  const label = OUTCOME_LABELS[outcome.status] ?? outcome.status;
  if (outcome.status === 'realized' || outcome.status === 'event_without_forecast') {
    const channels = outcome.event_channel_ids ?? [];
    // F-03: номер канала без названия не показывается.
    const names = [...new Set(channels.map(lineName))];
    // Время «Обесточен» — `power_off_at` (C0.4); если не зафиксировано, так и пишем.
    const powerOff = outcome.power_off_at ? fmtShortDateTime(outcome.power_off_at) : 'время не зафиксировано';
    const detail = `${names.join(', ') || 'линия не указана'} · «Неисправен» ${fmtShortDateTime(outcome.first_event_at)} → «Обесточен» ${powerOff}`;
    const lineHref = channels[0] && entry.card.object_id ? schemeLineHref(entry.card.object_id, channels[0]) : null;
    return { label, detail, lineHref };
  }
  if (outcome.status === 'unknown') {
    const reason = outcome.unknown_reason ? UNKNOWN_REASON_LABELS[outcome.unknown_reason] ?? outcome.unknown_reason : null;
    return { label, detail: reason, lineHref: null };
  }
  return { label, detail: null, lineHref: null };
}

/** Запас: время от выдачи карточки до начала события. */
export function entryLead(entry: ForecastJournalEntry): string {
  return entry.outcome.lead_hours !== null && entry.outcome.lead_hours !== undefined ? fmtHours(entry.outcome.lead_hours) : '—';
}

export interface ResultText {
  /** Плашка «Итог»: «открыта» или итог по событию. */
  chip: string;
  /** Подсказка к плашке: «сбылась» — карточка снята при начале события. */
  hint: string | null;
  tone: 'open' | 'released' | 'neutral';
  /** Серая строка: только итог с датой; у открытой — пусто. */
  line: string | null;
  lineHref: string | null;
}

export const REALIZED_HINT = 'карточка снята при начале события';

/** «Итог» журнала: одна плашка и одна серая строка с датой (совместный просмотр F3). */
export function entryResult(entry: ForecastJournalEntry): ResultText {
  const outcome = entry.outcome;
  const base = entryOutcome(entry);
  if (outcome.status === 'pending') {
    const state = entry.list_state ? LIST_STATE_LABELS[entry.list_state] ?? entry.list_state : 'открыта';
    return { chip: state, hint: null, tone: entry.list_state === 'open' ? 'open' : 'neutral', line: null, lineHref: null };
  }
  if (outcome.status === 'realized' || outcome.status === 'event_without_forecast') {
    const powerOff = outcome.power_off_at ? ` · «Обесточен» ${fmtShortDateTime(outcome.power_off_at)}` : '';
    return {
      chip: base.label,
      hint: outcome.status === 'realized' ? REALIZED_HINT : null,
      tone: entry.list_state === 'released' ? 'released' : 'neutral',
      line: `событие ${fmtShortDateTime(outcome.first_event_at)}${powerOff}`,
      lineHref: base.lineHref,
    };
  }
  if (outcome.status === 'not_realized') {
    return { chip: base.label, hint: null, tone: 'neutral', line: `срок истёк ${fmtShortDateTime(entry.card.window_end)}`, lineHref: null };
  }
  return {
    chip: base.label,
    hint: null,
    tone: 'neutral',
    line: base.detail ?? (outcome.resolved_at ? fmtShortDateTime(outcome.resolved_at) : null),
    lineHref: null,
  };
}

/** «Прогноз не выдан» — не карточка: в журнале отдельным примечанием, без номера и счёта. */
export function isAbstained(entry: ForecastJournalEntry): boolean {
  return entry.card.status === 'abstained';
}
