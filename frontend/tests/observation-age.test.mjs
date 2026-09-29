import assert from 'node:assert/strict';
import { test } from 'node:test';

import { sourceImportTimeDifference } from '../src/shared/lib/observation-age.ts';

test('shows old source time without calling it network delivery delay', () => {
  assert.equal(
    sourceImportTimeDifference('2026-09-24T10:00:00+03:00', '2026-09-25T07:14:00+03:00'),
    'источник раньше импорта на 21 ч 14 мин',
  );
});

test('distinguishes a source clock ahead of local import', () => {
  assert.equal(
    sourceImportTimeDifference('2026-09-25T07:15:00+03:00', '2026-09-25T07:14:00+03:00'),
    'источник позже импорта на 1 мин',
  );
  assert.equal(
    sourceImportTimeDifference('2026-09-25T07:14:00+03:00', '2026-09-25T07:14:00+03:00'),
    'метки совпадают',
  );
  assert.equal(sourceImportTimeDifference('invalid', '2026-09-25T07:14:00+03:00'), 'не определяется');
});
