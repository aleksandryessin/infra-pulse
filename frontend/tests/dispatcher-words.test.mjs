import assert from 'node:assert/strict';
import { test } from 'node:test';

import { forbiddenWords } from '../scripts/check-dispatcher-words.mjs';

const words = (text) => forbiddenWords(text).map((item) => item.word.toLowerCase());

test('«аварийное освещение» is a normative term and passes (recommendation v5)', () => {
  for (const text of [
    'проверить, что аварийное освещение на пути бригады включается',
    'Аварийное освещение',
    'лампы аварийного освещения',
  ]) {
    assert.deepEqual(forbiddenWords(text), [], text);
  }
});

test('an emergency stays forbidden: авария, аварийная ситуация, аварийный выезд', () => {
  for (const text of ['авария на объекте', 'аварийная ситуация', 'аварийный выезд', 'Аварийное отключение']) {
    assert.deepEqual(words(text), ['авари'], text);
  }
});

test('approved phrases are removed before the check', () => {
  assert.deepEqual(forbiddenWords('это не подтверждённая авария'), []);
  assert.deepEqual(words('подтверждённая авария'), ['авари']);
});
