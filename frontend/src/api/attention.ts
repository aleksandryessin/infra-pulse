import type { components } from './schema';
import { apiRequest } from './http';

export type Capabilities = components['schemas']['Capabilities'];
export type AttentionList = components['schemas']['AttentionList'];
export type AttentionEntry = components['schemas']['AttentionEntry'];
export type ObservedMessage = components['schemas']['ObservedMessage'];
export type ObjectAttentionList = components['schemas']['ObjectAttentionList'];
export type ChannelAttentionList = components['schemas']['ChannelAttentionList'];
export type SourceAlarmWindow = components['schemas']['SourceAlarmWindow'];
export type SourceAlarmObject = components['schemas']['SourceAlarmObject'];
export type ReplayCoverageObjectList = components['schemas']['ReplayCoverageObjectList'];
export type ReplayCoverageChannelList = components['schemas']['ReplayCoverageChannelList'];
export type ReviewNoteCreate = components['schemas']['ReviewNoteCreate'];
export type ReviewNote = components['schemas']['ReviewNote'];
export type ReviewNoteList = components['schemas']['ReviewNoteList'];
export type ReviewJournalList = components['schemas']['ReviewJournalList'];

function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  // Ошибка — ApiError с текстом `HTTP <код>`, как ожидают прежние страницы исходных сообщений.
  return apiRequest<T>(path, { signal });
}

/**
 * Блок «Сейчас» (F-04): тревожные сообщения СМВУ (`alarm=true`) по всем объектам с временем
 * события от `eventFrom` (сутки до среза). Сервер считает всё окно — сообщения, объекты, линии,
 * сообщения на линиях схемы — и отдаёт `limit` последних сообщений по времени события. Отказ не
 * подтверждён.
 */
export function getSourceAlarmWindow(
  eventFrom: string,
  limit: number,
  asOf?: string | null,
  signal?: AbortSignal,
): Promise<SourceAlarmWindow> {
  const query = new URLSearchParams({ event_from: eventFrom, limit: String(limit) });
  if (asOf) query.set('as_of', asOf);
  return getJson<SourceAlarmWindow>(`/api/v1/attention/source-alarms?${query}`, signal);
}

/**
 * Последние исходные записи полного потока (новые по приёму сверху) — «Сейчас» без отметки
 * «тревожное». `eventFrom` ограничивает чтение окном: без него сервер считает весь поток.
 */
export function getRecentMessages(
  limit: number,
  asOf?: string | null,
  signal?: AbortSignal,
  eventFrom?: string | null,
): Promise<AttentionList> {
  const query = new URLSearchParams({ view: 'all', offset: '0', limit: String(limit) });
  if (asOf) query.set('as_of', asOf);
  if (eventFrom) query.set('event_from', eventFrom);
  return getJson<AttentionList>(`/api/v1/attention?${query}`, signal);
}

/**
 * Момент чтения исходных сообщений: в replay сервер требует `as_of` — берётся водяной знак
 * опубликованного прогноза; в received и fixture сервер сам берёт текущий срез.
 */
export function sourceAsOf(state: { mode: string; data_as_of?: string | null } | null): string | null {
  return state?.mode === 'replay' ? state.data_as_of ?? null : null;
}

export function getCapabilities(signal?: AbortSignal, afterReceivedWatermark?: number): Promise<Capabilities> {
  const query = new URLSearchParams();
  if (afterReceivedWatermark !== undefined) {
    query.set('after_received_watermark', String(afterReceivedWatermark));
  }
  return getJson<Capabilities>(`/api/v1/capabilities${query.size ? `?${query}` : ''}`, signal);
}

export function getAttention(
  asOf: string | undefined,
  offset: number,
  limit: number,
  objectId?: string,
  view: 'attention' | 'all' = 'attention',
  channelId?: string,
  rowUid?: string,
  localNote: 'any' | 'present' | 'absent' = 'any',
  alarm?: boolean,
  signal?: AbortSignal,
  receivedWatermark?: number,
  eventAt?: string,
  afterReceivedWatermark?: number,
): Promise<AttentionList> {
  const query = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
    view,
  });
  if (asOf) query.set('as_of', asOf);
  if (objectId) query.set('object_id', objectId);
  if (channelId) query.set('channel_id', channelId);
  if (rowUid) query.set('row_uid', rowUid);
  if (localNote !== 'any') query.set('local_note', localNote);
  if (alarm !== undefined) query.set('alarm', String(alarm));
  if (receivedWatermark !== undefined) query.set('received_watermark', String(receivedWatermark));
  if (eventAt) query.set('event_at', eventAt);
  if (afterReceivedWatermark !== undefined) {
    query.set('after_received_watermark', String(afterReceivedWatermark));
  }
  return getJson<AttentionList>(`/api/v1/attention?${query}`, signal);
}

export function getObjectAttention(
  asOf?: string,
  offset = 0,
  limit = 25,
  candidateKind: 'all' | 'alarm' | 'text' | 'candidate' = 'all',
  signal?: AbortSignal,
  receivedWatermark?: number,
): Promise<ObjectAttentionList> {
  const query = new URLSearchParams({
    offset: String(offset), limit: String(limit), candidate_kind: candidateKind,
  });
  if (asOf) query.set('as_of', asOf);
  if (receivedWatermark !== undefined) query.set('received_watermark', String(receivedWatermark));
  return getJson<ObjectAttentionList>(`/api/v1/attention/objects?${query}`, signal);
}

export function getChannelAttention(
  asOf: string,
  objectId: string,
  offset: number,
  limit: number,
  channelId?: string,
  signal?: AbortSignal,
  receivedWatermark?: number,
  candidateKind: 'all' | 'alarm' | 'text' | 'candidate' = 'all',
): Promise<ChannelAttentionList> {
  const query = new URLSearchParams({
    as_of: asOf,
    object_id: objectId,
    offset: String(offset),
    limit: String(limit),
  });
  if (channelId) query.set('channel_id', channelId);
  if (receivedWatermark !== undefined) query.set('received_watermark', String(receivedWatermark));
  if (candidateKind !== 'all') query.set('candidate_kind', candidateKind);
  return getJson<ChannelAttentionList>(`/api/v1/attention/channels?${query}`, signal);
}

export function getReplayCoverageObjects(
  asOf: string,
  signal?: AbortSignal,
): Promise<ReplayCoverageObjectList> {
  const query = new URLSearchParams({ as_of: asOf });
  return getJson<ReplayCoverageObjectList>(`/api/v1/attention/coverage/objects?${query}`, signal);
}

export function getReplayCoverageChannels(
  asOf: string,
  objectId: string,
  offset: number,
  limit: number,
  signal?: AbortSignal,
): Promise<ReplayCoverageChannelList> {
  const query = new URLSearchParams({
    as_of: asOf,
    object_id: objectId,
    offset: String(offset),
    limit: String(limit),
  });
  return getJson<ReplayCoverageChannelList>(`/api/v1/attention/coverage/channels?${query}`, signal);
}

export function getReviews(rowUid: string, signal?: AbortSignal): Promise<ReviewNoteList> {
  return getJson<ReviewNoteList>(`/api/v1/attention/${encodeURIComponent(rowUid)}/reviews`, signal);
}

export function getReviewJournal(
  offset: number,
  limit: number,
  filters: { rowUid?: string; objectId?: string; channelId?: string } = {},
  signal?: AbortSignal,
  reviewWatermark?: number,
): Promise<ReviewJournalList> {
  const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
  if (filters.rowUid) query.set('row_uid', filters.rowUid);
  if (filters.objectId) query.set('object_id', filters.objectId);
  if (filters.channelId) query.set('channel_id', filters.channelId);
  if (reviewWatermark !== undefined) query.set('review_watermark', String(reviewWatermark));
  return getJson<ReviewJournalList>(`/api/v1/review-journal?${query}`, signal);
}

export function addReview(rowUid: string, payload: ReviewNoteCreate): Promise<ReviewNote> {
  return apiRequest<ReviewNote>(`/api/v1/attention/${encodeURIComponent(rowUid)}/reviews`, {
    method: 'POST',
    json: payload,
  });
}
