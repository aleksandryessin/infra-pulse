import { useCallback, useMemo } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { getForecast, getJournal, listDecisions, type ForecastJournalEntry } from '../../api/forecast';
import { getScheme } from '../../api/scheme';
import { useForecastState } from '../../providers/ForecastStateProvider';
import { ROUTES } from '../../shared/config/routes';
import { lineNamer, repeatSummary, schemeLineNames } from '../../shared/lib/forecast';
import { endSentence, fmtDateTime, fmtHours, mskIsoDay } from '../../shared/lib/format';
import { useAsync } from '../../shared/lib/use-async';
import { useEscape } from '../../shared/lib/use-escape';
import { StateMessage } from '../../shared/ui/StateMessage';
import { DecisionSummary } from '../../widgets/forecast-card/DecisionSummary';
import { ForecastCardWidget } from '../../widgets/forecast-card/ForecastCardWidget';
import { backLink, cardHref } from '../forecast/back-link';
import { OutcomeCell } from './JournalPage';
import { entryState } from './journal-text';
import './JournalPage.css';

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * Запись журнала по номеру. Отдельного маршрута чтения одной записи в contract нет,
 * поэтому страницы журнала читаются до нужного номера (запрос на contract — в передаче F1).
 */
async function findEntry(position: number, signal: AbortSignal): Promise<ForecastJournalEntry | null> {
  let cursor: string | undefined;
  for (let page = 0; page < 20; page += 1) {
    const result = await getJournal({ limit: 100, cursor }, signal);
    const found = result.items.find((entry) => entry.journal_position === position);
    if (found) return found;
    if (!result.next_cursor) return null;
    cursor = result.next_cursor;
  }
  return null;
}

function SnapshotAside({ entry, currentOpen, names }: { entry: ForecastJournalEntry; currentOpen: boolean; names: Record<string, string> | null }) {
  const { objectName } = useForecastState();
  const outcome = entry.outcome;
  // F-03: названия линий из карточки и схемы объекта; номер канала — только в подсказке.
  const channelName = lineNamer(entry.card.channels, names);
  const releasedAt = entry.released_at ? Date.parse(entry.released_at) : null;
  return (
    <div className="snapshot-aside">
      <h2>Запись журнала</h2>
      <div className="snapshot-aside__block">
        <span className="muted small">Состояние</span>
        <strong>{entryState(entry)}</strong>
      </div>
      <div className="snapshot-aside__block">
        <span className="muted small">Итог</span>
        <strong><OutcomeCell entry={entry} lineName={channelName} /></strong>
        {outcome.lead_hours !== null && outcome.lead_hours !== undefined
          ? <span>Карточка выдана за {fmtHours(outcome.lead_hours)} до события.</span> : null}
        {outcome.event_channel_ids?.length
          ? <span>{endSentence(`Линии события: ${(outcome.event_channel_ids ?? []).map(channelName).join(', ')}`)}</span> : null}
        {outcome.other_events_on_object_count
          ? <span>Ещё событий на объекте по другим линиям: {outcome.other_events_on_object_count}.</span> : null}
        {outcome.intervention_before_window_end
          ? <span>До конца окна было решение с проверкой — отсутствие события не считается ошибкой прогноза.</span> : null}
        <span className="muted small">Итог определяется автоматически по зарегистрированному событию, не вердикт человека.</span>
      </div>
      {entry.events?.length ? (
        <div className="snapshot-aside__block">
          <span className="muted small">События в окне карточки</span>
          <ul>
            {(entry.events ?? []).map((event) => (
              <li key={event.event_id}>
                {fmtDateTime(event.started_at)} · <span title={event.channel_ids.join(', ')}>{[...new Set(event.channel_ids.map(channelName))].join(', ')}</span>
                {releasedAt !== null && Date.parse(event.started_at) === releasedAt
                  ? ' · карточка снята этим событием'
                  : event.while_open ? ' · карточка была открыта; линии не из отслеживаемых карточкой' : ' · после снятия карточки'}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="snapshot-aside__block">
        <span className="muted small">Решение в журнале</span>
        {entry.decision ? <DecisionSummary decision={entry.decision} /> : <span>Решения не было.</span>}
      </div>
      <p>
        <Link to={cardHref(entry.card.id, `${ROUTES.journal}/${entry.journal_position}`)}>
          {currentOpen ? 'Открыть текущую карточку и записать решение' : 'Открыть карточку в текущем состоянии'}
        </Link>
      </p>
      <p className="muted small">Объект: {objectName(entry.card.object_id)}.</p>
    </div>
  );
}

export function JournalSnapshotPage() {
  const { position = '' } = useParams();
  const [params] = useSearchParams();
  const back = backLink(params.get('back'), ROUTES.journal);
  const navigate = useNavigate();
  useEscape(useCallback(() => navigate(back.to), [navigate, back.to]));
  const { objectName, generation } = useForecastState();
  const number = Number(position);
  const entry = useAsync((signal) => findEntry(number, signal), [number, generation], Number.isInteger(number) && number > 0);
  const card = entry.data?.card;
  const current = useAsync((signal) => getForecast(card!.id, signal), [card?.id], Boolean(card));
  const decisions = useAsync((signal) => listDecisions(card!.id, signal), [card?.id], Boolean(card));
  const objectId = card?.object_id ?? null;
  const scheme = useAsync((signal) => getScheme(objectId as string, signal), [objectId], Boolean(objectId));
  const names = schemeLineNames(scheme.data);
  const issuedFrom = card ? mskIsoDay(new Date(Date.parse(card.issued_at) - 30 * DAY_MS)) : '';
  const history = useAsync(
    (signal) => getJournal({ object_id: objectId ?? undefined, issued_from: issuedFrom, limit: 100 }, signal),
    [objectId, issuedFrom],
    Boolean(card && objectId),
  );
  const repeats = useMemo(() => ({
    ...history,
    data: card && history.data ? repeatSummary(card, history.data.items) : null,
  }), [card, history]);

  if (!Number.isInteger(number) || number < 1) {
    return <StateMessage kind="empty" title="Неверный номер записи журнала" action={<Link to={back.to}>{back.label}</Link>} />;
  }
  if (entry.loading) return <StateMessage kind="loading" title="Загрузка записи журнала…" />;
  if (entry.error && !entry.data) {
    return <StateMessage kind="error" title="Запись журнала недоступна" error={entry.error} onRetry={entry.reload} action={<Link to={back.to}>{back.label}</Link>} />;
  }
  if (!entry.data || !card) {
    return <StateMessage kind="empty" title={`Записи № ${number} в журнале нет`} action={<Link to={back.to}>{back.label}</Link>} />;
  }
  return (
    <ForecastCardWidget
      card={card}
      snapshot={entry.data}
      objectName={objectName(card.object_id)}
      isNew={false}
      back={back}
      repeats={repeats}
      decisions={decisions}
      schemeLineNames={names}
      aside={<SnapshotAside entry={entry.data} currentOpen={current.data?.list_state === 'open'} names={names} />}
    />
  );
}
