import { useCallback, useMemo, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { getForecast, getJournal, listCheckResults, listDecisions } from '../../api/forecast';
import { getScheme } from '../../api/scheme';
import { useForecastState } from '../../providers/ForecastStateProvider';
import { ROUTES } from '../../shared/config/routes';
import { repeatSummary, schemeLineNames } from '../../shared/lib/forecast';
import { mskIsoDay } from '../../shared/lib/format';
import { useAsync } from '../../shared/lib/use-async';
import { useEscape } from '../../shared/lib/use-escape';
import { StateMessage } from '../../shared/ui/StateMessage';
import { CheckResultPanel } from '../../widgets/forecast-card/CheckResultPanel';
import { DecisionPanel } from '../../widgets/forecast-card/DecisionPanel';
import { NowOnObject } from '../../widgets/forecast-card/NowOnObject';
import { ForecastCardWidget } from '../../widgets/forecast-card/ForecastCardWidget';
import { backLink } from './back-link';

const DAY_MS = 24 * 60 * 60 * 1000;

function CardView({ forecastId }: { forecastId: string }) {
  const [params] = useSearchParams();
  const back = backLink(params.get('back'));
  const navigate = useNavigate();
  useEscape(useCallback(() => navigate(back.to), [navigate, back.to]));
  const { objectName, state, generation } = useForecastState();
  const view = useAsync((signal) => getForecast(forecastId, signal), [forecastId, generation]);
  const decisions = useAsync((signal) => listDecisions(forecastId, signal), [forecastId]);
  const checkResults = useAsync((signal) => listCheckResults(forecastId, signal), [forecastId]);
  // Решение, сохранённое в этой вкладке: в демонстрации сервер его не записывает и в список не вернёт.
  const [decidedHere, setDecidedHere] = useState(false);
  const card = view.data?.card;
  const objectId = card?.object_id ?? null;
  // Та же схема объекта, что на мнемосхеме: текущие тревоги по линиям и линии с пикетом.
  const scheme = useAsync((signal) => getScheme(objectId as string, signal), [objectId, generation], Boolean(objectId));
  const issuedFrom = card ? mskIsoDay(new Date(Date.parse(card.issued_at) - 30 * DAY_MS)) : '';
  // Журнал объекта за 30 сут: «m-я карточка», прошлое решение и итог; линии события снятой карточки.
  const history = useAsync(
    (signal) => getJournal({ object_id: objectId ?? undefined, issued_from: issuedFrom, limit: 100 }, signal),
    [objectId, issuedFrom, generation],
    Boolean(card && objectId),
  );
  const repeats = useMemo(() => ({
    ...history,
    data: card && history.data ? repeatSummary(card, history.data.items) : null,
  }), [card, history]);

  if (view.loading) return <StateMessage kind="loading" title="Загрузка карточки…" />;
  if (!view.data) {
    return (
      <div className="stack" style={{ maxWidth: 820 }}>
        <StateMessage kind="error" title="Карточка недоступна" error={view.error} onRetry={view.reload}
          action={<Link to={back.to}>{back.label}</Link>} />
      </div>
    );
  }
  const data = view.data;
  const horizon = (state?.horizons ?? []).find((item) => item.target_spec_id === data.card.target_spec_id && item.horizon === data.card.horizon);
  const isNew = horizon !== undefined && Date.parse(horizon.issued_at) === Date.parse(data.card.issued_at);
  const releaseEntry = history.data?.items.find((entry) => entry.card.id === data.card.id) ?? null;

  return (
    <div className="stack">
      {view.error ? (
        <StateMessage compact kind="stale" title="Карточка не обновилась" error={view.error} onRetry={view.reload}>
          Показано последнее прочитанное состояние.
        </StateMessage>
      ) : null}
      <ForecastCardWidget
        card={data.card}
        view={data}
        objectName={objectName(data.card.object_id)}
        isNew={isNew}
        back={back}
        repeats={repeats}
        decisions={decisions}
        releaseEntry={releaseEntry}
        nowOnObject={objectId ? <NowOnObject scheme={scheme} /> : null}
        linesTotal={scheme.data ? (scheme.data.feeders ?? []).length : null}
        schemeLineNames={schemeLineNames(scheme.data)}
        aside={(
          <div className="stack">
            <DecisionPanel
              key={forecastId}
              forecastId={forecastId}
              decisions={decisions}
              recommendedCode={data.recommendation?.decision_code}
              recommendationText={data.recommendation?.text}
              listState={data.list_state}
              onSaved={() => setDecidedHere(true)}
            />
            <CheckResultPanel
              key={`check-${forecastId}`}
              forecastId={forecastId}
              results={checkResults}
              hasDecision={decidedHere || (decisions.data ? decisions.data.items.length > 0 : null)}
              eventRegistered={data.list_state === 'released'}
            />
          </div>
        )}
      />
      {data.card.object_id ? (
        <p className="muted small">
          По объекту: <Link to={`${ROUTES.journal}?object=${encodeURIComponent(data.card.object_id)}`}>все карточки в журнале</Link>
        </p>
      ) : null}
    </div>
  );
}

export function ForecastCardPage() {
  const { forecastId = '' } = useParams();
  // Новый id — новое состояние страницы: данные другой карточки не мелькают.
  return <CardView key={forecastId} forecastId={forecastId} />;
}
