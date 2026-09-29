import {
  createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode,
} from 'react';
import { getMe, login, logout, type Me, type Role } from '../api/auth';
import { ApiError, setUnauthorizedHandler } from '../api/http';
import { can as canRole, type Permission } from '../shared/config/permissions';

export type SessionStatus = 'loading' | 'ready' | 'unauthenticated' | 'error';

interface SessionValue {
  status: SessionStatus;
  me: Me | null;
  error: unknown;
  /** Роли сессии с учётом проверочной подмены в dev/fixture. */
  roles: Role[];
  can: (permission: Permission) => boolean;
  /** Проверочная подмена роли: только без LDAP и только в dev или fixture-сборке. */
  devOverrideAllowed: boolean;
  devRole: Role | null;
  setDevRole: (role: Role | null) => void;
  refresh: () => void;
  signIn: (username: string, password: string) => Promise<Me>;
  signOut: () => Promise<void>;
}

const SessionCtx = createContext<SessionValue | null>(null);

const DEV_ROLE_KEY = 'infra-dev-role';
const DEV_BUILD = import.meta.env.DEV || import.meta.env.VITE_DATA_MODE === 'fixture';
const ROLES: Role[] = ['dispatcher', 'analyst', 'admin'];

function initialDevRole(): Role | null {
  if (!DEV_BUILD) return null;
  try {
    const fromUrl = new URLSearchParams(window.location.search).get('dev_role');
    if (fromUrl !== null) {
      const role = ROLES.includes(fromUrl as Role) ? (fromUrl as Role) : null;
      if (role) sessionStorage.setItem(DEV_ROLE_KEY, role);
      else sessionStorage.removeItem(DEV_ROLE_KEY);
      return role;
    }
    const saved = sessionStorage.getItem(DEV_ROLE_KEY);
    return ROLES.includes(saved as Role) ? (saved as Role) : null;
  } catch {
    return null;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>('loading');
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [devRole, setDevRoleState] = useState<Role | null>(initialDevRole);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setMe(null);
      setStatus('unauthenticated');
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    getMe(controller.signal)
      .then((value) => {
        setMe(value);
        setError(null);
        setStatus('ready');
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setMe(null);
        setError(cause);
        setStatus(cause instanceof ApiError && cause.status === 401 ? 'unauthenticated' : 'error');
      });
    return () => controller.abort();
  }, [revision]);

  const devOverrideAllowed = DEV_BUILD && me !== null && me.auth_source !== 'ldap';

  const roles = useMemo<Role[]>(() => {
    if (!me) return [];
    if (devOverrideAllowed && devRole && me.roles.includes(devRole)) return [devRole];
    return me.roles;
  }, [me, devOverrideAllowed, devRole]);

  const setDevRole = useCallback((role: Role | null) => {
    setDevRoleState(role);
    try {
      if (role) sessionStorage.setItem(DEV_ROLE_KEY, role);
      else sessionStorage.removeItem(DEV_ROLE_KEY);
    } catch {
      /* sessionStorage недоступен — подмена живёт до перезагрузки */
    }
  }, []);

  const signIn = useCallback(async (username: string, password: string) => {
    const value = await login(username, password);
    setMe(value);
    setError(null);
    setStatus('ready');
    return value;
  }, []);

  const signOut = useCallback(async () => {
    try {
      await logout();
    } finally {
      setMe(null);
      setStatus('unauthenticated');
    }
  }, []);

  const value = useMemo<SessionValue>(() => ({
    status,
    me,
    error,
    roles,
    can: (permission) => canRole(roles, permission),
    devOverrideAllowed,
    devRole,
    setDevRole,
    refresh: () => {
      setStatus('loading');
      setRevision((current) => current + 1);
    },
    signIn,
    signOut,
  }), [status, me, error, roles, devOverrideAllowed, devRole, setDevRole, signIn, signOut]);

  return <SessionCtx.Provider value={value}>{children}</SessionCtx.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionCtx);
  if (!value) throw new Error('SessionProvider is missing');
  return value;
}
