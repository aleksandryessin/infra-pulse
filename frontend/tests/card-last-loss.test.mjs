import assert from 'node:assert/strict';
import { test } from 'node:test';

import { LISTED_LINES_LAST_LOSS_LABEL } from '../src/shared/config/wording.ts';
import { episodeFacts, listedLinesFoot } from '../src/shared/lib/forecast.ts';

const loss = (at) => ({ kind: 'last_connection_loss_at', value_at: at });

// Проверка 29.09: у «ДП объект Эпсилон» карточка показывала 25.06, а объект терял связь 29.06 на
// линии вне списка. Фактор считается только по перечисленным линиям — подпись говорит об этом.
test('last loss is the latest start among the listed lines, and the label says so', () => {
  const facts = episodeFacts({
    facts: [{ kind: 'events_365d', value_number: 72 }],
    channels: [
      { channel_id: '1', reason_facts: [loss('2026-06-24T22:30:06Z')] },
      { channel_id: '2', reason_facts: [loss('2026-06-25T12:25:34Z')] },
    ],
  });
  assert.equal(facts.lastEpisodeAt, '2026-06-25T12:25:34Z');
  assert.equal(LISTED_LINES_LAST_LOSS_LABEL, 'Последняя потеря связи на линиях из списка');
});

test('footnote names the list only when some tracked lines are not listed', () => {
  assert.equal(listedLinesFoot(null), '');
  assert.equal(listedLinesFoot({ complete: true, listed: 3 }), '');
  assert.equal(
    listedLinesFoot({ complete: false, listed: 5 }),
    ' В «Подробнее» — список из 5 линий с наибольшим числом потерь связи; на других линиях объекта связь могла пропадать позже.',
  );
  assert.match(listedLinesFoot({ complete: false, listed: 1 }), /из 1 линии /);
  assert.match(listedLinesFoot({ complete: false, listed: 21 }), /из 21 линии /);
  assert.match(listedLinesFoot({ complete: false, listed: 11 }), /из 11 линий /);
});
