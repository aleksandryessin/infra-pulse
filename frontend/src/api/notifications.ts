import type { components } from './schema';
import { apiRequest } from './http';

type S = components['schemas'];
export type NotificationSummary = S['NotificationSummary'];
export type NotificationItem = S['NotificationItem'];

/** Колокольчик ТЗ §10 (N1): новые карточки и критические исходные сообщения после `since`. */
export function getNotifications(since: string, signal?: AbortSignal): Promise<NotificationSummary> {
  return apiRequest<NotificationSummary>(`/api/v1/notifications?since=${encodeURIComponent(since)}`, { signal });
}
