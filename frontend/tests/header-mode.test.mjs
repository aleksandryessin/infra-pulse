import assert from 'node:assert/strict';
import { test } from 'node:test';

import { headerModeLabel } from '../src/shared/config/labels.ts';

// Замечание владельца 29.09: «Загруженные файлы» в шапке диспетчеру и аналитику непонятно.
test('working data modes are not labelled for dispatcher and analyst', () => {
  assert.equal(headerModeLabel('received', false), null);
  assert.equal(headerModeLabel('live', false), null);
  assert.equal(headerModeLabel(null, false), null);
});

test('demo and replay modes stay visible to every role: the data are not real or not real-time', () => {
  assert.equal(headerModeLabel('fixture', false), 'Демонстрация: синтетические данные');
  assert.equal(headerModeLabel('replay', false), 'Исторический повтор, не в реальном времени');
});

test('administrator sees the data source in plain words', () => {
  assert.equal(headerModeLabel('received', true), 'Источник: загрузки файлов и API');
  assert.equal(headerModeLabel('received', true, true), 'Источник: загрузки файлов и API (по настройке сборки)');
  assert.equal(headerModeLabel(undefined, true), 'Режим данных неизвестен');
});
