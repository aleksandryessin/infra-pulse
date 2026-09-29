import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Collapse } from 'antd';
import { IconChevronRight, IconCircleDashed, IconCircleCheck } from '@tabler/icons-react';
import { Link, useNavigate } from 'react-router-dom';
import { getRecentMessages, getSourceAlarmWindow, sourceAsOf, type ObservedMessage } from '../../api/attention';
import { listAllForecasts, listDecisions, type ForecastCardView, type ForecastDecisionList } from '../../api/forecast';
import { useForecastState } from '../../providers/ForecastStateProvider';
import { useNotifications } from '../../providers/NotificationsProvider';
import { FEEDER_KIND_LABELS, decisionLabel } from '../../shared/config/labels';
import { ROUTES } from '../../shared/config/routes';
import { NOTIFICATION_ATTENTION_WINDOW, criticalRule } from '../../shared/config/notifications';
import {
  ALL_DECIDED, FORECAST_BELOW_THRESHOLD, FORECAST_EMPTY, FORECAST_ORDER_NOTE, FORECAST_SUBTITLE, FORECAST_TITLE,
  LINE_NO_NAME, NOW_COUNTS_HINT, NOW_LATEST_TITLE, NOW_NO_SCHEME, NOW_NO_SCHEME_HINT, NOW_OBJECTS_TITLE, NOW_ON_SCHEME_HINT,
  NOW_OPEN_CARD, NOW_TITLE, NO_ALARM_FLAG_NOTE, NO_ALARM_FLAG_ROW, NO_ALARM_FLAG_TITLE, NO_DECISION_OVERDUE_HOURS,
  NO_DECISION_OVERDUE_LABEL, NO_PICKET_TEXT, RELEASED_7D_TITLE, SMVU_RECORD, SMVU_RECORDS_DAY, SOURCE_ALARM_NOTE,
  fillTemplate,
} from '../../shared/config/wording';
import {
  frequencyTexts, isDecisionOverdue, isNewCard, orderForecastRows, picketText, type DecisionMark, type OrderedRow,
} from '../../shared/lib/forecast';
import { fmtHours, fmtShortDateTime, fmtTime } from '../../shared/lib/format';
import { noDataText } from '../../shared/lib/no-data';
import {
  fromMessages, fromServer, nowCountsText, orderNowObjects, schemeQuery, type NowObject, type NowWindow,
} from '../../shared/lib/source-alarms';
import { useAsync } from '../../shared/lib/use-async';
import { subscribeActiveRefresh } from '../../shared/lib/use-active-refresh';
import { AlarmMark, ForecastMark, ReleasedMark } from '../../shared/ui/Marks';
import { RiskLevelMark, RiskLevelSummary } from '../../shared/ui/RiskLevel';
import { StateMessage } from '../../shared/ui/StateMessage';
import './ForecastPage.css';

/** На экране «Сейчас» — объектов и последних сообщений; все объекты окна — по кнопке. */
const NOW_OBJECTS = 6;
const NOW_LATEST = 6;
const DAY_MS = 24 * 60 * 60 * 1000;

type DecisionsResult = Map<string, ForecastDecisionList | null>;

/** Название объекта записи: `object_name` из `/attention` (C0.4), иначе из списка схем. */
function messageObject(message: ObservedMessage, objectName: (id: string | null | undefined) => string): string {
  return message.object_name ?? objectName(message.object_id);
}

/** Признака тревожности в данных нет (CSV Приложения 1 ТЗ): у всех последних записей `alarm` = NULL. */
function hasNoAlarmFlag(messages: ObservedMessage[]): boolean {
  return messages.length > 0 && messages.every((message) => (message.alarm as boolean | null | undefined) == null);
}

/** F-03: группа и название линии вместо номера канала; не линия электропитания — тип датчика; без названия — пикет. */
function lineText(message: ObservedMessage): string {
  if (message.channel_name) {
    const kind = message.feeder_kind ? FEEDER_KIND_LABELS[message.feeder_kind] : null;
    const prefix = kind ? `${kind[0].toUpperCase()}${kind.slice(1)}` : message.sensor_type;
    return prefix ? `${prefix} · ${message.channel_name}` : message.channel_name;
  }
  const picket = picketText({ form: message.picket_form ?? 'unknown', from: message.picket_from, to: message.picket_to });
  return `${message.sensor_type ?? 'тип не указан'} · ${picket === NO_PICKET_TEXT ? LINE_NO_NAME : picket}`;
}

function spanText(item: NowObject): string {
  return item.first_event_at === item.last_event_at
    ? fmtShortDateTime(item.last_event_at)
    : `${fmtShortDateTime(item.first_event_at)}–${fmtTime(item.last_event_at)}`;
}

/** Строка ведёт на схему объекта (F-07), если схема у объекта есть; иначе это не ссылка. */
function NowRow({ query, className, title, children }: {
  query: URLSearchParams | null; className: string; title?: string; children: ReactNode;
}) {
  const navigate = useNavigate();
  return query
    ? <button type="button" className={className} title={title ?? 'Открыть на схеме'} onClick={() => navigate(`${ROUTES.scheme}?${query}`)}>{children}</button>
    : <div className={className} title={title}>{children}</div>;
}

/**
 * «Сейчас» (F-04): тревожные сообщения СМВУ за сутки до среза сообщений. Один запрос: сервер
 * считает всё окно (сообщения, объекты, линии, сообщения на линиях схемы) и отдаёт последние
 * сообщения по времени события — не по времени приёма (обкатка 29.09).
 */
function NowBlock({ openObjects }: { openObjects: ReadonlySet<string> }) {
  const { objectName, state, schemeObjects } = useForecastState();
  const asOf = sourceAsOf(state);
  const [tick, setTick] = useState(0);
  const [allObjects, setAllObjects] = useState(false);
  // Записи СМВУ обновляются сами по себе: блок показывает все объекты и не ждёт пересчёта прогноза.
  useEffect(() => subscribeActiveRefresh(window, document, () => setTick((value) => value + 1)), []);
  // F-04: окно — сутки до среза сообщений СМВУ (`source_messages_as_of`), не вся история.
  const sourceAt = state?.source_messages_as_of ?? null;
  const eventFrom = sourceAt ? new Date(Date.parse(sourceAt) - DAY_MS).toISOString() : null;
  const alarms = useAsync(
    (signal) => getSourceAlarmWindow(eventFrom ?? '', NOW_LATEST, asOf, signal),
    [tick, asOf, eventFrom],
    Boolean(eventFrom),
  );
  const noAlarms = Boolean(alarms.data && !alarms.data.record_count);
  // Пустое окно не показывается молча: есть ли в данных отметка «тревожное» вообще (только в окне).
  const recent = useAsync(
    (signal) => getRecentMessages(NOTIFICATION_ATTENTION_WINDOW, asOf, signal, eventFrom), [tick, asOf, eventFrom], noAlarms,
  );
  const recentMessages = (recent.data?.items ?? []).map((entry) => entry.message);
  const noFlag = noAlarms && Boolean(recent.data) && hasNoAlarmFlag(recentMessages);
  const view: NowWindow | null = noFlag
    ? fromMessages(recentMessages.filter((message) => criticalRule(message) !== null), NOW_LATEST)
    : alarms.data ? fromServer(alarms.data) : null;
  const objects = view ? orderNowObjects(view.objects, openObjects) : [];
  const shown = allObjects ? objects : objects.slice(0, NOW_OBJECTS);
  const title = sourceAt ? fillTemplate(NOW_TITLE, { asOf: fmtShortDateTime(sourceAt) }) : SMVU_RECORDS_DAY;

  return (
    <section className="panel now-block" aria-labelledby="now-title">
      <header className="panel__head">
        <h2 id="now-title">Сейчас · {noFlag ? NO_ALARM_FLAG_TITLE : `${title[0].toLowerCase()}${title.slice(1)}`}</h2>
        <span className="panel__meta" title={sourceAt ? fillTemplate(NOW_COUNTS_HINT, { asOf: fmtShortDateTime(sourceAt) }) : undefined}>
          {view ? nowCountsText(view) : ''}
          {noFlag ? ` · среди последних ${NOTIFICATION_ATTENTION_WINDOW}` : ''}
        </span>
      </header>
      {alarms.loading || (!eventFrom && !alarms.data) ? <div className="panel__body"><StateMessage compact kind="loading" title="Загрузка тревожных сообщений СМВУ…" /></div> : null}
      {alarms.error && !alarms.data ? (
        <div className="panel__body">
          <StateMessage compact kind="error" title="Тревожные сообщения СМВУ недоступны" error={alarms.error} onRetry={alarms.reload}>
            Прогноз ниже от этого не зависит.
          </StateMessage>
        </div>
      ) : null}
      {alarms.data && alarms.error ? (
        <div className="panel__body"><StateMessage compact kind="stale" title="Не удалось обновить сообщения" error={alarms.error}>
          Показаны сообщения на {fmtShortDateTime(alarms.data.as_of)}.
        </StateMessage></div>
      ) : null}
      {noFlag ? <p className="now-block__notice">{NO_ALARM_FLAG_NOTE}</p> : null}
      {alarms.data && noAlarms && !noFlag ? (
        <p className="now-block__empty">
          {recent.loading ? 'Проверка отметки «тревожное» в данных…'
            : `Тревожных сообщений СМВУ за сутки до ${fmtShortDateTime(sourceAt)} нет. Это не значит, что всё в порядке.`}
        </p>
      ) : null}
      {noFlag && !view?.records ? <p className="now-block__empty">Опасных по тексту сообщений за сутки нет среди последних {NOTIFICATION_ATTENTION_WINDOW}. Это не значит, что всё в порядке.</p> : null}
      {view?.records ? (
        <div className="now-grid">
          <div className="now-col">
            <h3 className="now-col__title">{NOW_OBJECTS_TITLE}</h3>
            <ul className="now-list">
              {shown.map((item) => {
                const hasScheme = !schemeObjects || schemeObjects.has(item.object_id);
                const name = item.object_name ?? objectName(item.object_id);
                const counts = `сообщений: ${item.record_count} · линий: ${item.channel_count}`;
                return (
                  <li key={item.object_id}>
                    <NowRow query={schemeQuery(item.last_message, schemeObjects, !noFlag)} className="now-object">
                      <span className="now-object__mark">{openObjects.has(item.object_id) ? <ForecastMark title={NOW_OPEN_CARD} /> : null}</span>
                      <span className="now-object__name" title={name}>{name}</span>
                      <span className="now-object__counts" title={counts}>{counts}</span>
                      <span className="now-object__scheme" title={hasScheme ? NOW_ON_SCHEME_HINT : NOW_NO_SCHEME_HINT}>
                        {!hasScheme ? NOW_NO_SCHEME : item.scheme_record_count == null ? '' : `на схеме: ${item.scheme_record_count}`}
                      </span>
                      {/* Замечание владельца 29.09: время не сжимается, текст сообщения — многоточием в своей
                          ячейке, полный текст — в подсказке. */}
                      <span className="now-object__last" title={`${spanText(item)} · «${item.last_message.value_raw}» · ${lineText(item.last_message)}`}>
                        <span className="now-object__time">{fmtShortDateTime(item.last_event_at)} ·</span>
                        <strong className="now-object__value">«{item.last_message.value_raw}»</strong>
                      </span>
                    </NowRow>
                  </li>
                );
              })}
            </ul>
            {objects.length > NOW_OBJECTS ? (
              <button type="button" className="now-col__more" aria-expanded={allObjects} onClick={() => setAllObjects((value) => !value)}>
                {allObjects ? 'Свернуть' : `Все объекты: ${objects.length}`}
              </button>
            ) : null}
            {view.objectCount > objects.length ? (
              <p className="now-block__empty small">Показаны {objects.length} объектов с последними сообщениями из {view.objectCount}.</p>
            ) : null}
          </div>
          <div className="now-col">
            <h3 className="now-col__title">{NOW_LATEST_TITLE}</h3>
            <ul className="now-list">
              {view.latest.map((message) => (
                <li key={message.row_uid}>
                  <NowRow query={schemeQuery(message, schemeObjects, !noFlag)} className="now-list__row">
                    <AlarmMark title={noFlag ? 'опасное по тексту сообщение' : SMVU_RECORD} />
                    <span className="now-list__time">{fmtShortDateTime(message.event_at)}</span>
                    <span className="now-list__object" title={messageObject(message, objectName)}>{messageObject(message, objectName)}</span>
                    <span className="now-list__channel" title={`${lineText(message)} · ${message.channel_id}`}>{lineText(message)}</span>
                    <strong className="now-list__value" title={`«${message.value_raw}»`}>«{message.value_raw}»{noFlag ? <span className="now-list__kind"> · {NO_ALARM_FLAG_ROW}</span> : null}</strong>
                  </NowRow>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
      <p className="panel__foot">{SOURCE_ALARM_NOTE}</p>
    </section>
  );
}

function DecisionCell({ mark, decisions, loading, overdue }: { mark: DecisionMark; decisions: ForecastDecisionList | null | undefined; loading: boolean; overdue: boolean }) {
  if (mark === 'unknown') return <span className="muted">{loading ? 'решение: загрузка…' : 'решение не загружено'}</span>;
  const latest = decisions?.items[0];
  if (!latest) {
    return (
      <span className={overdue ? 'forecast-row__nodecision forecast-row__nodecision--overdue' : 'forecast-row__nodecision'}>
        <IconCircleDashed size={16} aria-hidden className="icon-inline" /> {overdue ? NO_DECISION_OVERDUE_LABEL : 'Без решения'}
      </span>
    );
  }
  return (
    <span className="forecast-row__decision">
      <IconCircleCheck size={16} aria-hidden className="icon-inline" /> {decisionLabel(latest.decision_code)}
      <span className="muted"> · {fmtShortDateTime(latest.decided_at)}</span>
    </span>
  );
}

export function ForecastPage() {
  const { generation, objectName, state } = useForecastState();
  const { markForecastsSeen } = useNotifications();
  const open = useAsync((signal) => listAllForecasts('open', signal), [generation]);
  // F-04: «Сняты по событию за 7 сут» — не вся история снятий.
  const dataAt = state?.forecast_data_as_of ?? state?.data_as_of ?? null;
  const releasedSince = dataAt ? new Date(Date.parse(dataAt) - 7 * DAY_MS).toISOString() : null;
  const released = useAsync(
    (signal) => listAllForecasts('released', signal, releasedSince ?? undefined), [generation, releasedSince], Boolean(releasedSince),
  );
  const ids = useMemo(() => (open.data?.items ?? []).map((view) => view.card.id), [open.data]);
  // «Сейчас»: объекты с открытой карточкой прогноза — первыми.
  const openObjects = useMemo(() => new Set((open.data?.items ?? [])
    .map((view) => view.card.object_id).filter((id): id is string => Boolean(id))), [open.data]);
  // Список открыт — «новые прогнозы» у пункта меню прочитаны.
  useEffect(() => {
    if (open.data) markForecastsSeen(ids);
  }, [open.data, ids, markForecastsSeen]);
  const decisions = useAsync<DecisionsResult>(async (signal) => {
    const results = await Promise.allSettled(ids.map((id) => listDecisions(id, signal)));
    return new Map(ids.map((id, index) => {
      const result = results[index];
      return [id, result.status === 'fulfilled' ? result.value : null];
    }));
  }, [ids.join('|')], ids.length > 0);

  const rows = useMemo(() => {
    if (!open.data) return [];
    const runs = open.data.runs;
    const base: OrderedRow<ForecastCardView>[] = open.data.items.map((view, place) => {
      const found = decisions.data?.get(view.card.id);
      const mark: DecisionMark = !decisions.data ? 'unknown' : found === null || found === undefined
        ? 'unknown' : found.items.length ? 'decided' : 'none';
      const overdue = mark === 'none' && isDecisionOverdue(view.card.published_at, open.data!.as_of, NO_DECISION_OVERDUE_HOURS);
      return { item: view, place, isNew: isNewCard(view, runs), decision: mark, overdue };
    });
    return orderForecastRows(base);
  }, [open.data, decisions.data]);

  // Решение есть у каждой карточки: диспетчеру — строка «все разобраны»; места ниже порога не показываются.
  const allDecided = rows.length > 0 && rows.every((row) => row.decision === 'decided');
  const horizon = state?.horizons?.[0];
  const maxOpen = open.data?.runs[0]?.list_policy?.max_open;

  return (
    <div className="forecast-page">
      {horizon ? (
        <p className="page-line">
          <span>Последняя выдача <strong>{fmtShortDateTime(horizon.issued_at)}</strong></span>
          <span>новых: <strong>{horizon.new_cards}</strong></span>
          <span>снято по событию с прошлой выдачи: <strong>{horizon.released_since_previous}</strong></span>
        </p>
      ) : null}

      <NowBlock openObjects={openObjects} />

      <section className="panel forecast-list" aria-labelledby="forecast-title">
        <header className="panel__head forecast-list__head">
          <div>
            <h2 id="forecast-title"><ForecastMark title="прогноз" /> {FORECAST_TITLE}</h2>
            <p className="forecast-list__subtitle">{fillTemplate(FORECAST_SUBTITLE, { n: maxOpen ?? 10 })}</p>
          </div>
          <div className="forecast-list__meta">
            <RiskLevelSummary levels={rows.map((row) => row.item.card.risk_level)} />
            <span className="panel__meta">
              {open.data ? `${open.data.items.length} ${maxOpen ? `из ${maxOpen} карточек` : 'карточек'}` : ''}
            </span>
          </div>
        </header>
        {open.loading ? <div className="panel__body"><StateMessage kind="loading" title="Загрузка прогноза…" /></div> : null}
        {open.error && !open.data ? (
          <div className="panel__body">
            <StateMessage kind="error" title="Прогноз недоступен" error={open.error} onRetry={open.reload}>
              Демонстрационные данные вместо ответа сервера не показываются.
            </StateMessage>
          </div>
        ) : null}
        {open.error && open.data ? (
          <div className="panel__body">
            <StateMessage compact kind="stale" title="Список не обновился" error={open.error} onRetry={open.reload}>
              Показан список на {fmtShortDateTime(open.data.as_of)}.
            </StateMessage>
          </div>
        ) : null}
        {decisions.error ? (
          <div className="panel__body"><StateMessage compact kind="error" title="Решения по карточкам не загружены" error={decisions.error} onRetry={decisions.reload} /></div>
        ) : null}
        {open.data && !open.data.items.length ? (
          <div className="panel__body">
            <StateMessage kind="empty" title={noDataText(state?.no_data_from, state?.no_data_to) ?? FORECAST_EMPTY} />
          </div>
        ) : null}
        {allDecided ? (
          <p className="forecast-list__all-decided" role="status">
            <IconCircleCheck size={16} aria-hidden className="icon-inline" /> {fillTemplate(ALL_DECIDED, { n: rows.length })}
          </p>
        ) : null}
        {rows.length ? (
          <ol className="forecast-rows">
            {rows.map(({ item: view, isNew, decision, overdue }) => {
              const card = view.card;
              const frequency = frequencyTexts(card);
              return (
                <li key={card.id}>
                  <Link className="forecast-row" to={`${ROUTES.forecast}/${encodeURIComponent(card.id)}`}>
                    <ForecastMark title="прогноз" />
                    <span className="forecast-row__object">
                      <strong>{objectName(card.object_id)}</strong>
                      {isNew ? <span className="text-label text-label--new">новая</span> : null}
                    </span>
                    <span className="forecast-row__term">
                      {view.day_index && view.days_total ? `день ${view.day_index} из ${view.days_total}` : 'срок'} · до {fmtShortDateTime(card.window_end)}
                    </span>
                    <span className="forecast-row__frequency" title={frequency?.card}><RiskLevelMark level={card.risk_level} /> {frequency ? frequency.list : 'оценка недоступна'}</span>
                    <DecisionCell mark={decision} decisions={decisions.data?.get(card.id)} loading={decisions.loading || decisions.refreshing} overdue={overdue} />
                    <IconChevronRight size={18} aria-hidden className="forecast-row__chevron" />
                  </Link>
                </li>
              );
            })}
          </ol>
        ) : null}
        {open.data ? <p className="panel__foot">Порядок: {FORECAST_ORDER_NOTE}. {FORECAST_BELOW_THRESHOLD}</p> : null}
      </section>

      <Collapse
        className="released-block"
        items={[{
          key: 'released',
          label: (
            <span><ReleasedMark title="снята по событию" /> {RELEASED_7D_TITLE} · {released.data ? released.data.items.length : '…'}</span>
          ),
          children: released.error && !released.data ? (
            <StateMessage compact kind="error" title="Снятые карточки недоступны" error={released.error} onRetry={released.reload} />
          ) : released.data && !released.data.items.length ? (
            <p className="muted">За 7 сут карточек, снятых по событию, нет.</p>
          ) : (
            <ul className="released-rows">
              {(released.data?.items ?? []).map((view) => (
                <li key={view.card.id}>
                  <Link to={`${ROUTES.forecast}/${encodeURIComponent(view.card.id)}`}>
                    <ReleasedMark /> <strong>{objectName(view.card.object_id)}</strong>
                    <span> · событие {fmtShortDateTime(view.released_at)}</span>
                    <span className="muted"> · карточка выдана за {fmtHours((Date.parse(view.released_at ?? view.card.issued_at) - Date.parse(view.card.issued_at)) / 3600000)} до события</span>
                  </Link>
                </li>
              ))}
            </ul>
          ),
        }]}
      />
    </div>
  );
}
