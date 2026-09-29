/**
 * Признак тревожности источника в исходном сообщении. В CSV Приложения 1 ТЗ флага нет —
 * `alarm` приходит как NULL: это «не передан», а не «нет» и не false.
 */
export function alarmFlagText(alarm: boolean | null | undefined): string {
  if (alarm === true) return 'alarm=true';
  if (alarm === false) return 'alarm=false';
  return 'alarm: не передан';
}

export function alarmFlagMissing(alarm: boolean | null | undefined): boolean {
  return alarm === null || alarm === undefined;
}
