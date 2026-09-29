import type { components } from './schema';
import { apiRequest } from './http';

export type Me = components['schemas']['Me'];
export type Role = Me['roles'][number];

export function getMe(signal?: AbortSignal): Promise<Me> {
  return apiRequest<Me>('/api/v1/auth/me', { signal, skipUnauthorizedHandler: true });
}

export function login(username: string, password: string): Promise<Me> {
  return apiRequest<Me>('/api/v1/auth/login', {
    method: 'POST',
    json: { username, password },
    skipUnauthorizedHandler: true,
  });
}

export function logout(): Promise<void> {
  return apiRequest<void>('/api/v1/auth/logout', { method: 'POST', skipUnauthorizedHandler: true });
}
