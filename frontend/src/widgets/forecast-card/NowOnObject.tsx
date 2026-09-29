import { Link } from 'react-router-dom';
import type { ObjectScheme, SchemeAlarm, SchemeFeeder } from '../../api/scheme';
import { ROUTES } from '../../shared/config/routes';
import {
  FORECAST_EVENT_MARK, NOT_FORECAST_EVENT, POWER_OFF_ALARM_NOTE, SMVU_RECORDS_HINT, fillTemplate,
} from '../../shared/config/wording';
import { fmtDateTime, fmtShortDateTime, fmtTime } from '../../shared/lib/format';
import type { AsyncState } from '../../shared/lib/use-async';
import { AlarmMark } from '../../shared/ui/Marks';
import { linesWord } from '../object-mnemo/mnemo-layout';

/** Строк в блоке не больше: остальное — на схеме (F-01, «Срок» виден без прокрутки). */
const GROUPS_SHOWN = 5;

interface AlarmGroup {
  text: string;
  records: number;
  lines: Set<string>;
  first: string;
  last: string;
  active: boolean;
}

function norm(value: string): string {
  return value.trim().toLocaleLowerCase('ru');
}

/** Записи за сутки по тексту: «Отключено устройство» · 32 линии · 30.06 16:30–17:45. */
function groupByText(items: { feeder: SchemeFeeder; alarm: SchemeAlarm }[]): AlarmGroup[] {
  const groups = new Map<string, AlarmGroup>();
  for (const { feeder, alarm } of items) {
    const key = norm(alarm.value_raw);
    const group = groups.get(key) ?? {
      text: alarm.value_raw, records: 0, lines: new Set<string>(), first: alarm.event_at, last: alarm.event_at, active: false,
    };
    group.records += 1;
    group.lines.add(feeder.channel_id);
    if (Date.parse(alarm.event_at) < Date.parse(group.first)) group.first = alarm.event_at;
    if (Date.parse(alarm.event_at) > Date.parse(group.last)) group.last = alarm.event_at;
    group.active ||= !alarm.cleared_at;
    groups.set(key, group);
  }
  return [...groups.values()].sort((a, b) => Date.parse(b.last) - Date.parse(a.last));
}

function span(group: AlarmGroup): string {
  return group.first === group.last
    ? fmtShortDateTime(group.first)
    : `${fmtShortDateTime(group.first)}–${fmtTime(group.last)}`;
}

/**
 * «За сутки на объекте» в карточке (F-01): тревожные сообщения СМВУ за 24 ч до среза по линиям
 * объекта из той же схемы, что на мнемосхеме, сгруппированные по тексту, не больше пяти строк.
 * Это не событие прогноза; «Обесточен», ещё не сменившийся «Нормой», — повод действовать по
 * тревоге (показывается только для записей с отметкой «тревожное»).
 */
export function NowOnObject({ scheme }: { scheme: AsyncState<ObjectScheme> }) {
  if (scheme.loading) return <p className="card-now card-now--none small">За сутки на объекте: загрузка тревожных сообщений СМВУ…</p>;
  if (!scheme.data) {
    return scheme.error ? <p className="card-now card-now--none small">За сутки на объекте: тревожные сообщения СМВУ не загружены.</p> : null;
  }
  const feeders = scheme.data.feeders ?? [];
  const items = feeders.flatMap((feeder) => (feeder.current_alarms ?? []).map((alarm) => ({ feeder, alarm })));
  if (!items.length) {
    return <p className="card-now card-now--none small">За сутки на объекте: тревожных сообщений СМВУ по линиям нет на {fmtShortDateTime(scheme.data.as_of)}.</p>;
  }
  const withAlarms = feeders.filter((feeder) => feeder.current_alarms?.length);
  const records = withAlarms.reduce((sum, feeder) => sum + Math.max(feeder.alarm_records_24h ?? 0, feeder.current_alarms!.length), 0);
  const groups = groupByText(items);
  const powerOff = groups.some((group) => group.active && norm(group.text) === 'обесточен');
  const schemeHref = `${ROUTES.scheme}?object=${encodeURIComponent(scheme.data.object_id)}`;
  return (
    <section className="card-now" aria-labelledby="card-now-title"
      title={fillTemplate(SMVU_RECORDS_HINT, { asOf: fmtDateTime(scheme.data.as_of) })}>
      <h3 id="card-now-title">
        За сутки на объекте · тревожных сообщений СМВУ: {records} · линий: {withAlarms.length}
      </h3>
      <ul>
        {groups.slice(0, GROUPS_SHOWN).map((group) => (
          <li key={group.text} className={group.active ? undefined : 'card-now__past'}>
            <AlarmMark size={13} />
            <span className="card-now__value">«{group.text}»</span>
            <span>{group.lines.size} {linesWord(group.lines.size)} · сообщений: {group.records}</span>
            <span className="muted small">
              {span(group)}{group.active ? '' : ' · после сообщений — «Норма»'}
              {norm(group.text) === 'неисправен' ? ` · ${FORECAST_EVENT_MARK}` : ''}
            </span>
          </li>
        ))}
      </ul>
      {groups.length > GROUPS_SHOWN ? <p className="muted small">и ещё видов сообщений: {groups.length - GROUPS_SHOWN}</p> : null}
      <p className="card-now__note-muted small">
        {NOT_FORECAST_EVENT}. <Link to={schemeHref}>Все сообщения — на схеме →</Link>
      </p>
      {powerOff ? <p className="card-now__note">{POWER_OFF_ALARM_NOTE}</p> : null}
    </section>
  );
}
