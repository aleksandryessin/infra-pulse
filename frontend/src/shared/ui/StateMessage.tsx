import type { ReactNode } from 'react';
import { Button } from 'antd';
import {
  IconAlertTriangle, IconClockExclamation, IconInbox, IconLoader2, IconLock, IconRefresh,
} from '@tabler/icons-react';
import { describeApiError, errorCode } from '../../api/http';
import './StateMessage.css';

export type StateKind = 'loading' | 'empty' | 'error' | 'denied' | 'stale';

const ICONS = {
  loading: IconLoader2,
  empty: IconInbox,
  error: IconAlertTriangle,
  denied: IconLock,
  stale: IconClockExclamation,
} as const;

/**
 * Состояния loading / empty / error / denied / stale — текстом и значком, не только цветом.
 * Ошибка API показывается как ошибка: данные fixture вместо неё не подставляются.
 */
export function StateMessage({
  kind, title, children, error, onRetry, action, compact,
}: {
  kind: StateKind;
  title: ReactNode;
  children?: ReactNode;
  error?: unknown;
  onRetry?: () => void;
  action?: ReactNode;
  compact?: boolean;
}) {
  const Icon = ICONS[kind];
  const code = error ? errorCode(error) : null;
  return (
    <div className={`state-message state-message--${kind}${compact ? ' state-message--compact' : ''}`}
      role={kind === 'error' || kind === 'denied' ? 'alert' : 'status'}>
      <Icon size={20} stroke={1.75} className="state-message__icon" aria-hidden />
      <div className="state-message__text">
        <strong>{title}</strong>
        {error ? <span>{describeApiError(error)}{code ? <span className="state-message__code"> · {code}</span> : null}</span> : null}
        {children ? <span>{children}</span> : null}
      </div>
      {onRetry || action ? (
        <div className="state-message__actions">
          {action}
          {onRetry ? <Button icon={<IconRefresh size={16} />} onClick={onRetry}>Повторить</Button> : null}
        </div>
      ) : null}
    </div>
  );
}
