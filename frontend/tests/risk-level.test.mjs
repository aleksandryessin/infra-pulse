import assert from 'node:assert/strict';
import { test } from 'node:test';

import { RISK_LEVEL_BARS, countRiskLevels, riskLevelOf, riskLevelText } from '../src/shared/lib/risk-level.ts';

test('risk level: unknown and foreign values are «не определён», never a default level', () => {
  assert.equal(riskLevelOf('high'), 'high');
  assert.equal(riskLevelOf('medium'), 'medium');
  assert.equal(riskLevelOf(null), 'unknown');
  assert.equal(riskLevelOf(undefined), 'unknown');
  assert.equal(riskLevelOf('critical'), 'unknown');
  assert.equal(riskLevelText('high'), 'высокий риск');
  assert.equal(riskLevelText('medium', 'full'), 'Уровень риска: средний');
  assert.equal(riskLevelText(null, 'full'), 'Уровень риска: не определён');
});

test('risk level: the mark differs by shape (filled bars), not only by colour', () => {
  const bars = ['high', 'medium', 'low', 'unknown'].map((level) => RISK_LEVEL_BARS[level]);
  assert.deepEqual(bars, [3, 2, 1, 0]);
  assert.equal(new Set(bars).size, bars.length);
});

test('risk level summary counts cards from high to unknown and skips absent levels', () => {
  assert.deepEqual(countRiskLevels(['medium', 'high', 'medium', null, 'high', 'medium']), [
    { level: 'high', count: 2 },
    { level: 'medium', count: 3 },
    { level: 'unknown', count: 1 },
  ]);
  assert.deepEqual(countRiskLevels([]), []);
});
