import assert from 'node:assert/strict';
import { test } from 'node:test';

import { noDataText } from '../src/shared/lib/no-data.ts';

test('an empty forecast names the days without data (P1-2)', () => {
  assert.equal(
    noDataText('2026-07-01', '2026-07-31'),
    'Нет данных за 01.07–31.07: выдачи за эти сутки без карточек. Прогноз появится после поступления данных',
  );
  assert.equal(
    noDataText('2026-09-28', '2026-09-28'),
    'Нет данных за 28.09: выдачи за эти сутки без карточек. Прогноз появится после поступления данных',
  );
});

test('without days without data the empty list keeps its usual text', () => {
  assert.equal(noDataText(null, null), null);
  assert.equal(noDataText(undefined, undefined), null);
  assert.equal(noDataText('2026-07-01', null), null);
});
