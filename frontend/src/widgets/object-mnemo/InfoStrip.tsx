import type { ReactNode } from 'react';
import { Button } from 'antd';
import { IconX } from '@tabler/icons-react';
import { useNavigate } from 'react-router-dom';
import type { ForecastCardView } from '../../api/forecast';
import type { ObjectScheme, SchemeAlarm, SchemeFeeder, SchemePicket } from '../../api/scheme';
import {
  LANDMARK_KIND_HINTS, LANDMARK_KIND_LABELS, LINE_GROUP_LABELS, LINE_TITLES, PICKET_BASIS_LABELS,
} from '../../shared/config/labels';
import { ROUTES } from '../../shared/config/routes';
import {
  FORECAST_OBJECT_NOTE, FORECAST_TARGET, LINE_LOSSES_365_LABEL, NO_PICKET_COLUMN, SMVU_ALARM_NOTE, SMVU_RECORD,
  SMVU_RECORDS_DAY,
} from '../../shared/config/wording';
import { frequencyTexts, picketText } from '../../shared/lib/forecast';
import { fmtDate, fmtDateTime, fmtShortDateTime } from '../../shared/lib/format';
import { AlarmMark, ForecastMark, ReleasedMark } from '../../shared/ui/Marks';
import { RiskLevelMark } from '../../shared/ui/RiskLevel';
import { cardHref } from '../../pages/forecast/back-link';
import { linesWord, picketSpan, pkRangeText } from './mnemo-layout';
import type { MnemoSelection } from './selection';

function pk(picket: SchemePicket): string {
  return picketText({ form: picket.form, from: picket.picket_from, to: picket.picket_to });
}

/** Название линии и ПК без повтора: если пикет уже есть в названии канала, второй раз не пишется. */
export function lineTitle(feeder: Pick<SchemeFeeder, 'feeder_kind' | 'name' | 'picket'>): string {
  const kind = LINE_TITLES[feeder.feeder_kind] ?? 'Линия электропитания';
  const hasPicket = /ПК\s?\d/i.test(feeder.name);
  return hasPicket || feeder.picket.form === 'unknown'
    ? `${kind} · ${feeder.name}`
    : `${kind} · ${feeder.name} · ${pk(feeder.picket)}`;
}

/** Служебные пометки о происхождении — одной серой строкой. */
function provenance(feeder: SchemeFeeder): string {
  const parts = ['тип по названию канала'];
  parts.push(feeder.picket.basis ? `пикет — ${PICKET_BASIS_LABELS[feeder.picket.basis] ?? feeder.picket.basis}` : `пикета нет — колонка «${NO_PICKET_COLUMN}»`);
  // Ответ заказчика 28.09: в скобках названия линии — то, что она питает.
  if (feeder.named_link) parts.push(`питает: ${feeder.named_link.label}`);
  return parts.join(' · ');
}

/** Тревожное сообщение за сутки; если после него была «Норма» — строка серая с временем (B-3). */
function AlarmLine({ alarm }: { alarm: SchemeAlarm }) {
  return (
    <p className={`info-strip__alarm${alarm.cleared_at ? ' info-strip__alarm--past' : ''}`}>
      <AlarmMark size={13} />
      <strong>{SMVU_RECORD}: «{alarm.value_raw}», {fmtDateTime(alarm.event_at)}</strong>
      {alarm.cleared_at ? <span className="muted"> · «{alarm.cleared_value_raw}» {fmtDateTime(alarm.cleared_at)}</span> : null}
    </p>
  );
}

function alarmsCell(feeder: SchemeFeeder): string {
  const alarms = feeder.current_alarms ?? [];
  if (!alarms.length) return '—';
  const total = Math.max(feeder.alarm_records_24h ?? 0, alarms.length);
  const latest = [...alarms].sort((a, b) => Date.parse(b.event_at) - Date.parse(a.event_at))[0];
  const state = alarms.some((alarm) => !alarm.cleared_at) ? '' : ' · после — «Норма»';
  return `${total} · последнее «${latest.value_raw}» ${fmtShortDateTime(latest.event_at)}${state}`;
}

function LinesTable({ feeders, onSelect }: { feeders: SchemeFeeder[]; onSelect: (selection: MnemoSelection) => void }) {
  return (
    <table className="plain-table info-strip__table">
      <thead>
        <tr><th scope="col">Линия</th><th scope="col">Группа</th><th scope="col">Пикет</th><th scope="col">{LINE_LOSSES_365_LABEL}</th><th scope="col">{SMVU_RECORDS_DAY}</th></tr>
      </thead>
      <tbody>
        {feeders.map((feeder) => (
          <tr key={feeder.channel_id}>
            <td>
              <button type="button" className="link-button" onClick={() => onSelect({ kind: 'feeder', channelId: feeder.channel_id })}>
                {feeder.name}
              </button>
            </td>
            <td>{LINE_GROUP_LABELS[feeder.feeder_kind] ?? feeder.feeder_kind}</td>
            <td>{pk(feeder.picket)}</td>
            <td>{feeder.episodes_365d}</td>
            <td>{alarmsCell(feeder)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Полоса сведений под мнемосхемой, перед легендой (не drawer и не закреплена): что выбрано
 * и куда перейти — карточка прогноза C4 («← К схеме»), журнал объекта, исходные сообщения.
 */
export function InfoStrip({
  scheme, objectName, openCard, selection, backTo, onClose, onSelect,
}: {
  scheme: ObjectScheme;
  objectName: string;
  openCard: ForecastCardView | null;
  selection: MnemoSelection;
  backTo: string;
  onClose: () => void;
  onSelect: (selection: MnemoSelection) => void;
}) {
  const navigate = useNavigate();
  const objectId = scheme.object_id;
  const feeders = scheme.feeders ?? [];
  const cardId = selection.kind === 'forecast' || selection.kind === 'released'
    ? selection.cardId
    : openCard?.card.id ?? scheme.released_7d?.[0]?.forecast_id ?? null;
  let channelId: string | null = null;

  let icon: ReactNode = null;
  let title = objectName;
  let meta: string | null = null;
  let body: ReactNode = null;

  if (selection.kind === 'forecast') {
    const frequency = openCard ? frequencyTexts(openCard.card) : null;
    icon = <ForecastMark size={16} />;
    title = `Прогноз на объект · ${objectName}`;
    meta = FORECAST_OBJECT_NOTE;
    body = openCard ? (
      <p className="info-strip__line">
        {FORECAST_TARGET} · до {fmtShortDateTime(openCard.card.window_end)}
        {openCard.day_index && openCard.days_total ? ` · день ${openCard.day_index} из ${openCard.days_total}` : ''}
        {frequency ? ` · ${frequency.list}` : ''}
        {' '}<RiskLevelMark level={openCard.card.risk_level} />
      </p>
    ) : <p className="muted info-strip__line">Открытой карточки по объекту уже нет.</p>;
  } else if (selection.kind === 'released') {
    const item = scheme.released_7d?.find((released) => released.forecast_id === selection.cardId);
    icon = <ReleasedMark size={16} />;
    title = `Карточка снята по событию · ${objectName}`;
    body = <p className="info-strip__line">Событие зарегистрировано {fmtDateTime(item?.released_at)}; карточка ушла из списка прогноза.</p>;
  } else if (selection.kind === 'feeder') {
    const feeder = feeders.find((item) => item.channel_id === selection.channelId);
    channelId = feeder?.channel_id ?? null;
    title = feeder ? lineTitle(feeder) : 'Линия не найдена на схеме объекта';
    meta = feeder ? provenance(feeder) : null;
    body = feeder ? (
      <>
        {[...(feeder.current_alarms ?? [])].reverse().slice(0, 5).map((alarm) => <AlarmLine key={alarm.row_uid} alarm={alarm} />)}
        {(feeder.current_alarms?.length ?? 0) > 5 ? (
          <p className="muted small info-strip__line">и ещё тревожных сообщений за сутки: {Math.max(feeder.alarm_records_24h ?? 0, feeder.current_alarms!.length) - 5}</p>
        ) : null}
        <p className="info-strip__line">
          {LINE_LOSSES_365_LABEL}: {feeder.episodes_365d}
          {feeder.last_episode_at ? `, последняя ${fmtDate(feeder.last_episode_at)}` : ''}
        </p>
      </>
    ) : null;
  } else if (selection.kind === 'alarm') {
    const feeder = feeders.find((item) => (item.current_alarms ?? []).some((alarm) => alarm.row_uid === selection.rowUid));
    const alarm = feeder?.current_alarms?.find((item) => item.row_uid === selection.rowUid);
    channelId = feeder?.channel_id ?? null;
    icon = <AlarmMark size={16} />;
    title = feeder ? lineTitle(feeder) : 'Сообщение не найдено среди сообщений за сутки';
    meta = SMVU_ALARM_NOTE;
    body = alarm ? <AlarmLine alarm={alarm} /> : null;
  } else if (selection.kind === 'landmark') {
    const landmark = scheme.landmarks?.[selection.index];
    const kind = landmark ? LANDMARK_KIND_LABELS[landmark.kind] ?? landmark.kind : '';
    title = landmark ? `${kind[0]?.toUpperCase() ?? ''}${kind.slice(1)} · ${landmark.name} · ${pk(landmark.picket)}` : 'Узел не найден';
    meta = [landmark ? LANDMARK_KIND_HINTS[landmark.kind] ?? null : null,
      landmark?.picket.basis ? `пикет — ${PICKET_BASIS_LABELS[landmark.picket.basis] ?? landmark.picket.basis}` : null,
      'подключение линий к вводам и АВР в данных не записано'].filter(Boolean).join(' · ');
  } else if (selection.kind === 'alarms') {
    const inGroup = feeders.filter((feeder) => feeder.feeder_kind === selection.group && feeder.current_alarms?.length);
    icon = <AlarmMark size={16} />;
    title = `${LINE_GROUP_LABELS[selection.group] ?? 'Линии'} · тревожные сообщения СМВУ за сутки · ${inGroup.length} ${linesWord(inGroup.length)}`;
    meta = `${SMVU_ALARM_NOTE}; после сообщения линия могла вернуться в «Норма»`;
    body = <LinesTable feeders={inGroup} onSelect={onSelect} />;
  } else if (selection.kind === 'range') {
    const inRange = feeders.filter((feeder) => {
      const span = picketSpan(feeder.picket);
      return feeder.feeder_kind === selection.group && span !== null && span.from <= selection.to && span.to >= selection.from;
    });
    title = `${LINE_GROUP_LABELS[selection.group] ?? 'Линии'} · ${inRange.length} ${linesWord(inRange.length)} · ${pkRangeText(selection.from, selection.to)}`;
    meta = 'линии рядом на схеме объединены в один знак; выберите линию в таблице';
    body = <LinesTable feeders={inRange} onSelect={onSelect} />;
  } else {
    const unknown = feeders.filter((feeder) => feeder.picket.form === 'unknown');
    title = `Линии без пикета · ${unknown.length}`;
    meta = 'пикета в названии канала нет — место на схеме не угадывается';
    body = <LinesTable feeders={unknown} onSelect={onSelect} />;
  }


  return (
    <section className="info-strip" aria-live="polite" aria-label="Сведения о выбранном на схеме">
      <div className="info-strip__text">
        <h3>{icon}<span>{title}</span></h3>
        {body}
        {meta ? <p className="info-strip__meta muted small">{meta}</p> : null}
      </div>
      <div className="info-strip__actions">
        {cardId ? (
          <Button type="primary" onClick={() => navigate(cardHref(cardId, backTo))}>Открыть карточку объекта</Button>
        ) : null}
        <Button onClick={() => navigate(`${ROUTES.journal}?object=${encodeURIComponent(objectId)}`)}>К журналу</Button>
        <Button icon={<IconX size={16} />} onClick={onClose} aria-label="Закрыть сведения (Esc)" title="Закрыть (Esc)" />
      </div>
    </section>
  );
}
