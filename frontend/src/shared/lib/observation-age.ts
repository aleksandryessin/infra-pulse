/** Elapsed time at the selected API moment, including historical replay. */
export function elapsedSinceAt(asOf: string, observedAt: string): string {
  const elapsedMs = Date.parse(asOf) - Date.parse(observedAt);
  if (!Number.isFinite(elapsedMs)) return 'не определяется';
  if (elapsedMs < 0) return 'время позже момента просмотра';
  if (elapsedMs < 60_000) return 'менее минуты';

  const minutes = Math.floor(elapsedMs / 60_000);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч ${minutes % 60} мин`;
  const days = Math.floor(hours / 24);
  return `${days} д ${hours % 24} ч`;
}

/** Compare source and local import timestamps without inferring network latency. */
export function sourceImportTimeDifference(eventAt: string, importedAt: string): string {
  const eventMs = Date.parse(eventAt);
  const importMs = Date.parse(importedAt);
  if (!Number.isFinite(eventMs) || !Number.isFinite(importMs)) return 'не определяется';
  if (eventMs === importMs) return 'метки совпадают';
  if (eventMs < importMs) {
    return `источник раньше импорта на ${elapsedSinceAt(importedAt, eventAt)}`;
  }
  return `источник позже импорта на ${elapsedSinceAt(eventAt, importedAt)}`;
}
