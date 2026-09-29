import { Link } from 'react-router-dom';
import { ROUTES } from '../../shared/config/routes';
import { StateMessage } from '../../shared/ui/StateMessage';

export function NotFoundPage() {
  return (
    <div className="stack" style={{ maxWidth: 760 }}>
      <StateMessage kind="empty" title="Страница не найдена" action={<Link to={ROUTES.forecast}>К прогнозу</Link>}>
        Такого раздела нет. Разделы перечислены в меню слева.
      </StateMessage>
    </div>
  );
}
