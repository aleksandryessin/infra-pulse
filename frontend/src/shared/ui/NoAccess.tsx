import { Link } from 'react-router-dom';
import { useSession } from '../../providers/SessionProvider';
import { ROLE_LABELS, rolesAllowed, type Permission } from '../config/permissions';
import { ROUTES } from '../config/routes';
import { StateMessage } from './StateMessage';

/** Прямой URL чужого раздела: «Нет доступа», без данных раздела и без запроса к API. */
export function NoAccess({ section, permission }: { section: string; permission: Permission }) {
  const { roles } = useSession();
  const current = roles.map((role) => ROLE_LABELS[role]).join(', ') || 'нет роли';
  return (
    <div className="stack" style={{ maxWidth: 760 }}>
      <StateMessage kind="denied" title="Нет доступа"
        action={<Link to={ROUTES.forecast}>К прогнозу</Link>}>
        Раздел «{section}» доступен ролям: {rolesAllowed(permission)}. Ваша роль: {current}.
      </StateMessage>
    </div>
  );
}
