import { useState } from 'react';
import { Button, Input } from 'antd';
import { IconLogin } from '@tabler/icons-react';
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router-dom';
import { ApiError, describeApiError } from '../../api/http';
import { useSession } from '../../providers/SessionProvider';
import { ROUTES } from '../../shared/config/routes';
import { StateMessage } from '../../shared/ui/StateMessage';
import './LoginPage.css';

function safeNext(raw: string | null): string {
  return raw && raw.startsWith('/') && !raw.startsWith('//') && !raw.startsWith(ROUTES.login) ? raw : ROUTES.forecast;
}

function loginErrorText(error: unknown): string {
  if (error instanceof ApiError && error.kind === 'http') {
    if (error.status === 401) return 'Неверное имя пользователя или пароль.';
    if (error.status === 429) return `Слишком много попыток входа. ${error.retryAfter ? `Повторите через ${error.retryAfter} с.` : 'Повторите позже.'}`;
    if (error.status === 503) return `Каталог учётных записей недоступен — вход сейчас невозможен (${describeApiError(error)}).`;
    if (error.status === 422) return 'Имя пользователя: латинские буквы, цифры, точка, дефис или подчёркивание.';
  }
  return describeApiError(error);
}

/** Вход через каталог (B3): cookie-сессия; в dev_stub вход не настроен. */
export function LoginPage() {
  const { status, me, error, signIn, refresh } = useSession();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const next = safeNext(params.get('next'));
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [loginError, setLoginError] = useState<unknown>(null);

  if (status === 'ready' && me && me.auth_source === 'ldap') return <Navigate to={next} replace />;

  const submit = async () => {
    if (!username.trim() || !password) {
      setLoginError(null);
      return;
    }
    setBusy(true);
    setLoginError(null);
    try {
      await signIn(username.trim(), password);
      setPassword('');
      navigate(next, { replace: true });
    } catch (cause) {
      setLoginError(cause);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-panel" aria-labelledby="login-title">
        <h1 id="login-title">InfraPulse MSK — вход</h1>

        {status === 'loading' ? <StateMessage kind="loading" title="Проверка входа…" /> : null}

        {status === 'ready' && me && me.auth_source !== 'ldap' ? (
          <StateMessage kind="denied" title="Вход не настроен"
            action={<Link to={next}>Продолжить</Link>}>
            Сервер работает без каталога учётных записей ({me.auth_source}): все запросы выполняются
            от «{me.display_name}». Такой режим допустим только локально или на закрытом стенде.
          </StateMessage>
        ) : null}

        {status === 'error' ? (
          <StateMessage kind="error" title="Не удалось проверить вход" error={error} onRetry={refresh} />
        ) : null}

        {status === 'unauthenticated' || status === 'error' ? (
          <form className="login-form" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
            <label>
              <span>Имя пользователя</span>
              <Input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)}
                maxLength={128} autoFocus aria-label="Имя пользователя" />
            </label>
            <label>
              <span>Пароль</span>
              <Input.Password autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)}
                maxLength={256} aria-label="Пароль" />
            </label>
            {loginError ? <p className="login-form__error" role="alert">{loginErrorText(loginError)}</p> : null}
            <Button type="primary" htmlType="submit" icon={<IconLogin size={18} />} loading={busy} disabled={!username.trim() || !password} block>
              Войти
            </Button>
            <p className="muted small">Учётные записи — в каталоге организации. Сессия хранится в cookie; пароль в браузере не сохраняется.</p>
          </form>
        ) : null}
      </section>
    </main>
  );
}
