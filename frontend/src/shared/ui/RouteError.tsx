import { Button } from 'antd';
import { IconRefresh } from '@tabler/icons-react';
import { Link } from 'react-router-dom';
import { ROUTES } from '../config/routes';
import { ROUTE_ERROR_TEXT, ROUTE_ERROR_TITLE } from '../config/wording';
import { StateMessage } from './StateMessage';

/**
 * `errorElement` маршрутов (I4 аудита 29.09): сбой отрисовки раздела — русский текст,
 * «Обновить страницу» и «К прогнозу». Текст ошибки и стек не показываются: подробности
 * пишет в консоль сам React. Внутри раздела меню и шапка остаются (`fullScreen` — только
 * для сбоя самой оболочки или страницы входа).
 */
export function RouteError({ fullScreen = false }: { fullScreen?: boolean }) {
  const message = (
    <div className="stack route-error" style={{ maxWidth: 760 }}>
      <StateMessage
        kind="error"
        title={ROUTE_ERROR_TITLE}
        action={(
          <>
            <Button type="primary" icon={<IconRefresh size={16} />} onClick={() => window.location.reload()}>
              Обновить страницу
            </Button>
            <Link className="route-error__link" to={ROUTES.forecast}>К прогнозу</Link>
          </>
        )}
      >
        {ROUTE_ERROR_TEXT}
      </StateMessage>
    </div>
  );
  return fullScreen ? <main className="full-screen">{message}</main> : message;
}
