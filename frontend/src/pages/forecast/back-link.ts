import { ROUTES } from '../../shared/config/routes';

/** Возврат из карточки туда, откуда её открыли: «← К прогнозу / К схеме / К журналу». */
export function backLink(raw: string | null, fallback: string = ROUTES.forecast): { to: string; label: string } {
  const to = raw && raw.startsWith('/') && !raw.startsWith('//') ? raw : fallback;
  if (to.startsWith(ROUTES.scheme)) return { to, label: 'К схеме' };
  if (to.startsWith(ROUTES.journal)) return { to, label: 'К журналу' };
  if (to.startsWith(ROUTES.upload)) return { to, label: 'К загрузке' };
  return { to, label: 'К прогнозу' };
}

export function cardHref(forecastId: string, back?: string): string {
  const base = `${ROUTES.forecast}/${encodeURIComponent(forecastId)}`;
  return back ? `${base}?back=${encodeURIComponent(back)}` : base;
}
