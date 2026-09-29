import assert from 'node:assert/strict';
import { test } from 'node:test';

import { linkedReceivedSnapshot } from '../src/shared/lib/received-snapshot.ts';

test('linked received scope requires both an aware moment and a safe committed position', () => {
  assert.deepEqual(linkedReceivedSnapshot('2026-09-25T02:40:00+03:00', '0'), {
    asOf: '2026-09-25T02:40:00+03:00', watermark: 0,
  });
  assert.equal(linkedReceivedSnapshot('2026-09-25T02:40:00', '3'), null);
  assert.equal(linkedReceivedSnapshot('2026-09-25T02:40:00Z', null), null);
  assert.equal(linkedReceivedSnapshot('2026-09-25T02:40:00Z', '-1'), null);
  assert.equal(linkedReceivedSnapshot('2026-09-25T02:40:00Z', '9007199254740992'), null);
});
