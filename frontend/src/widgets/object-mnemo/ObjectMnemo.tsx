import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { Button } from 'antd';
import { IconArrowsHorizontal, IconBox, IconZoomReset } from '@tabler/icons-react';
import { Link } from 'react-router-dom';
import type { ForecastCardView } from '../../api/forecast';
import type { ObjectScheme } from '../../api/scheme';
import {
  LANDMARK_KIND_HINTS, LANDMARK_KIND_LABELS, LINE_GROUP_HINTS, LINE_GROUP_LABELS, LINE_TITLES,
} from '../../shared/config/labels';
import {
  FORECAST_OBJECT_HINT, FORECAST_TARGET, NO_CARD_BELOW_LIST, NO_CARD_NO_EVENTS, NO_PICKET_COLUMN, NO_PICKET_TEXT,
  NO_NORM_AFTER_LABEL, SCHEME_HOW_TO_READ, SMVU_RECORD, SMVU_RECORDS_DAY, SMVU_RECORDS_HINT, fillTemplate,
} from '../../shared/config/wording';
import { fmtDateTime, fmtShortDateTime } from '../../shared/lib/format';
import { AlarmMark, ForecastMark, ReleasedMark } from '../../shared/ui/Marks';
import { RiskLevelMark } from '../../shared/ui/RiskLevel';
import { cardHref } from '../../pages/forecast/back-link';
import { InfoStrip } from './InfoStrip';
import { LANDMARK_ICONS, LINE_ICONS } from './line-icons';
import { MnemoLegend } from './MnemoLegend';
import {
  LINE_KINDS, M, fmtPk, layoutMnemo, lineAlarmRecords, lineKind, linesWord, picketSpan, pkRangeText, textWidth,
  type LineKind, type MnemoItem, type MnemoLayout,
} from './mnemo-layout';
import { groupsFromParam, sameSelection, type MnemoSelection } from './selection';
import './ObjectMnemo.css';

function capitalize(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

/** Элемент схемы — кнопка: Tab, Enter/пробел выбирают; фокус и выбор видны без цвета заливки. */
function Hit({
  label, selected, onSelect, className, children,
}: {
  label: string;
  selected: boolean;
  onSelect: () => void;
  className?: string;
  children: ReactNode;
}) {
  const onKeyDown = (event: KeyboardEvent<SVGGElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onSelect();
    }
  };
  return (
    <g className={`mnemo-hit${selected ? ' is-selected' : ''}${className ? ` ${className}` : ''}`} role="button" tabIndex={0}
      aria-label={label} aria-pressed={selected} onClick={onSelect} onKeyDown={onKeyDown}>
      <title>{label}</title>
      {children}
    </g>
  );
}

/** Выбор знака: одна линия — `feeder`, знак нескольких линий — `range` группы по ПК. */
function itemSelection(item: MnemoItem): MnemoSelection {
  if (item.type === 'line') return { kind: 'feeder', channelId: item.id };
  if (item.unknown || !item.range) return { kind: 'unknown' };
  return { kind: 'range', group: item.kind, from: item.range.from, to: item.range.to };
}

function itemSelected(item: MnemoItem, selection: MnemoSelection | null): boolean {
  if (!selection) return false;
  if (selection.kind === 'feeder') return item.ids.includes(selection.channelId);
  if (selection.kind === 'alarm') return false;
  return sameSelection(selection, itemSelection(item));
}

function LineItem({ item, selected, onSelect }: { item: MnemoItem; selected: boolean; onSelect: () => void }) {
  const IconKind = LINE_ICONS[item.kind];
  const { cx, cy } = item;
  const top = cy - M.r - 4;
  const bottom = item.labelHidden ? cy + M.r + 4 : item.label.y + 4;
  const cluster = item.type === 'cluster';
  const aria = cluster
    ? `${LINE_GROUP_LABELS[item.kind]}: ${item.name}, потерь связи за 365 сут ${item.badge.text || 'не было'}`
    : `${LINE_TITLES[item.kind]} ${item.name}: потерь связи за 365 сут ${item.badge.text || 'не было'}${item.link ? `, питает: ${item.link.label}` : ''}`;
  const labelWidth = textWidth(item.label.text) + 8;
  return (
    <Hit label={aria} selected={selected} onSelect={onSelect} className={item.labelHidden ? 'mnemo-hit--dense' : undefined}>
      <rect className="mnemo-hit__area" x={item.extent.left - 4} y={top} width={item.extent.right - item.extent.left + 8} height={bottom - top} />
      {item.bar ? <rect className="mnemo-bar" x={item.bar.x1} y={cy - M.barH / 2} width={item.bar.x2 - item.bar.x1} height={M.barH} rx={2} /> : null}
      {item.link ? (
        <g className="mnemo-link">
          <line x1={item.link.x1} x2={item.link.x2} y1={cy} y2={cy} />
          <rect x={item.link.boxX} y={cy - M.linkBoxH / 2} width={item.link.boxW} height={M.linkBoxH} rx={2} />
          <text x={item.link.boxX + item.link.boxW / 2} y={cy + 4} textAnchor="middle">{item.link.label}</text>
        </g>
      ) : null}
      {cluster ? <circle className="mnemo-node mnemo-node--stack" cx={cx + 4} cy={cy - 4} r={M.r} /> : null}
      <circle className="mnemo-node" cx={cx} cy={cy} r={M.r} />
      <IconKind className="mnemo-icon" x={cx - 8} y={cy - 8} size={16} stroke={1.6} aria-hidden />
      {item.badge.text ? (
        <>
          <rect className="mnemo-badge" x={item.badge.x} y={cy - M.badgeH / 2} width={item.badge.w} height={M.badgeH} rx={4} />
          <text className="mnemo-badge__text" x={item.badge.x + item.badge.w / 2} y={cy + 4} textAnchor="middle">{item.badge.text}</text>
        </>
      ) : null}
      {item.labelHidden ? (
        <g className="mnemo-label-pop">
          <rect x={item.label.x - 4} y={item.label.y - 12} width={labelWidth} height={17} rx={2} />
          <text className="mnemo-label" x={item.label.x} y={item.label.y}>{item.label.text}</text>
        </g>
      ) : <text className="mnemo-label" x={item.label.x} y={item.label.y}>{item.label.text}</text>}
      <circle className="mnemo-focus" cx={cx} cy={cy} r={M.r + 8} />
      {selected ? <circle className="mnemo-ring" cx={cx} cy={cy} r={M.r + 5} /> : null}
    </Hit>
  );
}

function AlarmItem({ item, selected, onSelect, lineName }: { item: MnemoItem; selected: boolean; onSelect: () => void; lineName: string }) {
  const alarm = item.alarm;
  if (!alarm) return null;
  const { w } = alarm;
  const state = alarm.active ? '' : '; после сообщения линия вернулась в «Норма»';
  return (
    <Hit label={`${SMVU_RECORD} за сутки: ${alarm.text}, ${lineName}${state}`} selected={selected} onSelect={onSelect}
      className={`mnemo-alarm${alarm.active ? '' : ' mnemo-alarm--cleared'}`}>
      <rect className="mnemo-hit__area" x={alarm.x - 2} y={alarm.y - 2} width={w + 4} height={M.alarm + 4} />
      <line className="mnemo-alarm__stem" x1={item.cx} x2={item.cx} y1={alarm.y + M.alarm} y2={item.cy - M.r - 1} />
      <rect className="mnemo-alarm__square" x={alarm.x} y={alarm.y} width={w} height={M.alarm} rx={1} />
      <text className="mnemo-alarm__count" x={alarm.x + w / 2} y={alarm.y + 12.5} textAnchor="middle">
        {alarm.count > 1 ? `!${alarm.count}` : '!'}
      </text>
      <rect className="mnemo-focus" x={alarm.x - 4} y={alarm.y - 4} width={w + 8} height={M.alarm + 8} rx={2} />
      {selected ? <rect className="mnemo-ring" x={alarm.x - 3} y={alarm.y - 3} width={w + 6} height={M.alarm + 6} rx={2} /> : null}
    </Hit>
  );
}

function Items({
  items, selection, onSelect, nameOf,
}: {
  items: MnemoItem[];
  selection: MnemoSelection | null;
  onSelect: (selection: MnemoSelection) => void;
  nameOf: (id: string) => string;
}) {
  return (
    <>
      {items.map((item) => {
        const own = itemSelection(item);
        const alarmSelection: MnemoSelection | null = item.alarm ? { kind: 'alarm', rowUid: item.alarm.id } : null;
        return (
          <g key={`${item.unknown ? 'u' : 't'}-${item.id}`}>
            <LineItem item={item} selected={itemSelected(item, selection)} onSelect={() => onSelect(own)} />
            {alarmSelection ? (
              <AlarmItem item={item} lineName={item.type === 'line' ? nameOf(item.id) : item.name}
                selected={sameSelection(selection, alarmSelection)} onSelect={() => onSelect(alarmSelection)} />
            ) : null}
          </g>
        );
      })}
    </>
  );
}

function Route({
  layout, hatchId, state, selected, onSelect, label,
}: {
  layout: MnemoLayout;
  hatchId: string;
  state: 'forecast' | 'released' | 'none';
  selected: boolean;
  onSelect: (() => void) | null;
  label: string;
}) {
  const tunnel = layout.tunnel;
  if (!tunnel) return null;
  const { x1, x2, y, h } = tunnel;
  const dx = M.tunnelDx;
  const dy = M.tunnelDy;
  const body = (
    <>
      <path className="mnemo-tunnel__top" d={`M${x1} ${y} L${x1 + dx} ${y - dy} L${x2 + dx} ${y - dy} L${x2} ${y} Z`} />
      <path className="mnemo-tunnel__side" d={`M${x2} ${y} L${x2 + dx} ${y - dy} L${x2 + dx} ${y + h - dy} L${x2} ${y + h} Z`} />
      <rect className={`mnemo-tunnel__front mnemo-tunnel__front--${state}`} x={x1} y={y} width={x2 - x1} height={h}
        fill={state === 'forecast' ? `url(#${hatchId})` : undefined} />
      <rect className="mnemo-focus" x={x1 - 5} y={y - dy - 5} width={x2 - x1 + dx + 10} height={h + dy + 10} rx={2} />
      {selected ? <rect className="mnemo-ring" x={x1 - 4} y={y - 4} width={x2 - x1 + 8} height={h + 8} rx={2} /> : null}
    </>
  );
  return (
    <>
      {onSelect ? <Hit label={label} selected={selected} onSelect={onSelect}>{body}</Hit> : <g aria-hidden>{body}</g>}
      <rect className="mnemo-tunnel__tail" x={tunnel.tail.x1} y={y + 2} width={tunnel.tail.x2 - tunnel.tail.x1} height={h - 4} />
      <text className="mnemo-small" x={(tunnel.tail.x1 + tunnel.tail.x2) / 2} y={y + h / 2 + 4} textAnchor="middle">…</text>
      <text className="mnemo-small" x={(tunnel.tail.x1 + tunnel.tail.x2) / 2} y={y - dy - 6} textAnchor="middle">длина неизвестна</text>
    </>
  );
}

export interface ObjectMnemoProps {
  scheme: ObjectScheme;
  objectName: string;
  openCard: ForecastCardView | null;
  selection: MnemoSelection | null;
  onSelect: (selection: MnemoSelection | null) => void;
  /** Видимые группы линий (null — все); фильтр в строке с «Вписать» (P1-9). */
  visibleGroups: LineKind[] | null;
  onGroupsChange: (groups: LineKind[] | null) => void;
  /** Текущий адрес схемы — для «← К схеме» из карточки. */
  backTo: string;
}

/** Переключатели групп линий: число — линии группы вместе с «Без пикета»; пустая группа неактивна. */
function GroupFilter({
  counts, hidden, onChange,
}: {
  counts: Record<LineKind, number>;
  hidden: LineKind[];
  onChange: (groups: LineKind[] | null) => void;
}) {
  const toggle = (kind: LineKind) => {
    const visible = LINE_KINDS.filter((item) => (item === kind ? hidden.includes(kind) : !hidden.includes(item)));
    onChange(groupsFromParam(visible.join(',')));
  };
  return (
    <div className="mnemo-groups" role="group" aria-label="Группы линий электропитания">
      {LINE_KINDS.map((kind) => {
        const IconKind = LINE_ICONS[kind];
        const shown = !hidden.includes(kind);
        return (
          <button key={kind} type="button" className="mnemo-group-toggle" aria-pressed={shown && counts[kind] > 0}
            disabled={!counts[kind]} onClick={() => toggle(kind)}
            title={`${LINE_GROUP_HINTS[kind]}. ${counts[kind] ? `${shown ? 'Скрыть' : 'Показать'} группу` : 'Линий этой группы на объекте нет'}`}>
            <IconKind size={14} stroke={1.6} aria-hidden />
            {LINE_GROUP_LABELS[kind]} <span className="mnemo-group-toggle__count">{counts[kind]}</span>
          </button>
        );
      })}
      {hidden.length ? <button type="button" className="link-button mnemo-groups__all" onClick={() => onChange(null)}>Все группы</button> : null}
    </div>
  );
}

/**
 * Мнемосхема одного объекта: трасса по пикетам с вводами и АВР, пять полос групп линий
 * электропитания, колонка «Без пикета», полоса сведений и легенда. Прогноз строится на объект —
 * штриховка на всю трассу. Связи рисуются только по `named_link`. Длинные объекты
 * прокручиваются по горизонтали (не меньше MIN_PX_PER_PK на пикет), «Вписать» — целиком.
 */
export function ObjectMnemo({
  scheme, objectName, openCard, selection, onSelect, visibleGroups, onGroupsChange, backTo,
}: ObjectMnemoProps) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const stripRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [fit, setFit] = useState(false);
  const [trackScroll, setTrackScroll] = useState(0);
  const scrollFrame = useRef(0);
  const onTrackScroll = () => {
    cancelAnimationFrame(scrollFrame.current);
    scrollFrame.current = requestAnimationFrame(() => setTrackScroll(trackRef.current?.scrollLeft ?? 0));
  };
  const hatchId = `mnemo-hatch-${useId().replace(/:/g, '')}`;

  useLayoutEffect(() => {
    const element = canvasRef.current;
    if (!element) return undefined;
    const update = () => setWidth(element.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const feeders = useMemo(() => scheme.feeders ?? [], [scheme]);
  const landmarks = useMemo(() => scheme.landmarks ?? [], [scheme]);
  const hiddenKey = visibleGroups === null ? '' : LINE_KINDS.filter((kind) => !visibleGroups.includes(kind)).join(',');
  const hidden = useMemo(() => (hiddenKey ? hiddenKey.split(',') as LineKind[] : []), [hiddenKey]);
  const groupCounts = useMemo(() => {
    const counts = Object.fromEntries(LINE_KINDS.map((kind) => [kind, 0])) as Record<LineKind, number>;
    for (const feeder of feeders) counts[lineKind(feeder.feeder_kind)] += 1;
    return counts;
  }, [feeders]);
  const layout = useMemo(() => (width ? layoutMnemo({
    width,
    fit,
    hidden,
    landmarks: landmarks.map((landmark, index) => ({
      index,
      kind: landmark.kind,
      label: capitalize(LANDMARK_KIND_LABELS[landmark.kind] ?? landmark.kind),
      picket: landmark.picket,
    })),
    lines: feeders.map((feeder) => ({
      id: feeder.channel_id,
      name: feeder.name,
      kind: feeder.feeder_kind,
      picket: feeder.picket,
      episodes: feeder.episodes_365d,
      link: feeder.named_link?.label ?? null,
      alarms: [...(feeder.current_alarms ?? [])]
        .sort((a, b) => Date.parse(b.event_at) - Date.parse(a.event_at))
        .map((alarm) => ({
          id: alarm.row_uid,
          text: `«${alarm.value_raw}» ${fmtShortDateTime(alarm.event_at)}`,
          active: !alarm.cleared_at,
        })),
      alarmRecords: feeder.alarm_records_24h ?? undefined,
    })),
  }) : null), [width, fit, feeders, landmarks, hidden]);

  const selectionKey = selection ? JSON.stringify(selection) : '';
  // Выбор (кликом, клавишей или по ссылке): полоса сведений и выбранный знак — в поле зрения.
  useEffect(() => {
    if (!selectionKey) return;
    const frame = requestAnimationFrame(() => {
      trackRef.current?.querySelector('.mnemo-hit.is-selected')?.scrollIntoView({ block: 'nearest', inline: 'center' });
      stripRef.current?.scrollIntoView({ block: 'nearest' });
    });
    return () => cancelAnimationFrame(frame);
  }, [selectionKey, fit]);

  const released = scheme.released_7d?.[0] ?? null;
  const routeState: 'forecast' | 'released' | 'none' = openCard ? 'forecast' : released ? 'released' : 'none';
  const alarmFeeders = feeders.filter((feeder) => feeder.current_alarms?.length);
  const alarmRecords = alarmFeeders.reduce(
    (sum, feeder) => sum + lineAlarmRecords({ alarms: feeder.current_alarms ?? [], alarmRecords: feeder.alarm_records_24h ?? undefined }), 0,
  );
  const activeLines = alarmFeeders.filter((feeder) => (feeder.current_alarms ?? []).some((alarm) => !alarm.cleared_at)).length;
  const latestAlarm = alarmFeeders.flatMap((feeder) => feeder.current_alarms ?? [])
    .sort((a, b) => Date.parse(b.event_at) - Date.parse(a.event_at))[0] ?? null;
  const known = feeders.filter((feeder) => picketSpan(feeder.picket) !== null).length;
  const episodes = feeders.reduce((sum, feeder) => sum + feeder.episodes_365d, 0);
  const select = (next: MnemoSelection) => onSelect(next);
  const isSelected = (candidate: MnemoSelection) => sameSelection(selection, candidate);
  const nameOf = (id: string) => feeders.find((feeder) => feeder.channel_id === id)?.name ?? id;

  // «ещё N линий правее»: знаки, чей значок правее видимой части прокручиваемой трассы.
  const beyond = useMemo(() => {
    if (!layout?.track.scrolls) return null;
    const edge = trackScroll + layout.track.viewport - M.r;
    const rest = layout.rows.flatMap((row) => row.items).filter((item) => item.cx > edge && item.range);
    if (!rest.length) return null;
    return {
      lines: rest.reduce((sum, item) => sum + item.ids.length, 0),
      from: Math.min(...rest.map((item) => item.range!.from)),
      to: Math.max(...rest.map((item) => item.range!.to)),
    };
  }, [layout, trackScroll]);

  const routeSelection: MnemoSelection | null = openCard
    ? { kind: 'forecast', cardId: openCard.card.id }
    : released ? { kind: 'released', cardId: released.forecast_id } : null;
  const showFit = layout ? layout.track.scrolls || fit : false;

  return (
    <section className="panel mnemo" aria-labelledby="mnemo-title">
      <header className="mnemo__head">
        <div>
          <h2 id="mnemo-title" className="mnemo__title">{objectName}</h2>
          <p className="mnemo__subtitle">{FORECAST_TARGET} · условная схема по пикетам</p>
        </div>
        <dl className="mnemo__counters">
          <dt>Линий электропитания</dt><dd>{feeders.length}</dd>
          <dt>с пикетом</dt><dd>{known}</dd>
          <dt>без пикета</dt><dd>{feeders.length - known}</dd>
          <dt title="Сумма по линиям; одно событие объекта обычно затрагивает несколько линий">потерь связи линий за 365 сут</dt>
          <dd title="Сумма по линиям; одно событие объекта обычно затрагивает несколько линий">{episodes}</dd>
        </dl>
      </header>

      <div className="mnemo__states">
        {openCard ? (
          <div className="mnemo-chip mnemo-chip--forecast">
            <button type="button" className="mnemo-chip__main" onClick={() => routeSelection && select(routeSelection)}
              aria-pressed={routeSelection ? isSelected(routeSelection) : false} title={FORECAST_OBJECT_HINT}>
              <ForecastMark size={14} title="прогноз" />
              Прогноз · до {fmtShortDateTime(openCard.card.window_end)}
              {openCard.day_index && openCard.days_total ? ` · день ${openCard.day_index} из ${openCard.days_total}` : ''}
            </button>
            <RiskLevelMark level={openCard.card.risk_level} />
            <Link to={cardHref(openCard.card.id, backTo)}>Открыть карточку →</Link>
          </div>
        ) : released ? (
          <div className="mnemo-chip mnemo-chip--released">
            <button type="button" className="mnemo-chip__main" onClick={() => routeSelection && select(routeSelection)}
              aria-pressed={routeSelection ? isSelected(routeSelection) : false}>
              <ReleasedMark size={14} title="снята по событию" />
              Карточка снята по событию {fmtShortDateTime(released.released_at)}
            </button>
            <Link to={cardHref(released.forecast_id, backTo)}>Открыть карточку →</Link>
          </div>
        ) : (
          // F-06: причина без backend — по сумме потерь связи линий за 365 сут.
          <span className="mnemo-chip mnemo-chip--none mnemo-chip--reason">{episodes > 0 ? NO_CARD_BELOW_LIST : NO_CARD_NO_EVENTS}</span>
        )}
        {latestAlarm ? (
          <button type="button" className={`mnemo-chip ${activeLines ? 'mnemo-chip--alarm' : 'mnemo-chip--alarm-past'}`}
            title={fillTemplate(SMVU_RECORDS_HINT, { asOf: fmtDateTime(scheme.as_of) })}
            onClick={() => select({ kind: 'alarm', rowUid: latestAlarm.row_uid })}>
            <AlarmMark size={14} />
            {SMVU_RECORDS_DAY}: {alarmRecords} · линий: {alarmFeeders.length} · {NO_NORM_AFTER_LABEL}: {activeLines}
          </button>
        ) : null}
      </div>

      <p className="mnemo__convention"><strong>Как читать:</strong> {SCHEME_HOW_TO_READ}</p>
      <div className="mnemo__bar">
        <GroupFilter counts={groupCounts} hidden={hidden} onChange={onGroupsChange} />
        {showFit ? (
          <Button size="small" icon={fit ? <IconZoomReset size={16} /> : <IconArrowsHorizontal size={16} />}
            onClick={() => setFit((value) => !value)} aria-pressed={fit}
            title={fit ? 'Подробный масштаб; трасса прокручивается по горизонтали' : 'Показать всю трассу по ширине'}>
            {fit ? 'Крупнее' : 'Вписать'}
          </Button>
        ) : null}
      </div>

      <div ref={canvasRef} className="mnemo__canvas">
        {layout ? (
          <div className="mnemo__areas" style={{ height: layout.height }}>
            <svg className="mnemo-svg mnemo-svg--left" width={layout.left.width} height={layout.height} aria-hidden>
              {layout.rows.map((row, index) => (index % 2 === 0 ? (
                <rect key={`band-${row.kind}`} className="mnemo-band" x={0} y={row.y} width={layout.left.width} height={row.h} />
              ) : null))}
              {layout.compact ? (
                <text className="mnemo-small" x={0} y={(layout.tunnel?.y ?? 40) + 17}>
                  <tspan x={0}>Трасса</tspan>
                  <tspan x={0} dy={15}>по пикетам</tspan>
                </text>
              ) : <text className="mnemo-small" x={0} y={(layout.tunnel?.y ?? 40) + 24}>Трасса по пикетам</text>}
              <text className="mnemo-rows-title" x={0} y={layout.rowsHeaderY}>Линии</text>
              <text className="mnemo-rows-title" x={0} y={layout.rowsHeaderY + 14}>электропитания</text>
              {layout.rows.filter((row) => !row.hidden).map((row) => {
                const IconKind = LINE_ICONS[row.kind];
                return (
                  <g key={`row-${row.kind}`}>
                    <title>{LINE_GROUP_HINTS[row.kind]}</title>
                    <IconKind className="mnemo-icon mnemo-icon--row" x={0} y={row.labelY - 12} size={16} stroke={1.6} aria-hidden />
                    <text className="mnemo-row-label" x={22} y={row.labelY}>{LINE_GROUP_LABELS[row.kind]}</text>
                  </g>
                );
              })}
            </svg>

            <div ref={trackRef} className={`mnemo__track${layout.track.scrolls ? ' is-scrolling' : ''}`}
              style={{ width: layout.track.viewport }} onScroll={onTrackScroll}>
              <svg className="mnemo-svg" width={layout.track.width} height={layout.height}
                role="group" aria-label={`Мнемосхема объекта ${objectName}: линии электропитания по пикетам`}>
                <defs>
                  <pattern id={hatchId} width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                    <rect className="mnemo-hatch__bg" width="7" height="7" />
                    <line className="mnemo-hatch__line" x1="0" y1="0" x2="0" y2="7" />
                  </pattern>
                </defs>
                {layout.rows.map((row, index) => (index % 2 === 0 ? (
                  <rect key={`band-${row.kind}`} className="mnemo-band" x={0} y={row.y} width={layout.track.width} height={row.h} />
                ) : null))}
                {layout.axis.ticks.filter((tick) => tick.major).map((tick) => (
                  <line key={`grid-${tick.pk}`} className="mnemo-grid" x1={tick.x} x2={tick.x} y1={layout.axis.y - 4} y2={layout.rowsBottom} />
                ))}

                <Route layout={layout} hatchId={hatchId} state={routeState}
                  selected={routeSelection ? isSelected(routeSelection) : false}
                  onSelect={routeSelection ? () => select(routeSelection) : null}
                  label={openCard ? `Прогноз на объект ${objectName}: штриховка на всю трассу` : `Карточка по объекту ${objectName} снята по событию`} />
                {!layout.tunnel ? (
                  <text className="mnemo-small" x={18} y={52}>Пикеты в названиях каналов не найдены — все линии в колонке «{NO_PICKET_COLUMN}»</text>
                ) : null}

                {layout.landmarks.map((landmark) => {
                  const source = landmarks[landmark.index];
                  const IconKind = LANDMARK_ICONS[landmark.kind] ?? IconBox;
                  const x = landmark.cx - M.cubeW / 2;
                  const bottom = landmark.bottom;
                  const top = bottom - M.cubeH;
                  const { cubeDx: dx, cubeDy: dy, cubeW: w } = M;
                  const own: MnemoSelection = { kind: 'landmark', index: landmark.index };
                  return (
                    <Hit key={`landmark-${landmark.index}`} selected={isSelected(own)} onSelect={() => select(own)}
                      label={`${landmark.label}: ${source?.name ?? ''}, ${source?.picket.picket_from !== null && source?.picket.picket_from !== undefined ? `ПК ${fmtPk(source.picket.picket_from)}` : NO_PICKET_TEXT}; подключение линий не записано${LANDMARK_KIND_HINTS[landmark.kind] ? `. ${LANDMARK_KIND_HINTS[landmark.kind]}` : ''}`}>
                      <path className="mnemo-cube__floor" d={`M${x} ${bottom} L${x + dx} ${bottom - dy} L${x + w + dx} ${bottom - dy} L${x + w} ${bottom} Z`} />
                      <path className="mnemo-cube__side" d={`M${x + w} ${bottom} L${x + w + dx} ${bottom - dy} L${x + w + dx} ${top - dy} L${x + w} ${top} Z`} />
                      <rect className="mnemo-cube__front" x={x} y={top} width={w} height={M.cubeH} />
                      <path className="mnemo-cube__top" d={`M${x} ${top} L${x + dx} ${top - dy} L${x + w + dx} ${top - dy} L${x + w} ${top} Z`} />
                      <IconKind className="mnemo-icon" x={x + 4} y={top + 5} size={16} stroke={1.6} aria-hidden />
                      <text className="mnemo-cube__label" x={landmark.cx + dx / 2} y={top - dy - 6} textAnchor="middle">{landmark.label}</text>
                      <rect className="mnemo-focus" x={x - 4} y={top - dy - 20} width={w + dx + 8} height={M.cubeH + dy + 24} rx={2} />
                      {isSelected(own) ? <rect className="mnemo-ring" x={x - 3} y={top - dy - 3} width={w + dx + 6} height={M.cubeH + dy + 6} rx={2} /> : null}
                    </Hit>
                  );
                })}

                {layout.domain ? (
                  <g className="mnemo-axis" aria-hidden>
                    <line x1={layout.axis.x1} x2={layout.axis.x2} y1={layout.axis.y} y2={layout.axis.y} />
                    {layout.axis.ticks.map((tick) => (
                      <line key={`tick-${tick.pk}`} x1={tick.x} x2={tick.x} y1={layout.axis.y} y2={layout.axis.y + (tick.major ? 8 : 4)} />
                    ))}
                    {layout.axis.ticks.filter((tick) => tick.label).map((tick) => (
                      <text key={`label-${tick.pk}`} className="mnemo-small" x={tick.x} y={layout.axis.y + 22} textAnchor="middle">{tick.label}</text>
                    ))}
                  </g>
                ) : null}

                {layout.rows.map((row) => (row.empty ? (
                  <text key={`empty-${row.kind}`} className="mnemo-small" x={row.empty.x} y={row.empty.y}>линий с пикетом в этой группе нет</text>
                ) : null))}
                <Items items={layout.rows.flatMap((row) => row.items)} selection={selection} onSelect={select} nameOf={nameOf} />
              </svg>
            </div>

            <svg className="mnemo-svg mnemo-svg--column" width={layout.column.width} height={layout.height}
              role="group" aria-label="Линии без пикета в названии канала">
              <rect className="mnemo-unknown" x={0.5} y={layout.column.y + 0.5} width={layout.column.width - 1} height={layout.column.h} rx={4} />
              {layout.column.count ? (
                <Hit label={`Линии без пикета: ${layout.column.count}`} selected={isSelected({ kind: 'unknown' })}
                  onSelect={() => select({ kind: 'unknown' })}>
                  <rect className="mnemo-hit__area" x={0} y={layout.column.y} width={layout.column.width} height={56} />
                  <text className="mnemo-unknown__title" x={layout.column.width / 2} y={layout.column.y + 20} textAnchor="middle">{NO_PICKET_COLUMN}</text>
                  <text className="mnemo-small" x={layout.column.width / 2} y={layout.column.y + 35} textAnchor="middle">в названии канала</text>
                  <text className="mnemo-small" x={layout.column.width / 2} y={layout.column.y + 49} textAnchor="middle">нет ПК</text>
                  <rect className="mnemo-focus" x={4} y={layout.column.y + 4} width={layout.column.width - 8} height={50} rx={2} />
                  {isSelected({ kind: 'unknown' }) ? <rect className="mnemo-ring" x={4} y={layout.column.y + 4} width={layout.column.width - 8} height={50} rx={2} /> : null}
                </Hit>
              ) : (
                <>
                  <text className="mnemo-unknown__title" x={layout.column.width / 2} y={layout.column.y + 20} textAnchor="middle">{NO_PICKET_COLUMN}</text>
                  <text className="mnemo-small" x={layout.column.width / 2} y={layout.rows[0].labelY} textAnchor="middle">таких линий нет</text>
                </>
              )}
              <Items items={layout.rows.flatMap((row) => row.columnItems)} selection={selection} onSelect={select} nameOf={nameOf} />
            </svg>
          </div>
        ) : null}
        {layout?.rows.filter((row) => row.hidden).map((row) => {
          const IconKind = LINE_ICONS[row.kind];
          const info = row.hidden!;
          return (
            <div key={`hidden-${row.kind}`} className="mnemo-hidden-row" style={{ top: row.y, height: row.h }}>
              <button type="button" className="mnemo-hidden-row__show" title="Показать группу"
                onClick={() => onGroupsChange(groupsFromParam(LINE_KINDS.filter((kind) => kind === row.kind || !hidden.includes(kind)).join(',')))}>
                <IconKind size={14} stroke={1.6} aria-hidden /> {LINE_GROUP_LABELS[row.kind]} — скрыто
              </button>
              {info.alarms && info.alarmId ? (
                <button type="button" className="mnemo-hidden-row__alarm" onClick={() => select({ kind: 'alarm', rowUid: info.alarmId as string })}
                  title="Показать группу и выбрать тревожное сообщение">
                  <AlarmMark size={14} /> сообщений за сутки: {info.alarms}
                </button>
              ) : null}
            </div>
          );
        })}
        {/* Правило 2: одна сводная метка тревожных сообщений на группу — под названием группы. */}
        {layout?.rows.filter((row) => !row.hidden && row.alarmSummary).map((row) => {
          const summary = row.alarmSummary!;
          return (
            <button key={`alarms-${row.kind}`} type="button"
              className={`mnemo-group-alarms${summary.active ? '' : ' mnemo-group-alarms--past'}`}
              style={{ top: row.labelY + 3, width: layout.left.width }}
              aria-pressed={isSelected({ kind: 'alarms', group: row.kind })}
              title={`${SMVU_RECORDS_DAY} группы «${LINE_GROUP_LABELS[row.kind]}»: ${summary.records}, линий: ${summary.lines}, ${NO_NORM_AFTER_LABEL}: ${summary.active}`}
              onClick={() => select({ kind: 'alarms', group: row.kind })}>
              <span>сообщений: {summary.records}</span>
              <span>линий: {summary.lines}</span>
            </button>
          );
        })}
        {/* Правило 6: счётчик линий за правым краем прокрутки вместо обрезанных подписей. */}
        {layout && beyond ? (
          <button type="button" className="mnemo-beyond"
            style={{ top: layout.axis.y + 27, right: layout.column.width + 16 }}
            onClick={() => trackRef.current?.scrollBy({ left: layout.track.viewport * 0.8, behavior: 'smooth' })}>
            ещё {beyond.lines} {linesWord(beyond.lines)} правее → {pkRangeText(beyond.from, beyond.to)}
          </button>
        ) : null}
      </div>
      {layout?.track.scrolls ? (
        <p className="mnemo__hint muted small">
          Трасса шире экрана: прокрутка по горизонтали или «Вписать». Мелкие подписи скрыты при плотности — видны при наведении, фокусе и в полосе сведений.
        </p>
      ) : null}

      <div ref={stripRef}>
        {selection ? (
          <InfoStrip scheme={scheme} objectName={objectName} openCard={openCard} selection={selection} backTo={backTo}
            onClose={() => onSelect(null)} onSelect={onSelect} />
        ) : (
          <p className="mnemo__hint muted small">
            Выберите трассу, узел или линию — сведения появятся здесь. Tab — к элементам схемы, Enter — выбрать, Esc — закрыть.
          </p>
        )}
      </div>

      <MnemoLegend />
    </section>
  );
}
