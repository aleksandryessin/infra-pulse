import { Layout, Switch } from 'antd';
import { IconClockExclamation, IconMoon, IconSun } from '@tabler/icons-react';
import { useLocation } from 'react-router-dom';
import { describeApiError } from '../../api/http';
import { useForecastState } from '../../providers/ForecastStateProvider';
import { useNotifications } from '../../providers/NotificationsProvider';
import { useSession } from '../../providers/SessionProvider';
import { useThemeMode } from '../../providers/ThemeProvider';
import { headerModeLabel } from '../../shared/config/labels';
import { sectionFor } from '../../shared/config/routes';
import { fmtDateTime, fmtShortDateTime, fmtTime } from '../../shared/lib/format';
import { NotificationsBell } from './NotificationsBell';
import './Header.css';

function pageTitle(pathname: string): string {
  if (/^\/forecast\/.+/.test(pathname)) return 'Карточка прогноза';
  if (/^\/journal\/.+/.test(pathname)) return 'Снимок карточки';
  if (pathname.startsWith('/queue/journal')) return 'Журнал исходных сообщений';
  const section = sectionFor(pathname);
  return section?.heading ?? section?.title ?? 'Страница не найдена';
}

/**
 * Два подписанных среза на каждом экране (C0.4): прогноз — `forecast_data_as_of`, сообщения
 * СМВУ — `source_messages_as_of` (может быть позже: сообщения идут между пересчётами).
 * Не текущее время. До C0.4 на сервере — `data_as_of` и `as_of` колокольчика.
 * Время публикации и последней проверки — только в подсказке блока (замечание владельца 29.09:
 * «опубликовано · проверено» — служебные отметки); сбой обновления виден строкой.
 */
export function DataAsOf() {
  const { state, error, loading, lastSuccessAt, lastErrorAt } = useForecastState();
  const { summary } = useNotifications();
  const sourceAt = state?.source_messages_as_of ?? summary?.as_of ?? null;
  const forecastAt = state?.forecast_data_as_of ?? state?.data_as_of ?? null;
  const source = <span className="data-as-of__line">Сообщения СМВУ: {sourceAt ? `до ${fmtDateTime(sourceAt)} МСК` : '—'}</span>;
  if (!state) {
    return (
      <div className="data-as-of" role="status">
        <strong>{loading ? 'Прогноз: загрузка…' : 'Прогноз: данные недоступны'}</strong>
        {source}
        {!loading && error ? <span className="data-as-of__meta">{describeApiError(error)}</span> : null}
      </div>
    );
  }
  const stale = error !== null && lastErrorAt !== null;
  const meta = `${state.published_at ? `Прогноз опубликован ${fmtShortDateTime(state.published_at)}` : 'Прогноз ещё не опубликован'}${lastSuccessAt ? ` · экран обновлён в ${fmtTime(lastSuccessAt)}` : ''}`;
  return (
    <div className="data-as-of" role="status" title={meta}>
      <strong>{forecastAt ? `Прогноз: данные до ${fmtDateTime(forecastAt)} МСК` : 'Прогноз: данные ещё не опубликованы'}</strong>
      {source}
      {stale ? (
        <span className="data-as-of__stale">
          <IconClockExclamation size={14} aria-hidden className="icon-inline" /> Обновление не удалось в {fmtTime(lastErrorAt)}: {describeApiError(error)}. Показаны прежние данные.
        </span>
      ) : null}
    </div>
  );
}

export function AppHeader() {
  const { pathname } = useLocation();
  const { mode, toggle } = useThemeMode();
  const { state } = useForecastState();
  const { roles } = useSession();
  const modeLabel = state
    ? headerModeLabel(state.mode, roles.includes('admin'))
    : headerModeLabel(import.meta.env.VITE_DATA_MODE, roles.includes('admin'), true);

  return (
    <Layout.Header className="app-header">
      <h1 className="app-header__title">{pageTitle(pathname)}</h1>
      <div className="app-header__right">
        <DataAsOf />
        {modeLabel ? <span className="app-header__mode">{modeLabel}</span> : null}
        <NotificationsBell />
        <label className="app-header__theme">
          {mode === 'dark' ? <IconMoon size={16} aria-hidden /> : <IconSun size={16} aria-hidden />}
          Тёмная тема
          <Switch checked={mode === 'dark'} onChange={toggle} aria-label="Тёмная тема" />
        </label>
      </div>
    </Layout.Header>
  );
}
