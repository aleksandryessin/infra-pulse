import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  calendarDays, formatRuDate, parseRuDate, ruDayLabel, shiftIsoDay, shiftMonth,
} from '../src/shared/lib/ru-date.ts';

test('journal date field shows дд.мм.гггг whatever the browser language', () => {
  assert.equal(formatRuDate('2026-06-01'), '01.06.2026');
  assert.equal(formatRuDate(''), '');
  assert.equal(formatRuDate(null), '');
  assert.equal(formatRuDate('06/01/2026'), '');
});

test('journal date field parses Russian input to an ISO day; incomplete input keeps the filter', () => {
  assert.equal(parseRuDate('01.06.2026'), '2026-06-01');
  assert.equal(parseRuDate('1.6.2026'), '2026-06-01');
  assert.equal(parseRuDate('01062026'), '2026-06-01');
  assert.equal(parseRuDate(' 29/02/2024 '), '2024-02-29');
  assert.equal(parseRuDate(''), null);
  assert.equal(parseRuDate('01.06'), undefined);
  assert.equal(parseRuDate('31.06.2026'), undefined);
  assert.equal(parseRuDate('29.02.2026'), undefined);
  assert.equal(parseRuDate('2026-06-01'), undefined);
});

test('journal calendar grid starts on Monday, shows 6 weeks and marks days of the month', () => {
  const days = calendarDays('2026-06');
  assert.equal(days.length, 42);
  // 01.06.2026 — понедельник: сетка начинается с него.
  assert.deepEqual(days[0], { iso: '2026-06-01', day: 1, inMonth: true });
  assert.deepEqual(days[29], { iso: '2026-06-30', day: 30, inMonth: true });
  assert.deepEqual(days[30], { iso: '2026-07-01', day: 1, inMonth: false });
  // 01.05.2026 — пятница: четыре дня апреля перед ним.
  const may = calendarDays('2026-05');
  assert.equal(may[0].iso, '2026-04-27');
  assert.equal(may[4].iso, '2026-05-01');
  assert.equal(may.filter((day) => day.inMonth).length, 31);
});

test('journal calendar moves by day and month across year boundaries', () => {
  assert.equal(shiftIsoDay('2026-06-30', 1), '2026-07-01');
  assert.equal(shiftIsoDay('2026-01-01', -7), '2025-12-25');
  assert.equal(shiftIsoDay('01.06.2026', 1), null);
  assert.equal(shiftMonth('2026-12', 1), '2027-01');
  assert.equal(shiftMonth('2026-01', -1), '2025-12');
  assert.equal(ruDayLabel('2026-06-01'), '1 июня 2026');
});
