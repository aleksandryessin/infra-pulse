import assert from 'node:assert/strict';
import { test } from 'node:test';

import { subscribeActiveRefresh } from '../src/shared/lib/use-active-refresh.ts';

test('resumes a hidden queue immediately and keeps the 30-second poll', () => {
  let current = 0;
  let visibilityState = 'visible';
  let poll;
  let intervalMs;
  let cleared = false;
  let refreshes = 0;
  const browserWindow = new EventTarget();
  browserWindow.setInterval = (callback, ms) => {
    poll = callback;
    intervalMs = ms;
    return 1;
  };
  browserWindow.clearInterval = () => { cleared = true; };
  const browserDocument = new EventTarget();
  Object.defineProperty(browserDocument, 'visibilityState', {
    get: () => visibilityState,
  });

  const unsubscribe = subscribeActiveRefresh(
    browserWindow,
    browserDocument,
    () => { refreshes += 1; },
    () => current,
  );
  assert.equal(intervalMs, 30_000);

  current = 5_000;
  visibilityState = 'hidden';
  browserDocument.dispatchEvent(new Event('visibilitychange'));
  poll();
  assert.equal(refreshes, 0);

  current = 5_500;
  visibilityState = 'visible';
  browserDocument.dispatchEvent(new Event('visibilitychange'));
  browserWindow.dispatchEvent(new Event('focus'));
  assert.equal(refreshes, 1, 'focus and visibility together cause one read');

  current = 35_500;
  poll();
  assert.equal(refreshes, 2);
  unsubscribe();
  assert.equal(cleared, true);
  browserWindow.dispatchEvent(new Event('focus'));
  assert.equal(refreshes, 2);
});
