import {
  RISK_LEVEL_HINT, RISK_LEVEL_LABEL, RISK_LEVEL_TEXT, RISK_LEVEL_UNKNOWN_HINT,
} from '../config/wording';
import { RISK_LEVEL_BARS, countRiskLevels, riskLevelOf, riskLevelText } from '../lib/risk-level';
import './RiskLevel.css';

/** Три столбика по высоте: закрашено 3 — высокий, 2 — средний, 1 — низкий, 0 — не определён. */
function RiskBars({ filled }: { filled: number }) {
  return (
    <svg className="risk-level__bars" width="12" height="12" viewBox="0 0 12 12" aria-hidden>
      {[0, 1, 2].map((index) => {
        const height = 4 + index * 3.5;
        return (
          <rect key={index} x={0.5 + index * 4} y={11.5 - height} width="3" height={height}
            className={index < filled ? 'risk-level__bar risk-level__bar--on' : 'risk-level__bar'} />
        );
      })}
    </svg>
  );
}

/**
 * Метка уровня риска карточки (`risk_level`, ТЗ §10). Цвет + знак + текст: уровни различимы
 * и без цвета. Палитра прогноза (янтарная), не тревожная: это оценка на 14 сут, а не
 * сообщение СМВУ. Подписи — `wording.ts`.
 *
 * В строке списка: `<RiskLevelMark level={item.card.risk_level} />` («высокий риск»);
 * в карточке: `variant="full"` («Уровень риска: высокий»).
 */
export function RiskLevelMark({
  level, variant = 'short', size = 'default', className,
}: {
  level: string | null | undefined;
  variant?: 'short' | 'full';
  /** `small` — для узких списков (левая панель схемы). */
  size?: 'default' | 'small';
  className?: string;
}) {
  const value = riskLevelOf(level);
  const hint = value === 'unknown' ? RISK_LEVEL_UNKNOWN_HINT : RISK_LEVEL_HINT;
  const classes = ['risk-level', `risk-level--${value}`, size === 'small' ? 'risk-level--small' : '', className ?? ''].filter(Boolean).join(' ');
  return (
    <span className={classes} title={hint}>
      <RiskBars filled={RISK_LEVEL_BARS[value]} />
      <span>{riskLevelText(value, variant)}</span>
    </span>
  );
}

/** Сводка над списком: «Уровень риска: высокий 4 · средний 6». Пустой список — ничего. */
export function RiskLevelSummary({ levels }: { levels: readonly (string | null | undefined)[] }) {
  const counts = countRiskLevels(levels);
  if (!counts.length) return null;
  return (
    <span className="risk-level-summary">
      <span className="risk-level-summary__label">{RISK_LEVEL_LABEL}:</span>
      {counts.map(({ level, count }) => (
        <span key={level} className={`risk-level risk-level--${level}`} title={level === 'unknown' ? RISK_LEVEL_UNKNOWN_HINT : RISK_LEVEL_HINT}>
          <RiskBars filled={RISK_LEVEL_BARS[level]} />
          <span>{RISK_LEVEL_TEXT[level]} <strong>{count}</strong></span>
        </span>
      ))}
    </span>
  );
}
