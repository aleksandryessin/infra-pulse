import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  assignLanes, axisTicks, clusterByDistance, fitText, layoutMnemo, picketSpan,
} from '../src/widgets/object-mnemo/mnemo-layout.ts';
import {
  groupsFromParam, groupsToParam, sameSelection, schemeLineHref, schemeParams, selectionFromParam, selectionGroup,
  selectionToParam, withSelectionGroup,
} from '../src/widgets/object-mnemo/selection.ts';
import { attentionOrder, defaultObject } from '../src/widgets/object-picker/attention-order.ts';

const point = (value) => ({ form: 'point', picket_from: value });
const range = (from, to) => ({ form: 'range', picket_from: from, picket_to: to });
const unknown = { form: 'unknown' };
const line = (id, kind, picket, extra = {}) => ({ id, name: `Линия ${id}`, kind, picket, episodes: 5, link: null, alarms: [], ...extra });

const fixture = {
  width: 1290,
  landmarks: [
    { index: 0, kind: 'input', label: 'Ввод', picket: point(10) },
    { index: 1, kind: 'ats', label: 'АВР', picket: point(11) },
  ],
  lines: [
    line('gro', 'lighting', range(12, 18), { link: 'ГРО1-6' }),
    line('fv', 'ventilation', point(21), { link: 'В23', alarms: [{ id: 'row-1', text: '«Обесточен» 24.09, 08:10' }] }),
    line('fans', 'pumps', point(24)),
    line('ozk', 'ozk', point(30)),
    line('res', 'other', unknown),
  ],
};

const allItems = (layout) => layout.rows.flatMap((row) => [...row.items, ...row.columnItems]);
const xOf = (layout, pk) => 18 + (pk - layout.domain.min) * layout.track.pxPerPk;

test('route is solid from the first to the last known picket, then a dashed tail', () => {
  const layout = layoutMnemo(fixture);
  assert.deepEqual(layout.known, { min: 10, max: 30 });
  assert.ok(Math.abs(layout.tunnel.x1 - xOf(layout, 10)) < 0.01);
  assert.ok(Math.abs(layout.tunnel.x2 - xOf(layout, 30)) < 0.01);
  assert.ok(layout.tunnel.tail.x1 > layout.tunnel.x2, 'tail starts after the last known picket');
  assert.ok(layout.tunnel.tail.x2 <= layout.track.width, 'tail stays inside the track');
  assert.equal(layout.track.scrolls, false, 'a short object fits without scrolling');
});

test('every line sits on its own picket; unknown picket goes to the «Без пикета» column', () => {
  const layout = layoutMnemo(fixture);
  const byId = Object.fromEntries(allItems(layout).map((item) => [item.id, item]));
  assert.equal(layout.rows.map((row) => row.kind).join(','), 'lighting,ventilation,pumps,ozk,other');
  assert.ok(byId.gro.bar && byId.gro.bar.x2 > byId.gro.bar.x1, 'range is a bar');
  assert.ok(byId.fv.cx < byId.fans.cx && byId.fans.cx < byId.ozk.cx, 'order follows pickets');
  assert.equal(byId.res.unknown, true);
  assert.equal(layout.column.count, 1);
  assert.ok(layout.rows[4].empty);
  assert.equal(layout.rows[0].empty, null);
});

test('a link is drawn only from named_link; no caption under the icon and no links from inputs', () => {
  const layout = layoutMnemo(fixture);
  const byId = Object.fromEntries(allItems(layout).map((item) => [item.id, item]));
  assert.equal(byId.gro.link.label, 'ГРО1-6');
  assert.equal(byId.gro.link.x1, byId.gro.bar.x2, 'link starts at the end of the range');
  assert.equal(byId.fans.link, null);
  assert.equal(byId.fv.label.text, 'Линия fv');
  assert.equal(layout.landmarks.length, 2);
  assert.ok(layout.landmarks.every((landmark) => !('link' in landmark)));
});

test('landmarks do not overlap and stay near their picket', () => {
  const layout = layoutMnemo(fixture);
  const [input, ats] = layout.landmarks;
  assert.ok(ats.cx - input.cx >= 32, 'nodes are spread apart');
  assert.ok(Math.abs(input.cx - input.x) < 1, 'first node on its picket');
  assert.ok(input.bottom < layout.tunnel.y, 'nodes stand on the route');
});

test('alarm of a line is placed above its icon and raises the lane', () => {
  const layout = layoutMnemo(fixture);
  const fv = layout.rows[1].items[0];
  assert.equal(fv.alarm.id, 'row-1');
  assert.ok(fv.alarm.y + 16 < fv.cy - 12, 'square above the icon');
  assert.ok(fv.alarm.y >= layout.rows[1].y, 'square inside its row');
  assert.ok(layout.rows[1].h > layout.rows[2].h);
});

test('no known picket: no route, every line in «Без пикета», more than two become one sign', () => {
  const layout = layoutMnemo({ width: 1000, landmarks: [], lines: [line('a', 'pumps', unknown), line('b', 'pumps', unknown), line('c', 'pumps', unknown)] });
  assert.equal(layout.tunnel, null);
  assert.equal(layout.domain, null);
  assert.equal(layout.axis.ticks.length, 0);
  assert.equal(layout.column.count, 3);
  const [sign] = layout.rows[2].columnItems;
  assert.equal(layout.rows[2].columnItems.length, 1);
  assert.equal(sign.type, 'cluster');
  assert.equal(sign.badge.text, '15');
  assert.equal(sign.label.text, '3 линии');
});

// «Реалистичный» синтетический объект: 40 линий, ПК 0–601, из них 10 без ПК.
function realistic() {
  const kinds = ['lighting', 'lighting', 'lighting', 'ventilation', 'ventilation', 'pumps', 'ozk', 'other'];
  const lines = [];
  for (let index = 0; index < 30; index += 1) {
    const kind = kinds[index % kinds.length];
    const from = Math.round((index * 601) / 29);
    const picket = kind === 'lighting' && index % 3 === 0 ? range(from, Math.min(601, from + 40)) : point(from);
    lines.push({
      id: `l${index}`, name: `Синт. линия ${kind} ${index} ПК${from}`, kind, picket, episodes: (index % 7) + 1,
      link: index % 5 === 0 ? `В${index}` : null,
      alarms: index === 12 ? [{ id: 'row-12', text: '«Обесточен» 24.09, 08:10' }] : [],
    });
  }
  // Плотное место: 5 линий освещения в пределах 3 ПК.
  for (let index = 0; index < 5; index += 1) lines[index * 3].picket = point(300 + index * 0.5);
  for (let index = 0; index < 10; index += 1) {
    lines.push({ id: `u${index}`, name: `Синт. линия без ПК ${index}`, kind: kinds[index % kinds.length], picket: unknown, episodes: 2, link: null, alarms: [] });
  }
  return lines;
}

test('realistic object (40 lines, ПК 0–601, 10 without ПК): readable density, ≤ 2 lanes, nothing lost', () => {
  const lines = realistic();
  const layout = layoutMnemo({ width: 1290, landmarks: [{ index: 0, kind: 'input', label: 'Ввод', picket: point(0) }], lines });
  assert.ok(layout.track.pxPerPk >= 6, 'at least 6 px per picket');
  assert.equal(layout.track.scrolls, true, 'long object scrolls instead of squeezing');
  assert.deepEqual(layout.known, { min: 0, max: 601 });
  for (const row of layout.rows) assert.ok(row.lanes <= 2, `${row.kind}: lanes ${row.lanes}`);
  const signs = allItems(layout);
  const covered = signs.flatMap((item) => item.ids).sort();
  assert.deepEqual(covered, lines.map((item) => item.id).sort(), 'every line is in exactly one sign');
  const episodes = signs.reduce((sum, item) => sum + Number(item.badge.text), 0);
  assert.equal(episodes, lines.reduce((sum, item) => sum + item.episodes, 0), 'episodes are summed, not lost');
  assert.equal(layout.column.count, 10);
  // Линии ближе 24 px — один знак «N линий · ПК a–b».
  const cluster = signs.find((item) => item.type === 'cluster' && !item.unknown);
  assert.ok(cluster, 'close lines are merged');
  assert.match(cluster.label.text, /^\d+ лини[йи] · ПК /);
  // На трассе знаки одной дорожки не перекрываются.
  for (const row of layout.rows) {
    for (const lane of [0, 1]) {
      const inLane = row.items.filter((item) => item.lane === lane).sort((a, b) => a.extent.left - b.extent.left);
      for (let index = 1; index < inLane.length; index += 1) assert.ok(inLane[index].extent.left >= inLane[index - 1].extent.right);
    }
  }
  // Тревога не теряется при слиянии.
  assert.ok(signs.some((item) => item.alarm && item.alarm.id === 'row-12'));
});

test('fit mode puts the whole route into the viewport; dense groups hide labels, keep icon and number', () => {
  const layout = layoutMnemo({ width: 1290, landmarks: [], lines: realistic(), fit: true });
  assert.equal(layout.track.scrolls, false);
  assert.ok(layout.track.pxPerPk < 6);
  for (const row of layout.rows) assert.ok(row.lanes <= 2);
  const dense = layout.rows.filter((row) => row.dense);
  assert.ok(dense.length > 0, 'some group is dense when fitted');
  assert.ok(dense.flatMap((row) => row.items).every((item) => item.labelHidden && item.badge.text));
});

// Плотный реальный объект (F6): 99 линий, 10 линий освещения на одном пикете, 32 линии с тревожными записями.
function dense99() {
  const kinds = ['lighting', 'lighting', 'lighting', 'ventilation', 'pumps', 'ozk', 'other'];
  const lines = [];
  for (let index = 0; index < 99; index += 1) {
    const kind = index < 10 ? 'lighting' : kinds[index % kinds.length];
    const picket = index < 10 ? point(120) : index % 9 === 0 ? unknown : point(Math.round((index * 1195) / 99));
    const alarms = index % 3 === 0 && index < 96
      ? [{ id: `r${index}`, text: '«Отключено устройство» 30.06, 16:30', active: index % 2 === 0 }]
      : [];
    lines.push({
      id: `d${index}`, name: `Синт. линия ${index}`, kind, picket, episodes: index % 4 === 0 ? 0 : index % 5,
      link: null, alarms, alarmRecords: alarms.length ? 3 : undefined,
    });
  }
  return lines;
}

test('dense real object: zero badges hidden, one «!N» square per sign, group summaries count records', () => {
  const lines = dense99();
  const layout = layoutMnemo({ width: 1290, landmarks: [], lines });
  const signs = allItems(layout);
  assert.deepEqual(signs.flatMap((item) => item.ids).sort(), lines.map((item) => item.id).sort(), 'every line kept');
  for (const row of layout.rows) assert.ok(row.lanes <= 2, `${row.kind}: lanes ${row.lanes}`);
  // Правило 4: нулевой бейдж не рисуется.
  const zero = signs.filter((item) => item.ids.every((id) => lines.find((line) => line.id === id).episodes === 0));
  assert.ok(zero.length > 0 && zero.every((item) => item.badge.text === '' && item.badge.w === 0));
  // Правило 3: у знака только квадрат, число сообщений — в «!N» и в начале подсказки.
  const alarmed = signs.filter((item) => item.alarm);
  assert.ok(alarmed.length > 0);
  for (const item of alarmed) {
    const records = item.ids.reduce((sum, id) => sum + (lines.find((line) => line.id === id).alarms.length ? 3 : 0), 0);
    assert.equal(item.alarm.count, records);
    assert.ok(item.alarm.text.startsWith(`сообщений: ${records}`));
    assert.ok(item.extent.right >= item.alarm.x + item.alarm.w, 'the square is inside the sign extent');
  }
  // Правило 2: сводка на группу — все 32 линии с записями и 96 записей учтены.
  const summaries = layout.rows.map((row) => row.alarmSummary).filter(Boolean);
  assert.equal(summaries.reduce((sum, item) => sum + item.lines, 0), 32);
  assert.equal(summaries.reduce((sum, item) => sum + item.records, 0), 96);
  assert.equal(summaries.reduce((sum, item) => sum + item.active, 0), 16);
  // Под сводкой хватает места: полоса не ниже подписи группы и двух строк сводки.
  for (const row of layout.rows) if (row.alarmSummary) assert.ok(row.y + row.h >= row.labelY + 30);
});

test('group filter: hidden groups collapse to a 24 px row, keep their alarms, the scale does not move', () => {
  const lines = realistic();
  const all = layoutMnemo({ width: 1290, landmarks: [], lines });
  const kinds = ['lighting', 'ventilation', 'pumps', 'ozk', 'other'];
  const hidden = kinds.filter((kind) => kind !== 'pumps');
  const one = layoutMnemo({ width: 1290, landmarks: [], lines, hidden });
  assert.equal(one.track.pxPerPk, all.track.pxPerPk, 'the route scale does not depend on the filter');
  for (const row of one.rows) {
    if (row.kind === 'pumps') {
      assert.equal(row.hidden, null);
      assert.ok(row.items.length > 0);
      continue;
    }
    assert.equal(row.h, 24, `${row.kind}: collapsed`);
    assert.equal(row.items.length + row.columnItems.length, 0, `${row.kind}: no signs`);
    assert.equal(row.hidden.lines, lines.filter((item) => item.kind === row.kind).length);
  }
  // Тревога row-12 (линия l12 — вентиляция) не исчезает: она в строке скрытой группы.
  const ventilation = one.rows.find((row) => row.kind === 'ventilation');
  assert.equal(ventilation.hidden.alarms, 1);
  assert.equal(ventilation.hidden.alarmId, 'row-12');
  assert.equal(one.rows.find((row) => row.kind === 'lighting').hidden.alarms, 0);
  assert.ok(one.height < all.height);
  assert.equal(one.column.count, lines.filter((item) => item.kind === 'pumps' && item.picket.form === 'unknown').length);
});

test('group filter in the URL; a selected line or alarm shows its group', () => {
  assert.equal(groupsFromParam(null), null, 'no parameter — all groups');
  assert.deepEqual(groupsFromParam('pumps,lighting,bogus'), ['lighting', 'pumps']);
  assert.deepEqual(groupsFromParam(''), []);
  assert.equal(groupsFromParam('lighting,ventilation,pumps,ozk,other'), null);
  assert.equal(groupsToParam(null), null);
  assert.equal(groupsToParam(['pumps', 'lighting']), 'lighting,pumps');
  assert.deepEqual(schemeParams('o-1', null, ['pumps']), { object: 'o-1', groups: 'pumps' });
  const feeders = [
    { channel_id: 'f1', feeder_kind: 'ventilation', current_alarms: [{ row_uid: 'r1' }] },
    { channel_id: 'f2', feeder_kind: 'unexpected', current_alarms: [] },
  ];
  assert.equal(selectionGroup({ kind: 'alarm', rowUid: 'r1' }, feeders), 'ventilation');
  assert.equal(selectionGroup({ kind: 'feeder', channelId: 'f2' }, feeders), 'other');
  assert.equal(selectionGroup({ kind: 'range', group: 'pumps', from: 1, to: 2 }, feeders), 'pumps');
  assert.equal(selectionGroup({ kind: 'forecast', cardId: 'c' }, feeders), null);
  assert.deepEqual(withSelectionGroup(['pumps'], 'ventilation'), ['ventilation', 'pumps']);
  assert.equal(withSelectionGroup(['lighting', 'ventilation', 'pumps', 'ozk'], 'other'), null);
  assert.equal(withSelectionGroup(null, 'ozk'), null);
});

test('axis ticks adapt to the scale and labels do not crowd', () => {
  const dense = axisTicks(8, 32, 40);
  assert.equal(dense[0].pk, 8);
  assert.ok(dense.filter((tick) => tick.major).every((tick) => tick.pk % 5 === 0));
  const wide = axisTicks(0, 601, 1.5);
  const majors = wide.filter((tick) => tick.major).map((tick) => tick.pk);
  assert.ok(majors.length <= 10);
  assert.ok(majors[1] - majors[0] >= 50);
});

test('helpers: picket span, lanes, clustering, text fitting', () => {
  assert.deepEqual(picketSpan(range(12, 18)), { from: 12, to: 18 });
  assert.deepEqual(picketSpan(point(21)), { from: 21, to: 21 });
  assert.equal(picketSpan(unknown), null);
  assert.deepEqual(assignLanes([{ left: 0, right: 50 }, { left: 40, right: 90 }, { left: 70, right: 120 }]).lanes, [0, 1, 0]);
  assert.equal(assignLanes([{ left: 0, right: 50 }, { left: 10, right: 60 }, { left: 20, right: 70 }], 12, 2).overflow, 2);
  assert.deepEqual(clusterByDistance([{ x: 0, value: 'a' }, { x: 20, value: 'b' }, { x: 50, value: 'c' }]), [['a', 'b'], ['c']]);
  assert.equal(fitText('коротко', 200), 'коротко');
  assert.ok(fitText('очень длинное название линии электропитания', 80).endsWith('…'));
});

test('selection round-trips through the URL', () => {
  for (const selection of [
    { kind: 'range', group: 'lighting', from: 12, to: 18.5 },
    { kind: 'forecast', cardId: 'c-1' },
    { kind: 'released', cardId: 'c-2' },
    { kind: 'feeder', channelId: 'ch:1' },
    { kind: 'alarm', rowUid: 'r-1' },
    { kind: 'landmark', index: 1 },
    { kind: 'alarms', group: 'ventilation' },
    { kind: 'unknown' },
  ]) {
    assert.ok(sameSelection(selectionFromParam(selectionToParam(selection)), selection));
  }
  assert.equal(selectionFromParam(null), null);
  assert.equal(selectionFromParam('landmark:x'), null);
  assert.deepEqual(schemeParams('o-1', null), { object: 'o-1' });
  assert.deepEqual(schemeParams('o-1', { kind: 'unknown' }), { object: 'o-1', sel: 'unknown' });
});

test('journal links the event line to the object scheme, not to the internal source-messages page', () => {
  const href = schemeLineHref('объект 1', 'ch:7/ПК 12');
  assert.ok(href.startsWith('/scheme?'));
  assert.ok(!href.includes('/queue'));
  const params = new URLSearchParams(href.slice('/scheme?'.length));
  assert.equal(params.get('object'), 'объект 1');
  assert.deepEqual(selectionFromParam(params.get('sel')), { kind: 'feeder', channelId: 'ch:7/ПК 12' });
});

test('attention: alarms first, then forecasts by nearest term, then released; default is the first', () => {
  const summaries = [
    { object_id: 'o11', object_name: 'Объект 11', current_alarms: 0, open_cards: 1, released_7d: 0 },
    { object_id: 'o12', object_name: 'Объект 12', current_alarms: 0, open_cards: 1, released_7d: 0 },
    { object_id: 'o19', object_name: 'Объект 19', current_alarms: 1, open_cards: 1, released_7d: 0 },
    { object_id: 'o21', object_name: 'Объект 21', current_alarms: 0, open_cards: 0, released_7d: 1 },
    { object_id: 'o30', object_name: 'Объект 30', current_alarms: 0, open_cards: 0, released_7d: 0 },
  ];
  const open = [
    { id: 'c11', object_id: 'o11', window_end: '2026-09-25T00:00:00+03:00' },
    { id: 'c12', object_id: 'o12', window_end: '2026-09-27T00:00:00+03:00' },
    { id: 'c19', object_id: 'o19', window_end: '2026-10-08T00:00:00+03:00' },
  ];
  const released = [{ id: 'c21', object_id: 'o21', released_at: '2026-09-23T11:40:00+03:00' }];
  const entries = attentionOrder(summaries, open, released);
  assert.deepEqual(entries.map((entry) => entry.objectId), ['o19', 'o11', 'o12', 'o21', 'o30']);
  assert.deepEqual(entries.map((entry) => entry.group), ['alarm', 'forecast', 'forecast', 'released', 'none']);
  assert.equal(defaultObject(entries), 'o19');
  // Без тревог — объект с ближайшим сроком.
  const calm = attentionOrder(summaries.map((item) => ({ ...item, current_alarms: 0 })), open, released);
  assert.equal(defaultObject(calm), 'o11');
  assert.equal(defaultObject([]), null);
});
