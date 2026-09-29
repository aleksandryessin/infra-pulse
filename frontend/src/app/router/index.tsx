import { lazy } from 'react';
import { createBrowserRouter, Navigate, useSearchParams } from 'react-router-dom';
import { AppLayout } from '../layouts/AppLayout';
import { ROUTES, SOURCE_MESSAGES_AVAILABLE } from '../../shared/config/routes';
import { ForecastPage } from '../../pages/forecast/ForecastPage';
import { ForecastCardPage } from '../../pages/forecast/ForecastCardPage';
import { SchemePage } from '../../pages/scheme/SchemePage';
import { JournalPage } from '../../pages/journal/JournalPage';
import { JournalSnapshotPage } from '../../pages/journal/JournalSnapshotPage';
import { LoginPage } from '../../pages/login/LoginPage';
import { NotFoundPage } from '../../pages/not-found/NotFoundPage';
import { RouteError } from '../../shared/ui/RouteError';
import { SourceMessagesUnavailable } from '../../pages/queue/QueuePage';
import { RequirePermission, RequireSession } from './guards';

// Разделы аналитика и администратора — отдельные чанки: их тексты не попадают в бандл диспетчера.
const ResearchPage = lazy(() => import('../../pages/research/ResearchPage'));
const UploadPage = lazy(() => import('../../pages/upload/UploadPage'));
const SourceMessagesPage = lazy(() => import('../../pages/queue/SourceMessagesRoute'));
const SourceJournalPage = lazy(() => import('../../pages/journal/SourceJournalRoute'));

/** Прежняя `/map` заменена схемой; объект из ссылки сохраняется. */
function MapRedirect() {
  const [params] = useSearchParams();
  const object = params.get('object') ?? params.get('object_id');
  return <Navigate to={object && object !== '__unknown__' ? `${ROUTES.scheme}?object=${encodeURIComponent(object)}` : ROUTES.scheme} replace />;
}

// Сбой отрисовки раздела показывается внутри оболочки (меню и шапка остаются); сбой самой
// оболочки или входа — на весь экран. Английский экран React Router со стеком не показывается.
export const router = createBrowserRouter([
  { path: ROUTES.login, element: <LoginPage />, errorElement: <RouteError fullScreen /> },
  {
    path: '/',
    element: <RequireSession><AppLayout /></RequireSession>,
    errorElement: <RouteError fullScreen />,
    children: [{
      errorElement: <RouteError />,
      children: [
        { index: true, element: <Navigate to={ROUTES.forecast} replace /> },
        { path: 'forecast', element: <ForecastPage /> },
        { path: 'forecast/:forecastId', element: <ForecastCardPage /> },
        { path: 'scheme', element: <SchemePage /> },
        { path: 'journal', element: <JournalPage /> },
        { path: 'journal/:position', element: <JournalSnapshotPage /> },
        {
          path: 'research',
          element: <RequirePermission permission="research" section="Исследование"><ResearchPage /></RequirePermission>,
        },
        {
          path: 'upload',
          element: <RequirePermission permission="import" section="Загрузка"><UploadPage /></RequirePermission>,
        },
        { path: 'queue', element: SOURCE_MESSAGES_AVAILABLE ? <SourceMessagesPage /> : <SourceMessagesUnavailable /> },
        { path: 'queue/journal', element: SOURCE_MESSAGES_AVAILABLE ? <SourceJournalPage /> : <SourceMessagesUnavailable /> },
        { path: 'map', element: <MapRedirect /> },
        { path: '*', element: <NotFoundPage /> },
      ],
    }],
  },
]);
