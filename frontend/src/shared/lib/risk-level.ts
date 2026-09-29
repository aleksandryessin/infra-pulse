import { RISK_LEVEL_LABEL, RISK_LEVEL_SHORT, RISK_LEVEL_TEXT } from '../config/wording.ts';

/** `risk_level` карточки из API; неизвестное и пустое значение — «не определён». */
export type RiskLevel = 'high' | 'medium' | 'low' | 'unknown';

export const RISK_LEVELS: readonly RiskLevel[] = ['high', 'medium', 'low', 'unknown'];

/** Число закрашенных столбиков знака: форма различает уровни без цвета. */
export const RISK_LEVEL_BARS: Record<RiskLevel, number> = { high: 3, medium: 2, low: 1, unknown: 0 };

export function riskLevelOf(value: string | null | undefined): RiskLevel {
  return RISK_LEVELS.includes(value as RiskLevel) ? (value as RiskLevel) : 'unknown';
}

/** Подпись метки: коротко в строке списка, полностью — в карточке. */
export function riskLevelText(value: string | null | undefined, variant: 'short' | 'full' = 'short'): string {
  const level = riskLevelOf(value);
  return variant === 'full' ? `${RISK_LEVEL_LABEL}: ${RISK_LEVEL_TEXT[level]}` : RISK_LEVEL_SHORT[level];
}

/** Сводка над списком: сколько карточек каждого уровня (порядок — от высокого). */
export function countRiskLevels(levels: readonly (string | null | undefined)[]): { level: RiskLevel; count: number }[] {
  const counts = new Map<RiskLevel, number>();
  for (const value of levels) {
    const level = riskLevelOf(value);
    counts.set(level, (counts.get(level) ?? 0) + 1);
  }
  return RISK_LEVELS.filter((level) => counts.has(level)).map((level) => ({ level, count: counts.get(level) ?? 0 }));
}
