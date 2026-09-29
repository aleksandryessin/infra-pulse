import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  fromMessages, fromServer, nowCountsText, orderNowObjects, schemeQuery,
} from '../src/shared/lib/source-alarms.ts';

const at = (hours, minutes = 0) => new Date(Date.UTC(2026, 5, 30, hours, minutes)).toISOString();

function message(uid, objectId, event, extra = {}) {
  return {
    row_uid: uid, object_id: objectId, object_name: objectId ? `объект ${objectId}` : null,
    channel_id: `${objectId}-${uid.split('-')[0]}`, value_raw: 'Отключено устройство', alarm: true,
    event_at: event, feeder_kind: null, ...extra,
  };
}

function object(objectId, last, records) {
  const lastMessage = message(`${objectId}-last`, objectId, last);
  return {
    object_id: objectId, object_name: `объект ${objectId}`, record_count: records, channel_count: 1,
    scheme_record_count: 0, first_event_at: last, last_event_at: last, last_message: lastMessage,
  };
}

// Обкатка 29.09: у «Дзета ДУ» открыта карточка прогноза, последнее сообщение 17:45; позже — дым.
test('objects with an open forecast card come first, then by the latest message', () => {
  const objects = [
    object('5962', at(20, 30), 106), // «Гамма ПС»: дым, последнее 23:30 МСК
    object('5578', at(14, 45), 126), // «Дзета ДУ»: открыта карточка
    object('5675', at(20, 20), 16),
    object('5003', at(2, 33), 5), // «Омега»: открыта карточка, самое старое сообщение
    object('5011', at(20, 20), 40), // то же время, что у 5675: больше сообщений — выше
  ];
  const ordered = orderNowObjects(objects, new Set(['5578', '5003']));
  assert.deepEqual(ordered.map((item) => item.object_id), ['5578', '5003', '5962', '5011', '5675']);
  assert.deepEqual(orderNowObjects(objects, new Set()).map((item) => item.object_id), ['5962', '5011', '5675', '5578', '5003']);
});

test('counts are the server counts of the whole window', () => {
  const window = {
    record_count: 1263, object_count: 18, without_object_count: 0, objects: [], latest: [],
  };
  assert.equal(nowCountsText(fromServer(window)), 'сообщений: 1263 · объектов: 18');
  assert.equal(nowCountsText({ records: 7, objectCount: 2, withoutObject: 1 }), 'сообщений: 7 · объектов: 2 · без объекта: 1');
  assert.equal(nowCountsText({ records: 0, objectCount: 0, withoutObject: 0 }), 'сообщений: 0');
});

test('a row opens the scheme only for an object with a scheme, the message only on a power line', () => {
  const schemes = new Set(['5578']);
  const onLine = message('a-1', '5578', at(14, 45), { feeder_kind: 'pumps' });
  const pump = message('b-1', '5578', at(14, 40));
  const smoke = message('c-1', '5962', at(20, 30));
  assert.equal(String(schemeQuery(onLine, schemes)), 'object=5578&sel=alarm%3Aa-1');
  // A pump of the reference is not drawn on the scheme: open the scheme, select nothing.
  assert.equal(String(schemeQuery(pump, schemes)), 'object=5578');
  // No scheme (fire alarm object): no link instead of «HTTP 404».
  assert.equal(schemeQuery(smoke, schemes), null);
  assert.equal(schemeQuery(message('d-1', null, at(1)), null), null);
  // Scheme list not loaded yet: the link stays.
  assert.equal(String(schemeQuery(smoke, null)), 'object=5962');
  // Messages chosen by text are never selected: the scheme lists only «тревожное».
  assert.equal(String(schemeQuery(onLine, schemes, false)), 'object=5578');
});

test('the text fallback groups the read messages by object and event time', () => {
  const messages = [
    message('x-1', 'b', at(10)),
    message('x-2', 'a', at(12)),
    message('y-1', 'a', at(11)),
    message('z-1', null, at(13)),
  ];
  const window = fromMessages(messages, 2);
  assert.deepEqual(window.latest.map((item) => item.row_uid), ['z-1', 'x-2']);
  assert.equal(nowCountsText(window), 'сообщений: 4 · объектов: 2 · без объекта: 1');
  const [a, b] = window.objects;
  assert.deepEqual([a.object_id, a.record_count, a.channel_count, a.first_event_at, a.last_event_at], ['a', 2, 2, at(11), at(12)]);
  assert.equal(a.last_message.row_uid, 'x-2');
  assert.equal(a.scheme_record_count, null);
  assert.equal(b.object_id, 'b');
});
