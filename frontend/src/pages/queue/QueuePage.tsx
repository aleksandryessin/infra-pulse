import { Link } from 'react-router-dom';
import { ROUTES } from '../../shared/config/routes';
import { StateMessage } from '../../shared/ui/StateMessage';

/**
 * Исходные сообщения и журнал заметок к ним читают PostgreSQL (replay/received).
 * В других режимах раздел не подменяется демонстрационными записями.
 */
export function SourceMessagesUnavailable() {
  return (
    <div className="stack" style={{ maxWidth: 820 }}>
      <StateMessage kind="empty" title="Полный список исходных сообщений недоступен в этом режиме"
        action={<Link to={ROUTES.forecast}>К прогнозу</Link>}>
        Он работает на стенде с подключённой базой данных, в демонстрации на синтетических данных его нет.
        Тревожные сообщения СМВУ за сутки — в блоке «Сейчас» на странице прогноза и на схеме объекта.
      </StateMessage>
    </div>
  );
}
