import assert from 'node:assert/strict';
import { test } from 'node:test';

import { storedAttentionReasons } from '../src/shared/lib/attention-reasons.ts';

test('keeps source alarm and exact-text reasons distinct from severity', () => {
  assert.match(storedAttentionReasons(['source_alarm_true', 'received_order']), /тревожное.*true/);
  assert.match(storedAttentionReasons(['exact_text_candidate', 'received_order']), /типа датчика.*исходного текста/);
  assert.match(storedAttentionReasons(['received_order']), /тяжесть не оценивалась/);
});

test('labels missing historical reasons without inventing an explanation', () => {
  assert.equal(storedAttentionReasons(null), 'для ранней заметки не сохранено');
  assert.equal(storedAttentionReasons(['new_policy_reason']), 'неизвестный код: new_policy_reason');
});
