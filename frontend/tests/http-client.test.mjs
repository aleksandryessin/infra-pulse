import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ApiError, apiRequest, describeApiError, setUnauthorizedHandler } from '../src/api/http.ts';

function stubFetch(status, body, headers = {}) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json', ...headers },
    });
  };
  return calls;
}

test('every POST carries X-CSRF-Token and the session cookie policy; GET does not need it', async () => {
  const calls = stubFetch(200, { ok: true }, { 'X-CSRF-Token': 'issued-by-server' });
  await apiRequest('/api/v1/auth/me');
  await apiRequest('/api/v1/forecasts/x/decisions', { method: 'POST', json: { a: 1 } });
  assert.equal(calls[0].init.headers['X-CSRF-Token'], undefined);
  assert.equal(calls[1].init.headers['X-CSRF-Token'], 'issued-by-server');
  assert.equal(calls[1].init.credentials, 'same-origin');
});

test('401 goes to the session handler, auth routes can opt out; errors are never replaced by data', async () => {
  let redirected = 0;
  setUnauthorizedHandler(() => { redirected += 1; });
  stubFetch(401, { detail: 'not_authenticated' });
  await assert.rejects(apiRequest('/api/v1/forecast-state'), (error) => error instanceof ApiError && error.status === 401);
  await assert.rejects(apiRequest('/api/v1/auth/me', { skipUnauthorizedHandler: true }));
  assert.equal(redirected, 1);
  setUnauthorizedHandler(null);
});

test('403, 429 and 503 are explained in plain text', async () => {
  stubFetch(503, { detail: 'decisions_not_implemented' });
  const unavailable = await apiRequest('/x').catch((error) => error);
  assert.equal(describeApiError(unavailable), 'Сохранение решений на сервере ещё не подключено');
  stubFetch(429, { detail: 'too_many' }, { 'Retry-After': '30' });
  const limited = await apiRequest('/x').catch((error) => error);
  assert.equal(describeApiError(limited), 'Слишком много запросов, повторите через 30 с');
  stubFetch(403, { detail: 'forbidden_role' });
  const denied = await apiRequest('/x').catch((error) => error);
  assert.equal(describeApiError(denied), 'Нет доступа для вашей роли');
});

test('the anti-CSRF value comes from the B3 cookie __Host-infrapulse-csrf', async () => {
  const previous = globalThis.document;
  globalThis.document = { cookie: 'other=x; __Host-infrapulse-csrf=token-from-b3' };
  try {
    const calls = stubFetch(200, { ok: true });
    await apiRequest('/api/v1/auth/logout', { method: 'POST' });
    assert.equal(calls[0].init.headers['X-CSRF-Token'], 'token-from-b3');
  } finally {
    globalThis.document = previous;
  }
});
