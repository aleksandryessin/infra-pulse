import type { components } from './schema';
import { apiRequest } from './http';

type S = components['schemas'];
export type ResearchSummary = S['ResearchSummary'];
export type ResearchScope = S['ResearchScope'];
export type ResearchMetric = S['ResearchMetric'];
export type ResearchLadderRow = S['ResearchLadderRow'];
export type ResearchBlock = S['ResearchBlock'];

/** «Исследование»: право `research` (аналитик, администратор); диспетчер получает 403. */
export function getResearchSummary(signal?: AbortSignal): Promise<ResearchSummary> {
  return apiRequest<ResearchSummary>('/api/v1/research-summary', { signal });
}
