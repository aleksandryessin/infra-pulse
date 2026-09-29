import type { ReactNode } from 'react';
import { Collapse } from 'antd';
import { IconArrowLeft, IconChartBar, IconChecklist } from '@tabler/icons-react';
import { Link } from 'react-router-dom';
import type {
  ForecastCard, ForecastCardView, ForecastChannel, ForecastDecisionList, ForecastJournalEntry,
} from '../../api/forecast';
import {
  COVERAGE_LABELS, FEEDER_KIND_LABELS, LIST_STATE_LABELS, OUTCOME_LABELS, PICKET_BASIS_LABELS, decisionLabel, scorerLabel,
} from '../../shared/config/labels';
import { ROUTES } from '../../shared/config/routes';
import {
  CARD_CLOSES, FACTS_NOT_CAUSE, FORECAST_TARGET, FREQUENCY_CALIBRATION_NOTE, FREQUENCY_ROW_LABEL, LINE_LOSSES_365_LABEL,
  LISTED_LINES_LAST_LOSS_LABEL, OBJECT_EVENTS_365_LABEL, RECENT_EVENT_HINT, RECENT_EVENT_LABEL, RECOMMENDATION_UNCONFIRMED, SMVU_ALARM,
} from '../../shared/config/wording';
import {
  channelEpisodes, channelLastEpisode, channelsPicketSpan, episodeFacts, frequencyTexts, lineNamer, linesFact,
  listedLinesFoot, ordinalText,
  picketText, ruQuotes, type RepeatSummary,
} from '../../shared/lib/forecast';
import { endSentence, fmtDate, fmtDateTime, fmtIsoDay, fmtShortDate, fmtTime, plural } from '../../shared/lib/format';
import type { AsyncState } from '../../shared/lib/use-async';
import { ForecastMark, ReleasedMark } from '../../shared/ui/Marks';
import { RiskLevelMark } from '../../shared/ui/RiskLevel';
import { StateMessage } from '../../shared/ui/StateMessage';
import { DecisionSummary } from './DecisionSummary';
import { draftTextFor } from './draft-texts';
import './ForecastCard.css';

function channelPicket(channel: ForecastChannel): string {
  return picketText({ form: channel.picket_form, from: channel.picket_from, to: channel.picket_to });
}

function lineShort(channel: ForecastChannel): string {
  return channel.channel_name ?? channel.channel_id;
}

/** Название инцидента: для линий электропитания — формулировка C0.3, иначе подпись из API. */
function targetTitle(card: ForecastCard): string {
  if (card.target_spec_id === 'phase-feeders/phase_loss_all') return FORECAST_TARGET;
  return card.target_label ? `${card.target_label[0].toUpperCase()}${card.target_label.slice(1)}` : card.target_spec_id;
}

function Factor({ label, value, note }: { label: string; value: ReactNode; note: ReactNode }) {
  return (
    <div className="card-factor">
      <span className="card-factor__label">{label}</span>
      <strong className="card-factor__value">{value}</strong>
      <span className="card-factor__note">{note}</span>
    </div>
  );
}

/** «Факторы для проверки»: три наблюдаемых факта из `facts` карточки, крупным значением. */
function Factors({ card, linesTotal }: { card: ForecastCard; linesTotal: number | null }) {
  const facts = episodeFacts(card);
  const lines = linesFact(card, linesTotal);
  const top = (card.channels ?? []).slice(0, 2);
  const linesNote = [
    lines?.note ?? null,
    top.length ? `чаще всего: ${top.map(lineShort).join(', ')}` : null,
  ].filter(Boolean).join(' · ');
  return (
    <section className="card-factors" aria-labelledby="card-factors-title">
      <h3 id="card-factors-title"><IconChartBar size={18} aria-hidden className="card-factors__icon" /> Факторы для проверки</h3>
      <div className="card-factors__grid">
        <Factor label={LISTED_LINES_LAST_LOSS_LABEL}
          value={facts.lastEpisodeAt ? fmtShortDate(facts.lastEpisodeAt) : '—'}
          note={facts.lastEpisodeAt ? `${fmtTime(facts.lastEpisodeAt)} · начало` : 'в карточке не указана'} />
        <Factor label={OBJECT_EVENTS_365_LABEL}
          value={facts.events365 ?? '—'}
          note={facts.events365 !== null ? 'событие — потеря связи одной или нескольких линий' : 'число не указано'} />
        <Factor label={lines?.label ?? 'Линий под наблюдением карточки'}
          value={lines?.value ?? '—'}
          note={linesNote || 'линии не перечислены'} />
      </div>
      <p className="card-factors__foot">
        {FACTS_NOT_CAUSE}
        {listedLinesFoot(lines)}
      </p>
    </section>
  );
}

function StateChips({ view, snapshot, isNew, chronic }: { view?: ForecastCardView; snapshot?: ForecastJournalEntry; isNew: boolean; chronic: boolean }) {
  const state = snapshot ? 'снимок на момент выдачи' : view?.list_state ? LIST_STATE_LABELS[view.list_state] ?? view.list_state : null;
  return (
    <div className="forecast-card__chips">
      {isNew ? <span className="text-label text-label--new">новая</span> : null}
      {state ? (
        <span className={`text-label${view?.list_state === 'open' && !snapshot ? ' text-label--forecast' : ''}`}>
          {view?.list_state === 'released' && !snapshot ? <ReleasedMark size={12} /> : <ForecastMark size={12} />}
          {state}
        </span>
      ) : null}
      {chronic ? <span className="text-label" title={RECENT_EVENT_HINT}>{RECENT_EVENT_LABEL}</span> : null}
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="card-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function TermRow({ card, view, snapshot }: { card: ForecastCard; view?: ForecastCardView; snapshot?: ForecastJournalEntry }) {
  if (snapshot) {
    return <>окно 14 сут: {fmtDateTime(card.window_start)} — {fmtDateTime(card.window_end)} · {CARD_CLOSES}</>;
  }
  if (view?.list_state === 'released') {
    return <>снята по событию {fmtDateTime(view.released_at)} · окно было до {fmtDateTime(card.window_end)}</>;
  }
  if (view?.list_state === 'expired') {
    return <>срок истёк {fmtDateTime(card.window_end)} · итог — в журнале</>;
  }
  return (
    <>
      до {fmtDateTime(card.window_end)}
      {view?.day_index && view.days_total ? ` · день ${view.day_index} из ${view.days_total}` : ''}
      {` · ${CARD_CLOSES}`}
    </>
  );
}

function RepeatsRow({ repeats }: { repeats: AsyncState<RepeatSummary> }) {
  if (repeats.loading || (repeats.refreshing && !repeats.data)) return <span className="muted">загрузка истории объекта…</span>;
  if (repeats.error && !repeats.data) return <span className="muted">история объекта недоступна: журнал не ответил</span>;
  if (!repeats.data) return <span className="muted">—</span>;
  const { ordinal, previous } = repeats.data;
  if (!previous) return <>{ordinalText(ordinal)}; прошлых карточек за 30 сут нет</>;
  const outcome = OUTCOME_LABELS[previous.outcome.status] ?? previous.outcome.status;
  return (
    <>
      {ordinalText(ordinal)}; прошлая выдана {fmtShortDate(previous.card.issued_at)}: {previous.decision
        ? `решение «${decisionLabel(previous.decision.decision_code)}»` : 'решения не было'}, итог — {outcome}
    </>
  );
}

export interface ForecastCardWidgetProps {
  card: ForecastCard;
  view?: ForecastCardView;
  snapshot?: ForecastJournalEntry;
  objectName: string;
  isNew: boolean;
  back: { to: string; label: string };
  repeats: AsyncState<RepeatSummary>;
  decisions?: AsyncState<ForecastDecisionList>;
  /** Сведения о событии снятой карточки из журнала (линии и время). */
  releaseEntry?: ForecastJournalEntry | null;
  /** Блок «За сутки на объекте» — тревожные сообщения СМВУ по линиям объекта за 24 ч (F-01). */
  nowOnObject?: ReactNode;
  /** Все линии объекта на схеме (`/schemes/{object}`); null — схема не загружена. */
  linesTotal?: number | null;
  /** Названия линий схемы объекта по номеру канала — вместо номеров (F-03). */
  schemeLineNames?: Record<string, string> | null;
  aside: ReactNode;
}

/**
 * Широкая карточка C4: один виджет для Прогноза, Схемы и Журнала.
 * Пять строк → рекомендация → «Подробнее» (свёрнуто); справа решение.
 */
export function ForecastCardWidget({
  card, view, snapshot, objectName, isNew, back, repeats, decisions, releaseEntry, nowOnObject, linesTotal, schemeLineNames,
  aside,
}: ForecastCardWidgetProps) {
  const lineName = lineNamer(card.channels, schemeLineNames ?? null);
  const frequency = frequencyTexts(card);
  const facts = episodeFacts(card);
  const channels = card.channels ?? [];
  const total = card.channels_total ?? channels.length;
  const picketSpan = channelsPicketSpan(channels);
  const recommendation = view?.recommendation ?? null;
  const released = view?.list_state === 'released';

  return (
    <article className="forecast-card" aria-label={`Карточка прогноза: ${objectName}`}>
      <div className="forecast-card__toolbar">
        <Link to={back.to} className="forecast-card__back"><IconArrowLeft size={16} aria-hidden /> {back.label}</Link>
        {snapshot ? <span className="muted">Снимок карточки на момент выдачи · запись журнала № {snapshot.journal_position}</span> : null}
      </div>
      <div className="forecast-card__grid">
        <section className="forecast-card__main">
          <header className="forecast-card__heading">
            <div>
              <h2><ForecastMark size={16} title="прогноз" /> {objectName}</h2>
              <p className="forecast-card__subline">
                {targetTitle(card)} · прогноз на 14 сут
                {picketSpan ? <> · <span title="пикеты линий карточки, по названию канала">{picketSpan}</span></> : null}
                {card.object_id ? <> · <Link to={`${ROUTES.scheme}?object=${encodeURIComponent(card.object_id)}`}>На схеме →</Link></> : null}
              </p>
            </div>
            <StateChips view={view} snapshot={snapshot} isNew={isNew} chronic={card.recurrence === 'chronic'} />
          </header>

          {released ? (
            <div className="forecast-card__banner" role="status">
              <ReleasedMark size={16} title="снята по событию" />
              <span>
                Событие зарегистрировано {fmtDateTime(view?.released_at)} — карточка снята по событию.
                {releaseEntry?.outcome.event_channel_ids?.length
                  ? ` ${endSentence(`Линии события: ${releaseEntry.outcome.event_channel_ids.map(lineName).join(', ')}`)}`
                  : ''}
                {releaseEntry ? <> <Link to={`/journal/${releaseEntry.journal_position}`}>Запись журнала № {releaseEntry.journal_position}</Link></> : null}
              </span>
            </div>
          ) : null}

          {nowOnObject}

          <dl className="card-rows">
            <Row label="Срок">
              <span className="card-row__line"><TermRow card={card} view={view} snapshot={snapshot} /></span>
            </Row>
            <Row label={FREQUENCY_ROW_LABEL}>
              {frequency ? (
                <>
                  <span className="card-row__line">
                    <strong className="card-row__big">{frequency.big}</strong> <span>{frequency.detail}</span>
                    {/* ТЗ §10: уровень риска цветом, знаком и текстом — «этого уровня» рядом с ним. */}
                    <RiskLevelMark level={card.risk_level} variant="full" />
                  </span>
                  <span className="card-row__note">{frequency.note}</span>
                </>
              ) : (
                <span className="card-row__line">
                  <span className="muted">оценка недоступна: мало истории</span>
                  <RiskLevelMark level={card.risk_level} variant="full" />
                </span>
              )}
            </Row>
          </dl>

          <Factors card={card} linesTotal={linesTotal ?? null} />

          <dl className="card-rows card-rows--after">
            <Row label="Повторы"><RepeatsRow repeats={repeats} /></Row>
          </dl>

          <div className="forecast-card__recommendation">
            <IconChecklist size={20} aria-hidden className="forecast-card__recommendation-icon" />
            <div>
              <div className="forecast-card__recommendation-head">
                <strong>Рекомендация</strong>
                {recommendation && !recommendation.regulation_confirmed
                  ? <span className="text-label">{RECOMMENDATION_UNCONFIRMED}</span> : null}
              </div>
              {recommendation ? (
                <>
                  <p>{recommendation.text}</p>
                  {/* v5: уточнения сервера (повтор, группы линий) — списком под основным текстом. */}
                  {recommendation.details?.length ? (
                    <ul className="forecast-card__recommendation-details">
                      {recommendation.details.map((detail) => <li key={detail}>{detail}</li>)}
                    </ul>
                  ) : null}
                  {recommendation.decision_code
                    ? <p className="muted small">Рекомендуемое действие: {decisionLabel(recommendation.decision_code)}</p> : null}
                </>
              ) : (
                <p className="muted">{snapshot ? 'В снимке журнала рекомендация не хранится; текущая — в открытой карточке.' : 'Сервер не выдал рекомендацию.'}</p>
              )}
            </div>
          </div>

          {view?.observed_overlay ? (
            <div className="forecast-card__overlay">
              <strong>Уже наблюдается после выдачи</strong>
              <span className="muted small"> · прогноз не пересчитывался, проверено {fmtDateTime(view.observed_overlay.checked_at)}</span>
              <ul>
                {view.observed_overlay.items.map((item) => (
                  <li key={`${item.channel_id}-${item.event_at}`}>
                    {fmtDateTime(item.event_at)} · <span title={item.channel_id}>{lineName(item.channel_id)}</span> · «{item.value_raw}»{item.alarm ? ` · ${SMVU_ALARM}` : ''}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <Collapse
            ghost
            className="forecast-card__details"
            items={[{
              key: 'details',
              label: 'Подробнее: линии электропитания, факты, частота, окно, история решений, версии',
              children: (
                <div className="card-details">
                  <section>
                    <h3>Что считается событием</h3>
                    <p>{ruQuotes(card.event_label)}</p>
                  </section>
                  <section>
                    <h3>Линии электропитания с наибольшим числом потерь связи за 365 сут</h3>
                    {/* Основание подписи — проверка DS 27.09: топ-5 линий задеты в 53–58% событий. */}
                    <p className="muted small">ориентир: событие задевает их примерно в половине случаев</p>
                    <table className="plain-table">
                      <thead>
                        <tr><th scope="col">Линия</th><th scope="col">Группа</th><th scope="col">Пикет</th><th scope="col">{LINE_LOSSES_365_LABEL}</th><th scope="col">Последняя потеря связи</th></tr>
                      </thead>
                      <tbody>
                        {channels.slice(0, 5).map((channel) => (
                          <tr key={channel.channel_id}>
                            <td>{channel.channel_name ?? channel.channel_id}</td>
                            <td>{channel.feeder_kind ? FEEDER_KIND_LABELS[channel.feeder_kind] ?? channel.feeder_kind : '—'}</td>
                            <td>{channelPicket(channel)}{channel.picket_basis ? <span className="muted small"> · {PICKET_BASIS_LABELS[channel.picket_basis] ?? channel.picket_basis}</span> : null}</td>
                            <td>{channelEpisodes(channel) ?? '—'}</td>
                            <td>{fmtDateTime(channelLastEpisode(channel))}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <p className="muted small">
                      Показаны {Math.min(5, channels.length)} из {total} {plural(total, ['линии', 'линий', 'линий'])} электропитания. 1 ПК = 10 м; положение по названию
                      канала или справочнику, длина объекта неизвестна.
                    </p>
                    {card.active_episode_channel_ids?.length ? (
                      <p className="small">
                        Уже без связи при выдаче: {card.active_episode_channel_ids.map(lineName).join(', ')}.
                      </p>
                    ) : null}
                  </section>
                  <section>
                    <h3>Факты за период до выдачи</h3>
                    <ul className="card-details__facts">
                      {(card.facts ?? []).map((fact) => (
                        <li key={`${fact.kind}-${fact.period_start}`}>
                          {fact.text}{fact.value_at ? `: ${fmtDateTime(fact.value_at)}` : ''}
                          <span className="muted small"> · {fmtDate(fact.period_start)} — {fmtDate(fact.period_end)}</span>
                        </li>
                      ))}
                    </ul>
                    {facts.powerOffText ? null : <p className="muted small">Сведений о «Обесточен» следом за потерей связи в карточке нет.</p>}
                  </section>
                  {card.score?.frequency ? (
                    <section>
                      <h3>Оценка вероятности: как получена</h3>
                      <p>
                        {card.score.frequency.positive_cards} из {card.score.frequency.cards} прошлых карточек с похожей историей
                        (событий на объекте за 365 сут: {card.score.frequency.events_to === null || card.score.frequency.events_to === undefined
                          ? `от ${card.score.frequency.events_from}`
                          : `${card.score.frequency.events_from}–${card.score.frequency.events_to}`}).
                        Интервал: {frequency?.interval}. Период таблицы: {fmtIsoDay(card.score.frequency.period_start)} — {fmtIsoDay(card.score.frequency.period_end)}.
                      </p>
                      <p className="muted small">{FREQUENCY_CALIBRATION_NOTE} Таблица: {card.score.frequency.table_version}.</p>
                    </section>
                  ) : null}
                  <section>
                    <h3>Окно и данные</h3>
                    <dl className="kv">
                      <dt>Выдана</dt><dd>{fmtDateTime(card.issued_at)}, опубликована {fmtDateTime(card.published_at)}</dd>
                      <dt>Окно</dt><dd>{fmtDateTime(card.window_start)} — {fmtDateTime(card.window_end)}</dd>
                      <dt>Данные до</dt><dd>{fmtDateTime(card.freshness.data_as_of)}; последняя запись объекта {fmtDateTime(card.freshness.last_object_record_at)}</dd>
                      <dt>История</dt><dd>{card.freshness.history_days_available ?? '—'} сут из {card.freshness.lookback_days}; полнота {COVERAGE_LABELS[card.freshness.coverage] ?? card.freshness.coverage}</dd>
                    </dl>
                  </section>
                  {decisions ? (
                    <section>
                      <h3>История решений</h3>
                      {decisions.error && !decisions.data ? <StateMessage compact kind="error" title="История решений недоступна" error={decisions.error} onRetry={decisions.reload} /> : null}
                      {decisions.data && !decisions.data.items.length ? <p className="muted">Решений не было.</p> : null}
                      <div className="stack">
                        {(decisions.data?.items ?? []).map((item) => (
                          <DecisionSummary key={item.revision} decision={item} draftText={draftTextFor(card.id, item.revision)} />
                        ))}
                      </div>
                    </section>
                  ) : null}
                  <section>
                    <h3>Версии</h3>
                    <dl className="kv">
                      <dt>Способ выдачи</dt><dd>{scorerLabel(card.versions.scorer)}, {card.versions.model_version}</dd>
                      <dt>Признаки и метки</dt><dd>{card.versions.feature_version}; {card.versions.label_version}</dd>
                      <dt>Правила списка</dt><dd>{card.versions.policy_version}{card.versions.recurrence_rule_version ? `; ${card.versions.recurrence_rule_version}` : ''}</dd>
                      <dt>Выпуск и расчёт</dt><dd>{card.versions.release_id}; {card.versions.run_id}</dd>
                      {view?.recommendation ? (
                        <><dt>Рекомендация</dt><dd>{view.recommendation.policy_version}{view.recommendation.rule_ids?.length ? `; ${view.recommendation.rule_ids.join(', ')}` : ''}</dd></>
                      ) : null}
                    </dl>
                  </section>
                </div>
              ),
            }]}
          />
        </section>
        <aside className="forecast-card__aside">{aside}</aside>
      </div>
    </article>
  );
}
