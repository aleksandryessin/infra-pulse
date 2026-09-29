import { useMemo, useState } from 'react';
import { Select } from 'antd';
import { IconChevronDown, IconChevronUp } from '@tabler/icons-react';
import type { ObjectSchemeSummary } from '../../api/scheme';
import { SCHEME_MAP_NOTE, SCHEME_ORDER_NOTE, SCHEME_PICKER_TITLE } from '../../shared/config/wording';
import { fmtShortDate } from '../../shared/lib/format';
import { riskLevelText } from '../../shared/lib/risk-level';
import { AlarmMark, ForecastMark, ReleasedMark } from '../../shared/ui/Marks';
import { RiskLevelMark } from '../../shared/ui/RiskLevel';
import type { AttentionEntry } from './attention-order';
import './ObjectPicker.css';

function meta(entry: AttentionEntry): string {
  const day = entry.forecast?.day_index && entry.forecast.days_total ? `д. ${entry.forecast.day_index} из ${entry.forecast.days_total}` : null;
  // F-01: число — тревожные сообщения за сутки, не действующие тревоги.
  if (entry.group === 'alarm') return [`сообщений: ${entry.alarms}`, day].filter(Boolean).join(' · ');
  if (entry.group === 'forecast') return day ?? 'прогноз';
  if (entry.group === 'released') return entry.released?.released_at ? `снята ${fmtShortDate(entry.released.released_at)}` : 'снята';
  return '';
}

function EntryRow({ entry, active, onSelect }: { entry: AttentionEntry; active: boolean; onSelect: (id: string) => void }) {
  const level = entry.forecast?.risk_level ?? null;
  const label = [entry.name, meta(entry), level ? riskLevelText(level) : null].filter(Boolean).join(', ');
  return (
    <li>
      <button type="button" className={`picker-entry${active ? ' is-active' : ''}`} aria-current={active ? 'true' : undefined}
        aria-label={label} onClick={() => onSelect(entry.objectId)}>
        <span className="picker-entry__marks" aria-hidden>
          {entry.alarms ? <AlarmMark size={12} /> : <span className="picker-entry__slot" />}
          {entry.forecast ? <ForecastMark size={12} /> : entry.group === 'released' ? <ReleasedMark size={12} /> : <span className="picker-entry__slot" />}
        </span>
        <span className="picker-entry__name">{entry.name}</span>
        {meta(entry) || level ? (
          <span className="picker-entry__meta">
            {meta(entry)}
            {level ? <RiskLevelMark level={level} size="small" /> : null}
          </span>
        ) : null}
      </button>
    </li>
  );
}

/**
 * Левая панель схемы — обзор всех объектов (ТЗ §10 «интерактивная карта» без координат):
 * выбор объекта, счётчики, «Требуют внимания» и «Все объекты». Прогнозов не больше 10, поэтому
 * они показываются все; «Все объекты» раскрыты по умолчанию (повторная проверка аудита 29.09).
 * Порядок списка — порядок просмотра, не тяжесть.
 */
export function ObjectPicker({
  summaries, entries, selectedId, onSelect,
}: {
  summaries: ObjectSchemeSummary[];
  entries: AttentionEntry[];
  selectedId: string | null;
  onSelect: (objectId: string) => void;
}) {
  const [allObjects, setAllObjects] = useState(true);

  const counts = useMemo(() => ({
    total: summaries.length,
    forecast: summaries.filter((item) => item.open_cards > 0).length,
    alarm: summaries.filter((item) => item.current_alarms > 0).length,
    released: summaries.filter((item) => item.released_7d > 0).length,
  }), [summaries]);

  const alarms = entries.filter((entry) => entry.group === 'alarm');
  const forecasts = entries.filter((entry) => entry.group === 'forecast');
  const released = entries.filter((entry) => entry.group === 'released');
  const quiet = entries.filter((entry) => entry.group === 'none');
  const options = entries
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name, 'ru', { numeric: true }))
    .map((entry) => ({ value: entry.objectId, label: entry.name }));

  return (
    <aside className="panel object-picker" aria-labelledby="picker-title">
      <h2 id="picker-title">{SCHEME_PICKER_TITLE}</h2>
      <label className="object-picker__field">
        <span className="muted small">Объект</span>
        <Select<string>
          showSearch
          optionFilterProp="label"
          value={selectedId ?? undefined}
          placeholder="Выберите объект"
          options={options}
          onChange={(value) => onSelect(value)}
          aria-label="Объект"
          className="object-picker__select"
        />
      </label>

      <dl className="object-picker__counts">
        <div><dt><span className="object-picker__slot" aria-hidden />Объектов на схеме</dt><dd>{counts.total}</dd></div>
        <div><dt><ForecastMark size={13} />С открытым прогнозом</dt><dd>{counts.forecast}</dd></div>
        <div><dt><AlarmMark size={13} />С тревожными сообщениями за сутки</dt><dd>{counts.alarm}</dd></div>
        <div><dt><ReleasedMark size={13} />Снято по событию за 7 сут</dt><dd>{counts.released}</dd></div>
      </dl>
      <p className="object-picker__map-note muted small">{SCHEME_MAP_NOTE}</p>

      <section className="object-picker__attention" aria-labelledby="attention-title">
        <h3 id="attention-title">Требуют внимания</h3>
        {alarms.length + forecasts.length + released.length === 0 ? (
          <p className="muted small">Тревожных сообщений, прогнозов и снятых карточек нет — это не отсутствие риска.</p>
        ) : null}
        <ul className="picker-list">
          {[...alarms, ...forecasts].map((entry) => (
            <EntryRow key={entry.objectId} entry={entry} active={entry.objectId === selectedId} onSelect={onSelect} />
          ))}
        </ul>
        {released.length ? (
          <ul className="picker-list">
            {released.map((entry) => <EntryRow key={entry.objectId} entry={entry} active={entry.objectId === selectedId} onSelect={onSelect} />)}
          </ul>
        ) : null}
        {quiet.length ? (
          <>
            <button type="button" className="link-button picker-more" onClick={() => setAllObjects((value) => !value)} aria-expanded={allObjects}>
              {allObjects ? <IconChevronUp size={14} aria-hidden /> : <IconChevronDown size={14} aria-hidden />}
              {' '}Все объекты ({counts.total})
            </button>
            {allObjects ? (
              <ul className="picker-list">
                {quiet.map((entry) => <EntryRow key={entry.objectId} entry={entry} active={entry.objectId === selectedId} onSelect={onSelect} />)}
              </ul>
            ) : null}
          </>
        ) : null}
      </section>

      <p className="object-picker__note muted small">{SCHEME_ORDER_NOTE}</p>
    </aside>
  );
}
