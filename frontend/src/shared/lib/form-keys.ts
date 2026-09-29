/** Ключ идемпотентности записи: повтор после сбоя сети идёт с тем же ключом. */
export function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // randomUUID нет вне защищённого контекста (http на стенде) — ключ из getRandomValues.
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/** datetime-local в Москве (UTC+3 без перехода на летнее время) → aware ISO. */
export function mskLocalToIso(value: string): string {
  return `${value.length === 16 ? `${value}:00` : value}+03:00`;
}

/** Текущее московское время для `max` у datetime-local. */
export function mskNowLocal(): string {
  return new Date(Date.now() + 3 * 3600 * 1000).toISOString().slice(0, 16);
}
