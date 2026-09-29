import { useId } from 'react';

/**
 * Знаки смысла, различимые без цвета: форма + подпись рядом.
 * - прогноз — квадрат с диагональной штриховкой (янтарный);
 * - текущая тревога источника — залитый квадрат с «!» (красный);
 * - снята по событию — квадрат с пунктирной рамкой (серый).
 */

export function ForecastMark({ size = 14, title }: { size?: number; title?: string }) {
  const id = `hatch-${useId().replace(/:/g, '')}`;
  return (
    <svg className="icon-inline" width={size} height={size} viewBox="0 0 14 14" role={title ? 'img' : undefined}
      aria-label={title} aria-hidden={title ? undefined : true}>
      <defs>
        <pattern id={id} width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="2" height="4" fill="var(--forecast-fill)" />
        </pattern>
      </defs>
      <rect x="0.5" y="0.5" width="13" height="13" fill={`url(#${id})`} stroke="var(--forecast-fill)" />
    </svg>
  );
}

export function AlarmMark({ size = 14, title }: { size?: number; title?: string }) {
  return (
    <svg className="icon-inline" width={size} height={size} viewBox="0 0 14 14" role={title ? 'img' : undefined}
      aria-label={title} aria-hidden={title ? undefined : true}>
      <rect x="0.5" y="0.5" width="13" height="13" fill="var(--attention-fill)" />
      <rect x="6" y="2.5" width="2" height="6" fill="var(--surface)" />
      <rect x="6" y="10" width="2" height="2" fill="var(--surface)" />
    </svg>
  );
}

export function ReleasedMark({ size = 14, title }: { size?: number; title?: string }) {
  return (
    <svg className="icon-inline" width={size} height={size} viewBox="0 0 14 14" role={title ? 'img' : undefined}
      aria-label={title} aria-hidden={title ? undefined : true}>
      <rect x="1" y="1" width="12" height="12" fill="none" stroke="var(--neutral-fill)" strokeWidth="1.5" strokeDasharray="2.5 2" />
    </svg>
  );
}
