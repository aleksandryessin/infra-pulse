import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useSession } from '../../providers/SessionProvider';
import type { Permission } from '../../shared/config/permissions';
import { ROUTES } from '../../shared/config/routes';
import { NoAccess } from '../../shared/ui/NoAccess';
import { StateMessage } from '../../shared/ui/StateMessage';

function FullScreen({ children }: { children: ReactNode }) {
  return <main className="full-screen">{children}</main>;
}

/** Сессия обязательна: 401 ведёт на вход с возвратом; сбой входа показывается текстом. */
export function RequireSession({ children }: { children: ReactNode }) {
  const { status, error, refresh } = useSession();
  const location = useLocation();
  if (status === 'loading') {
    return <FullScreen><StateMessage kind="loading" title="Проверка входа…" /></FullScreen>;
  }
  if (status === 'unauthenticated') {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={`${ROUTES.login}?next=${encodeURIComponent(next)}`} replace />;
  }
  if (status === 'error') {
    return (
      <FullScreen>
        <StateMessage kind="error" title="Не удалось проверить вход" error={error} onRetry={refresh}>
          Данные не показываются, пока сервер не подтвердит сессию.
        </StateMessage>
      </FullScreen>
    );
  }
  return <>{children}</>;
}

/** Чужой раздел по прямому URL: «Нет доступа», запрос к API не выполняется. */
export function RequirePermission({
  permission, section, children,
}: { permission: Permission; section: string; children: ReactNode }) {
  const { can } = useSession();
  return can(permission) ? <>{children}</> : <NoAccess section={section} permission={permission} />;
}
