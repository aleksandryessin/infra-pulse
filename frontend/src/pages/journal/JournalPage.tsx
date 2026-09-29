import { useMemo, useState } from 'react';
import { Button, Segmented } from 'antd';
import { IconChevronRight, IconX } from '@tabler/icons-react';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import {
  getJournal, getJournalCounts, type ForecastJournalEntry, type ForecastJournalList, type ListState,
} from '../../api/forecast';
import { useForecastState } from '../../providers/ForecastStateProvider';
import { ABSTENTION_LABELS, CHECK_RESULT_LABELS, decisionLabel } from '../../shared/config/labels';
import { ROLE_LABELS } from '../../shared/config/permissions';
import { ROUTES } from '../../shared/config/routes';
import {
  DEMO_MARK, DEMO_MARK_HINT, JOURNAL_ABSTAINED_NOTE, LINE_ON_SCHEME, RECENT_EVENT_HINT, RECENT_EVENT_LABEL, actorText,
} from '../../shared/config/wording';
import { fmtIsoDay, fmtShortDateTime, mskIsoDay } from '../../shared/lib/format';
import { useAsync } from '../../shared/lib/use-async';
import { RuDateInput } from '../../shared/ui/RuDateInput';
import { StateMessage } from '../../shared/ui/StateMessage';
import { ForecastMark, ReleasedMark } from '../../shared/ui/Marks';
import { REALIZED_HINT, entryLead, entryOutcome, entryResult, isAbstained } from './journal-text';
import './JournalPage.css';

const STATES: { value: 'all' | ListState; label: string }[] = [
  { value: 'all', label: 'Все' },
  { value: 'open', label: 'Открыта' },
  { value: 'released', label: 'Сбылась' },
  { value: 'expired', label: 'Срок истёк' },
];

/** Фильтр по решению (C0.4, `decision_state`): найти карточки с решением или без него. */
const DECISIONS: { value: 'all' | 'any' | 'none'; label: string }[] = [
  { value: 'all', label: 'Все' },
  { value: 'any', label: 'С решением' },
  { value: 'none', label: 'Без решения' },
];

function Counters() {
  const { generation } = useForecastState();
  const counts = useAsync((signal) => getJournalCounts(signal), [generation]);
  if (counts.loading) return <StateMessage compact kind="loading" title="Загрузка счётчиков…" />;
  if (!counts.data) return <StateMessage compact kind="error" title="Счётчики журнала недоступны" error={counts.error} onRetry={counts.reload} />;
  const rows = counts.data.rows;
  if (!rows.length) return <p className="journal-counters muted">Карточек в журнале пока нет.</p>;
  const sum = (key: 'cards_issued' | 'cards_released' | 'cards_no_event' | 'cards_unknown' | 'cards_open') =>
    rows.reduce((total, row) => total + row[key], 0);
  const from = rows.map((row) => row.period_start).sort()[0];
  // F-08: конец периода — по данным: последний день недели (period_end не включается), но не позже среза.
  const lastEnd = rows.map((row) => row.period_end).sort().at(-1);
  const weekEnd = lastEnd ? mskIsoDay(new Date(Date.parse(`${lastEnd}T12:00:00+03:00`) - 24 * 3600 * 1000)) : '';
  const asOfDay = mskIsoDay(counts.data.as_of);
  const to = weekEnd && asOfDay && asOfDay < weekEnd ? asOfDay : weekEnd || asOfDay;
  return (
    <p className="journal-counters" aria-label="Счётчики журнала" title={JOURNAL_ABSTAINED_NOTE}>
      <span className="muted">Выдано с {fmtIsoDay(from)} по {fmtIsoDay(to)}:</span>
      <span>выдано <strong>{sum('cards_issued')}</strong></span>
      <span title={REALIZED_HINT}>сбылась <strong>{sum('cards_released')}</strong></span>
      <span>истекла без события <strong>{sum('cards_no_event')}</strong></span>
      <span>исход неизвестен <strong>{sum('cards_unknown')}</strong></span>
      <span>открыто <strong>{sum('cards_open')}</strong></span>
      <span className="muted small">{JOURNAL_ABSTAINED_NOTE}</span>
    </p>
  );
}

export function OutcomeCell({ entry, lineName }: { entry: ForecastJournalEntry; lineName?: (id: string) => string }) {
  const outcome = entryOutcome(entry, lineName);
  return (
    <span className="outcome-cell">
      <span>{outcome.label}</span>
      {outcome.detail ? <span className="muted small">{outcome.detail}</span> : null}
      {outcome.lineHref ? (
        <Link className="small" to={outcome.lineHref} onClick={(event) => event.stopPropagation()}>{LINE_ON_SCHEME}</Link>
      ) : null}
    </span>
  );
}

function DecisionCell({ entry }: { entry: ForecastJournalEntry }) {
  const decision = entry.decision;
  if (!decision) return <span className="cell-main muted">нет решения</span>;
  const role = ROLE_LABELS[decision.actor_role as keyof typeof ROLE_LABELS] ?? decision.actor_role;
  return (
    <>
      <span className="cell-main">{decisionLabel(decision.decision_code)}</span>
      <span className="cell-sub">{actorText(decision.actor_id, decision.simulated)} ({role}), {fmtShortDateTime(decision.decided_at)}</span>
    </>
  );
}

/** «Итог»: плашка-статус и одна серая строка с датой; красного в журнале нет. */
function ResultCell({ entry }: { entry: ForecastJournalEntry }) {
  const result = entryResult(entry);
  return (
    <>
      <span className={`status-chip status-chip--${result.tone}`} title={result.hint ?? undefined}>
        {result.tone === 'open' ? <ForecastMark size={12} /> : result.tone === 'released' ? <ReleasedMark size={12} /> : null}
        {result.chip}
      </span>
      {result.line ? <span className="cell-sub">{result.line}</span> : null}
      {result.lineHref ? (
        <Link className="cell-sub" to={result.lineHref} onClick={(event) => event.stopPropagation()}>{LINE_ON_SCHEME}</Link>
      ) : null}
    </>
  );
}

/** «Результат проверки» (C0.4): последняя ревизия на момент журнала. */
function CheckCell({ entry }: { entry: ForecastJournalEntry }) {
  const result = entry.check_result;
  if (!result) return <span className="cell-main muted">{entry.decision ? 'ждём результат' : '—'}</span>;
  return (
    <>
      <span className="cell-main">{CHECK_RESULT_LABELS[result.check_result] ?? result.check_result}</span>
      <span className="cell-sub">
        {fmtShortDateTime(result.result_at)}{result.simulated ? <> · <span title={DEMO_MARK_HINT}>{DEMO_MARK}</span></> : null}
      </span>
    </>
  );
}

export function JournalPage() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { generation, objectName, state } = useForecastState();
  const listState = (['open', 'released', 'expired'] as const).find((value) => value === params.get('state'));
  const issuedFrom = params.get('from') ?? '';
  const issuedTo = params.get('to') ?? '';
  const objectId = params.get('object') ?? '';
  // C0.4: решение (`?decision=any|none` → decision_state) и «исход неизвестен» (outcome=unknown).
  const decisionState = (['any', 'none'] as const).find((value) => value === params.get('decision'));
  const unknownOutcome = params.get('outcome') === 'unknown';
  const [more, setMore] = useState<ForecastJournalList['items']>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [moreError, setMoreError] = useState<unknown>(null);
  const rangeInvalid = Boolean(issuedFrom && issuedTo && issuedFrom > issuedTo);
  const filters = {
    list_state: listState, issued_from: issuedFrom || undefined, issued_to: issuedTo || undefined,
    object_id: objectId || undefined, decision_state: decisionState,
    outcome: unknownOutcome ? 'unknown' as const : undefined,
  };

  const journal = useAsync(async (signal) => {
    const page = await getJournal({ ...filters, limit: 50 }, signal);
    setMore([]);
    setCursor(page.next_cursor ?? null);
    return page;
  }, [listState, issuedFrom, issuedTo, objectId, decisionState, unknownOutcome, generation], !rangeInvalid);

  const all = useMemo(() => [...(journal.data?.items ?? []), ...more], [journal.data, more]);
  // «Прогноз не выдан» — не карточка: без номера в таблице, отдельным примечанием.
  const items = useMemo(() => all.filter((entry) => !isAbstained(entry)), [all]);
  const abstained = useMemo(() => all.filter(isAbstained), [all]);

  const patch = (key: string, value: string | null) => setParams((current) => {
    const next = new URLSearchParams(current);
    if (value) next.set(key, value);
    else next.delete(key);
    return next;
  }, { replace: true });

  const loadMore = async () => {
    if (!cursor) return;
    try {
      const page = await getJournal({ ...filters, cursor, limit: 50 });
      setMore((current) => [...current, ...page.items]);
      setCursor(page.next_cursor ?? null);
      setMoreError(null);
    } catch (error) {
      setMoreError(error);
    }
  };

  // Календарь пустого поля открывается на месяце среза данных прогноза, а не на сегодняшнем.
  const dataDay = mskIsoDay(state?.forecast_data_as_of ?? state?.data_as_of ?? '') || null;

  const back = `${location.pathname}${location.search}`;
  const open = (entry: ForecastJournalEntry) =>
    navigate(`${ROUTES.journal}/${entry.journal_position}?back=${encodeURIComponent(back)}`);

  return (
    <div className="journal-page">
      <Counters />

      <div className="journal-filters" role="group" aria-label="Фильтры журнала">
        <Segmented value={listState ?? 'all'} options={STATES}
          onChange={(value) => patch('state', value === 'all' ? null : String(value))} />
        <span className="journal-filters__group" role="group" aria-label="Решение по карточке">
          <span className="journal-filters__label" aria-hidden>Решение</span>
          <Segmented value={decisionState ?? 'all'} options={DECISIONS}
            onChange={(value) => patch('decision', value === 'all' ? null : String(value))} />
        </span>
        <Button aria-pressed={unknownOutcome} type={unknownOutcome ? 'primary' : 'default'} ghost={unknownOutcome}
          onClick={() => patch('outcome', unknownOutcome ? null : 'unknown')}>Исход неизвестен</Button>
        {/* Период выдачи переносится целиком: «с» и «по» на одной строке. */}
        <span className="journal-filters__dates">
          <span className="journal-filters__date">
            <label htmlFor="journal-issued-from">Выдано с</label>
            <RuDateInput id="journal-issued-from" value={issuedFrom} onChange={(value) => patch('from', value)} label="Выдано с"
              defaultMonth={issuedTo || dataDay} />
          </span>
          <span className="journal-filters__date">
            <label htmlFor="journal-issued-to">по</label>
            <RuDateInput id="journal-issued-to" value={issuedTo} onChange={(value) => patch('to', value)} label="Выдано по"
              defaultMonth={issuedFrom || dataDay} />
          </span>
        </span>
        {objectId ? (
          <Button icon={<IconX size={14} />} onClick={() => patch('object', null)}>Объект: {objectName(objectId)}</Button>
        ) : null}
        {issuedFrom || issuedTo || listState || decisionState || unknownOutcome ? (
          <Button type="link" onClick={() => setParams(objectId ? { object: objectId } : {}, { replace: true })}>Сбросить фильтры</Button>
        ) : null}
      </div>

      {rangeInvalid ? <StateMessage compact kind="error" title="Дата «с» позже даты «по»" /> : null}
      {journal.loading ? <StateMessage kind="loading" title="Загрузка журнала…" /> : null}
      {journal.error && !journal.data ? (
        <StateMessage kind="error" title="Журнал недоступен" error={journal.error} onRetry={journal.reload}>
          Демонстрационные записи вместо ответа сервера не показываются.
        </StateMessage>
      ) : null}
      {journal.error && journal.data ? (
        <StateMessage compact kind="stale" title="Журнал не обновился" error={journal.error} onRetry={journal.reload}>
          Показаны записи на {fmtShortDateTime(journal.data.as_of)}.
        </StateMessage>
      ) : null}

      {journal.data ? (
        <section className="panel" aria-labelledby="journal-title">
          <header className="panel__head">
            <h2 id="journal-title">Карточки прогноза</h2>
            <span className="panel__meta">карточек: {items.length}{cursor ? ' (есть ещё)' : ''} · строка открывает снимок карточки на момент выдачи</span>
          </header>
          {items.length ? (
            <table className="journal-table">
              <colgroup>
                <col className="journal-col--issued" />
                <col className="journal-col--object" />
                <col className="journal-col--wide" />
                <col className="journal-col--check" />
                <col className="journal-col--wide" />
                <col className="journal-col--lead" />
                <col className="journal-col--view" />
              </colgroup>
              <thead>
                <tr>
                  <th scope="col">Выдана / срок</th>
                  <th scope="col">Объект</th>
                  <th scope="col">Решение и автор</th>
                  <th scope="col">Результат проверки</th>
                  <th scope="col">Итог</th>
                  <th scope="col">Запас до события</th>
                  <th scope="col">Просмотр</th>
                </tr>
              </thead>
              <tbody>
                {items.map((entry) => {
                  const href = `${ROUTES.journal}/${entry.journal_position}?back=${encodeURIComponent(back)}`;
                  return (
                    <tr key={entry.journal_position} onClick={() => open(entry)}>
                      <td>
                        {/* Номер и время выдачи переносятся целиком, без наложения на «Объект» (QA 29.09, D1). */}
                        <span className="cell-main">
                          <span className="journal-issued__part">№ {entry.journal_position} ·</span>{' '}
                          <span className="journal-issued__part">{fmtShortDateTime(entry.card.issued_at)}</span>
                        </span>
                        <span className="cell-sub">срок до {fmtShortDateTime(entry.card.window_end)}</span>
                      </td>
                      <td>
                        <span className="cell-main">{objectName(entry.card.object_id)}</span>
                        {entry.card.recurrence === 'chronic'
                          ? <span className="cell-sub" title={RECENT_EVENT_HINT}>{RECENT_EVENT_LABEL}</span> : null}
                      </td>
                      <td><DecisionCell entry={entry} /></td>
                      <td><CheckCell entry={entry} /></td>
                      <td><ResultCell entry={entry} /></td>
                      <td><span className="cell-main">{entryLead(entry)}</span></td>
                      <td>
                        <Link className="journal-view" to={href} onClick={(event) => event.stopPropagation()}>
                          Карточка <IconChevronRight size={16} aria-hidden />
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="panel__body"><StateMessage kind="empty" title="Записей по фильтру нет" /></div>
          )}
          {abstained.length ? (
            <p className="panel__foot journal-abstained">
              Прогноз не выдан (не карточка, в счётчиках не учитывается):{' '}
              {abstained.map((entry) => `${objectName(entry.card.object_id)}, ${fmtShortDateTime(entry.card.issued_at)}${entry.card.abstention_reason ? ` — ${ABSTENTION_LABELS[entry.card.abstention_reason] ?? entry.card.abstention_reason}` : ''}`).join('; ')}.
            </p>
          ) : null}
          {cursor ? (
            <div className="panel__body">
              <Button onClick={() => { void loadMore(); }}>Показать ещё</Button>
              {moreError ? <StateMessage compact kind="error" title="Следующая страница не загружена" error={moreError} /> : null}
            </div>
          ) : null}
        </section>
      ) : null}
      <p className="muted small">
        Итог — автоматически зарегистрированное событие по тому же определению, что и прогноз; это не вердикт человека.
        «Сбылась» — карточка снята при начале события. Записи журнала не изменяются задним числом.
      </p>
    </div>
  );
}
