import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  frequencyTexts, isDecisionOverdue, orderForecastRows, picketText, repeatSummary,
} from '../src/shared/lib/forecast.ts';
import { criticalRule } from '../src/shared/config/notifications.ts';

const row = (place, decision, isNew = false, overdue = false) => ({ item: place, place, decision, isNew, overdue });

test('orders forecast: overdue without decision, then without decision, then decided; new first inside', () => {
  const ordered = orderForecastRows([
    row(0, 'decided', true),
    row(1, 'none'),
    row(2, 'none', true),
    row(3, 'none', false, true),
    row(4, 'unknown'),
  ]).map((item) => item.place);
  assert.deepEqual(ordered, [3, 2, 1, 4, 0]);
});

test('overdue is measured on data time, not on the browser clock', () => {
  assert.equal(isDecisionOverdue('2026-09-11T00:04:00+03:00', '2026-09-24T10:00:00+03:00', 24), true);
  assert.equal(isDecisionOverdue('2026-09-24T00:04:00+03:00', '2026-09-24T10:00:00+03:00', 24), false);
});

test('frequency text uses only API numbers and the approved template', () => {
  const texts = frequencyTexts({
    score: { frequency: { positive_cards: 255, cards: 300, wilson_low: 0.8051, wilson_high: 0.886 } },
  });
  assert.equal(texts.card, 'Оценка вероятности для карточек этого уровня: ≈ 85% (сбылось 255 из 300)');
  assert.equal(texts.list, '≈ 85% · сбылось 255 из 300');
  assert.equal(texts.big, '≈ 85%');
  assert.equal(texts.detail, 'для карточек этого уровня (сбылось 255 из 300)');
  assert.equal(texts.interval, '80,5–88,6%');
  assert.equal(frequencyTexts({ score: { frequency: { positive_cards: 272, cards: 324, wilson_low: 0.79, wilson_high: 0.87 } } }).big, '≈ 85%');
  assert.equal(frequencyTexts({ score: { frequency: { positive_cards: 181, cards: 335, wilson_low: 0.49, wilson_high: 0.59 } } }).big, '≈ 55%');
  assert.equal(frequencyTexts({ score: null }), null);
});

test('picket text never invents a position', () => {
  assert.equal(picketText({ form: 'range', from: 12, to: 18 }), 'ПК 12–18');
  assert.equal(picketText({ form: 'point', from: 21 }), 'ПК 21');
  assert.equal(picketText({ form: 'unknown' }), 'пикет не указан');
});

test('repeat summary counts cards of the object within 30 days before issue', () => {
  const card = (id, issued) => ({ id, object_id: 'o', target_spec_id: 't', issued_at: issued });
  const entry = (id, issued, decision) => ({ card: card(id, issued), decision, outcome: { status: 'not_realized' } });
  const current = card('c3', '2026-09-24T00:00:00+03:00');
  const result = repeatSummary(current, [
    entry('c1', '2026-08-01T00:00:00+03:00'),
    entry('c2', '2026-09-10T00:00:00+03:00', { decision_code: 'R1' }),
    entry('c3', '2026-09-24T00:00:00+03:00'),
  ]);
  assert.equal(result.ordinal, 2);
  assert.equal(result.previous.card.id, 'c2');
});

test('critical by text (policy v1, when the data has no alarm flag): gas counts, fault and power-off do not', () => {
  const rule = (value_raw, sensor_type) => criticalRule({ value_raw, sensor_type })?.label ?? null;
  assert.equal(rule('Обнаружен газ', 'Газовый датчик'), 'газ');
  assert.equal(rule('Неисправен', 'Насос'), null);
  assert.equal(rule('Обесточен', 'Состояние фазы'), null);
  assert.equal(rule('Не замкнут', 'КД Дверь'), null);
  assert.equal(rule('Не замкнут', 'Тепловой датчик'), 'срабатывание теплового извещателя');
  assert.equal(rule('Затоплен', 'Датчик затопления'), 'затопление');
});

test('pump «Затоплен» is selected by the journal type «Состояние насоса», as on the server (flood_pump)', () => {
  const rule = (value_raw, sensor_type) => criticalRule({ value_raw, sensor_type })?.label ?? null;
  // Прежнее правило искало тип «Насос», которого в журнале нет, и насос не отбирался.
  assert.equal(rule('Затоплен', 'Состояние насоса'), 'затопление');
  assert.equal(rule(' затоплен ', 'состояние насоса'), 'затопление', 'case and edge spaces are ignored');
  assert.equal(rule('Затоплен', 'Насос'), null);
  assert.equal(rule('Неисправен', 'Состояние насоса'), null);
  assert.equal(rule('Обесточен', 'Состояние насоса'), null);
});

test('card picket span and lines fact use only listed data', async () => {
  const { channelsPicketSpan, linesFact } = await import('../src/shared/lib/forecast.ts');
  assert.equal(channelsPicketSpan([
    { picket_form: 'range', picket_from: 12, picket_to: 18 },
    { picket_form: 'point', picket_from: 30 },
    { picket_form: 'unknown' },
  ]), 'ПК 12–30');
  assert.equal(channelsPicketSpan([{ picket_form: 'unknown' }]), null);
  const episodes = (n) => ({ reason_facts: [{ kind: 'events_365d', value_number: n }] });
  assert.deepEqual(linesFact({ channels: [episodes(3), episodes(0)], channels_total: 2 }),
    { label: 'Линий под наблюдением карточки', value: '2', note: null, complete: true, listed: 2 });
  // F-05: 37 под наблюдением из 41 на схеме — разница подписана.
  const tracked = linesFact({ channels: [episodes(3)], channels_total: 37 }, 41);
  assert.equal(tracked.value, '37 из 41');
  assert.match(tracked.note, /^остальные 4 при выдаче не отслеживались/);
  assert.equal(linesFact({ channels: [], channels_total: null }), null);
  const { lineNamer } = await import('../src/shared/lib/forecast.ts');
  const name = lineNamer([{ channel_id: 'c1', channel_name: 'ГРО2 ПК39' }], { c2: 'ФВ1 ПК40' });
  assert.deepEqual(['c1', 'c2', '115762'].map(name), ['ГРО2 ПК39', 'ФВ1 ПК40', 'линия без названия в справочнике']);
});

test('bell rows follow the server order and row_kind; new cards stay out of the bell', async () => {
  const { bellRows } = await import('../src/shared/lib/bell.ts');
  const member = (index) => ({ row_uid: `m${index}`, channel_id: `c${index}`, event_at: '2026-09-24T07:00:00+03:00', value_raw: 'Не замкнут' });
  const rows = bellRows([
    { kind: 'critical_alarm', ref_id: 'gas', title: 'Газ', at: '2026-09-24T06:00:00+03:00', priority: 0, row_kind: 'single' },
    { kind: 'new_card', ref_id: 'card', title: 'Новая карточка', at: '2026-09-24T08:00:00+03:00', priority: 9 },
    { kind: 'critical_alarm', ref_id: 'test', title: 'Тепловой', at: '2026-09-24T07:10:00+03:00', row_kind: 'test_series', collapsed_count: 60, channels_count: 6, first_at: '2026-09-24T07:00:00+03:00', members: Array.from({ length: 50 }, (_, index) => member(index)) },
    { kind: 'critical_alarm', ref_id: 'cal', title: 'Газ', at: '2026-09-24T05:00:00+03:00', row_kind: 'calibration_series', collapsed_count: 3, channels_count: 3, members: [member(90), member(91), member(92)] },
    { kind: 'critical_alarm', ref_id: 'chat', title: 'Датчик дыма: «Обнаружен дым» ×12', at: '2026-09-24T04:55:00+03:00', first_at: '2026-09-24T04:00:10+03:00', row_kind: 'chatter', collapsed_count: 12, channels_count: 1, members: [member(99)] },
  ]);
  assert.deepEqual(rows.map((row) => row.item.ref_id), ['gas', 'test', 'cal', 'chat'], 'server order kept, card excluded');
  assert.deepEqual(rows.map((row) => row.summary), [
    null,
    'похоже на ППР или ТО: 6 извещателей — сверить с графиком',
    'похоже на ППР или ТО: 3 газоанализатора — сверить с графиком',
    'Датчик дыма: «Обнаружен дым» ×12',
  ], 'repeats of one detector within an hour: the server title «… ×N» as is');
  const { repeatSpan } = await import('../src/shared/lib/bell.ts');
  assert.equal(repeatSpan(rows[3].item), '24.09, с 04:00 по 04:55', 'span of the repeats in MSK under the row');
  assert.equal(rows[1].count, 60);
  assert.equal(rows[1].members.length, 50);
  assert.equal(rows[1].refIds.length, 51);
});

test('interface quotes are «…» even when the API text uses „…“', async () => {
  const { ruQuotes } = await import('../src/shared/lib/forecast.ts');
  assert.equal(ruQuotes('потеря связи („Неисправен“), через ~10 мин — „Обесточен“.'), 'потеря связи («Неисправен»), через ~10 мин — «Обесточен».');
  assert.equal(ruQuotes('уже «так»'), 'уже «так»');
});

test('bell rows carry the customer group by rule_id; the server title is left as is', async () => {
  const { bellRows } = await import('../src/shared/lib/bell.ts');
  const row = (ref_id, rule_id, row_kind = 'single') => ({
    kind: 'critical_alarm', ref_id, title: `Строка ${ref_id}`, at: '2026-09-24T06:00:00+03:00', rule_id, row_kind,
  });
  const rows = bellRows([
    row('smoke', 'fire_smoke'), row('heat', 'fire_heat'), row('manual', 'fire_manual'),
    row('fire-series', 'fire_test_series', 'test_series'),
    row('gas', 'gas_detected'), row('gas-series', 'gas_calibration_series', 'calibration_series'),
    row('pump', 'flood_pump'), row('flood', 'flood_sensor', 'chatter'),
    row('unknown', 'intrusion_armed'), row('none', null),
  ]);
  assert.deepEqual(rows.map((item) => item.group), [
    'Пожар', 'Пожар', 'Пожар', 'Пожар', 'Газ', 'Газ', 'Затопление', 'Затопление', null, null,
  ]);
  assert.ok(rows.every((item) => item.item.title.startsWith('Строка ')), 'title from the server is not rewritten');
});

test('bell row leads to the scheme only when the object has one (rehearsal 29.09: HTTP 404)', async () => {
  const { schemeObject } = await import('../src/shared/lib/bell.ts');
  const row = (object_id) => ({ kind: 'critical_alarm', ref_id: 'r', title: 'Датчик дыма: «Обнаружен дым»', at: '2026-09-24T06:00:00+03:00', object_id });
  const schemes = new Set(['synthetic-object-with-lines']);
  assert.equal(schemeObject(row('synthetic-object-with-lines'), schemes), 'synthetic-object-with-lines');
  assert.equal(schemeObject(row('synthetic-fire-object'), schemes), null, 'no lines of power supply — no scheme link');
  assert.equal(schemeObject(row(null), schemes), null, 'unbound record');
  assert.equal(schemeObject(row('synthetic-fire-object'), null), 'synthetic-fire-object', 'list not loaded yet: the link stays');
});
