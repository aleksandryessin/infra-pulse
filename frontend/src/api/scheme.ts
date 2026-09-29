import type { components } from './schema';
import { apiRequest } from './http';

type S = components['schemas'];
export type ObjectSchemeList = S['ObjectSchemeList'];
export type ObjectSchemeSummary = S['ObjectSchemeSummary'];
export type ObjectScheme = S['ObjectScheme'];
export type SchemeFeeder = S['SchemeFeeder'];
export type SchemeLandmark = S['SchemeLandmark'];
export type SchemePicket = S['SchemePicket'];
export type SchemeAlarm = S['SchemeAlarm'];
/** Связь, записанная в названии канала (C0.3): только подпись, топология из неё не выводится. */
export type SchemeNamedLink = S['SchemeNamedLink'];

export function listSchemes(signal?: AbortSignal): Promise<ObjectSchemeList> {
  return apiRequest<ObjectSchemeList>('/api/v1/schemes', { signal });
}

export function getScheme(objectId: string, signal?: AbortSignal): Promise<ObjectScheme> {
  return apiRequest<ObjectScheme>(`/api/v1/schemes/${encodeURIComponent(objectId)}`, { signal });
}
