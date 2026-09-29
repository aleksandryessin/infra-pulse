import assert from 'node:assert/strict';
import { test } from 'node:test';

import { endSentence } from '../src/shared/lib/format.ts';

test('a sentence ending with an abbreviation gets no second full stop (P3 «щит..»)', () => {
  assert.equal(endSentence('Линии события: ФВ1 ПК254 щит., ФВ2 ПК254 щит.'), 'Линии события: ФВ1 ПК254 щит., ФВ2 ПК254 щит.');
  assert.equal(endSentence('Линии события: ГРО2 ПК39'), 'Линии события: ГРО2 ПК39.');
});
