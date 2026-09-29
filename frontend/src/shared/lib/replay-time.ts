/** Keep a linked replay moment only when it is aware and inside the loaded window. */
export function linkedReplayAsOf(
  value: string | null,
  windowStart: string,
  windowEnd: string,
): string | null {
  if (!value || !/(?:[zZ]|[+-]\d{2}:\d{2})$/.test(value)) return null;
  const moment = Date.parse(value);
  const start = Date.parse(windowStart);
  const end = Date.parse(windowEnd);
  if (!Number.isFinite(moment) || !Number.isFinite(start) || !Number.isFinite(end)) return null;
  if (moment < start || moment > end) return null;
  return value;
}
