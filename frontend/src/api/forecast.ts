import type { components } from './schema';
import { apiRequest, query } from './http';

type S = components['schemas'];
export type ForecastState = S['ForecastState'];
export type ForecastList = S['ForecastList'];
export type ForecastCardView = S['ForecastCardView'];
export type ForecastCard = S['ForecastCard'];
export type ForecastChannel = S['ForecastChannel'];
export type ForecastFact = S['ForecastFact'];
export type ForecastDecisionList = S['ForecastDecisionList'];
export type ForecastDecisionSummary = S['ForecastDecisionSummary'];
export type ForecastDecisionCreate = S['ForecastDecisionCreate'];
export type ForecastJournalList = S['ForecastJournalList'];
export type ForecastJournalEntry = S['ForecastJournalEntry'];
export type ForecastJournalCounts = S['ForecastJournalCounts'];
export type ForecastQualitySummary = S['ForecastQualitySummary'];
export type ForecastOutcome = S['ForecastOutcome'];
export type ForecastCheckResult = S['ForecastCheckResult'];
export type ForecastCheckResultCreate = S['ForecastCheckResultCreate'];
export type ForecastCheckResultList = S['ForecastCheckResultList'];
export type CheckResultStatus = ForecastCheckResultCreate['check_result'];
export type FoundItem = NonNullable<ForecastCheckResultCreate['found']>[number];
export type EventCause = NonNullable<ForecastCheckResultCreate['event_cause']>;
export type ListState = NonNullable<ForecastCardView['list_state']>;
export type DecisionCode = ForecastDecisionCreate['decision_code'];
export type VerificationMethod = ForecastDecisionCreate['verification_methods'][number];

export function getForecastState(signal?: AbortSignal): Promise<ForecastState> {
  return apiRequest<ForecastState>('/api/v1/forecast-state', { signal });
}

export function listForecasts(
  params: { list_state?: ListState; object_id?: string; cursor?: string; limit?: number; released_since?: string } = {},
  signal?: AbortSignal,
): Promise<ForecastList> {
  return apiRequest<ForecastList>(`/api/v1/forecasts${query({ limit: 100, ...params })}`, { signal });
}

/** Все страницы списка (открытых карточек не больше max_open, обычно одна страница). */
export async function listAllForecasts(listState: ListState, signal?: AbortSignal, releasedSince?: string): Promise<ForecastList> {
  const first = await listForecasts({ list_state: listState, released_since: releasedSince }, signal);
  let page = first;
  const items = [...first.items];
  for (let guard = 0; page.next_cursor && guard < 20; guard += 1) {
    page = await listForecasts({ list_state: listState, cursor: page.next_cursor, released_since: releasedSince }, signal);
    items.push(...page.items);
  }
  return { ...first, items, next_cursor: null };
}

export function getForecast(forecastId: string, signal?: AbortSignal): Promise<ForecastCardView> {
  return apiRequest<ForecastCardView>(`/api/v1/forecasts/${encodeURIComponent(forecastId)}`, { signal });
}

export function listDecisions(forecastId: string, signal?: AbortSignal): Promise<ForecastDecisionList> {
  return apiRequest<ForecastDecisionList>(
    `/api/v1/forecasts/${encodeURIComponent(forecastId)}/decisions`,
    { signal },
  );
}

export function createDecision(
  forecastId: string,
  payload: ForecastDecisionCreate,
): Promise<ForecastDecisionSummary> {
  return apiRequest<ForecastDecisionSummary>(
    `/api/v1/forecasts/${encodeURIComponent(forecastId)}/decisions`,
    { method: 'POST', json: payload },
  );
}

/** История результатов проверки карточки (C0.4), новые сверху. */
export function listCheckResults(forecastId: string, signal?: AbortSignal): Promise<ForecastCheckResultList> {
  return apiRequest<ForecastCheckResultList>(`/api/v1/forecasts/${encodeURIComponent(forecastId)}/check-results`, { signal });
}

/** «Внести результат проверки» (C0.4): ревизия с аудитом; повтор с тем же ключом не пишет дубль. */
export function createCheckResult(forecastId: string, payload: ForecastCheckResultCreate): Promise<ForecastCheckResult> {
  return apiRequest<ForecastCheckResult>(
    `/api/v1/forecasts/${encodeURIComponent(forecastId)}/check-result`,
    { method: 'POST', json: payload },
  );
}

export interface JournalFilters {
  list_state?: ListState;
  /** `none` — «без решения» (C0.4). */
  decision_state?: 'none' | 'any';
  /** `unknown` — «исход неизвестен» (C0.4). */
  outcome?: ForecastOutcome['status'];
  object_id?: string;
  issued_from?: string;
  issued_to?: string;
  cursor?: string;
  limit?: number;
}

export function getJournal(filters: JournalFilters, signal?: AbortSignal): Promise<ForecastJournalList> {
  return apiRequest<ForecastJournalList>(`/api/v1/forecast-journal${query({ limit: 50, ...filters })}`, { signal });
}

export function getJournalCounts(signal?: AbortSignal): Promise<ForecastJournalCounts> {
  return apiRequest<ForecastJournalCounts>('/api/v1/forecast-journal/summary', { signal });
}

/** «Прогноз против факта» с отношениями: право `research` (аналитик, администратор). */
export function getJournalQuality(signal?: AbortSignal): Promise<ForecastQualitySummary> {
  return apiRequest<ForecastQualitySummary>('/api/v1/forecast-journal/quality', { signal });
}
