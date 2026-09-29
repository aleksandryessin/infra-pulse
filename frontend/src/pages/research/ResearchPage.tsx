import { Collapse } from 'antd';
import { getJournalQuality } from '../../api/forecast';
import { getResearchSummary, type ResearchBlock, type ResearchMetric, type ResearchScope } from '../../api/research';
import { useForecastState } from '../../providers/ForecastStateProvider';
import { EVIDENCE_SCOPE_LABELS, LADDER_STEP_LABELS, RESEARCH_STATUS_LABELS } from './research-labels';
import { decimal, fmtIsoDay } from '../../shared/lib/format';
import { useAsync } from '../../shared/lib/use-async';
import { NoAccess } from '../../shared/ui/NoAccess';
import { StateMessage } from '../../shared/ui/StateMessage';
import { ApiError } from '../../api/http';
import './ResearchPage.css';

/**
 * Целевые показатели проекта для точности и полноты списка: P ≥ 0,70 и R ≥ 0,50 одновременно,
 * по нижним границам 95% ДИ. ТЗ §9 оставляет их на этап проектирования; 0,7 и 0,5 организаторы
 * назвали плановыми по умолчанию. Константа проекта, не результат исследования; в сводке API её
 * нет (запрос на contract).
 */
const PROJECT_TARGET = { precision: 0.7, recall: 0.5 } as const;

const METRIC_LABELS: Record<string, string> = {
  precision: 'P — доля карточек со событием',
  recall: 'R — доля инцидентов, пойманных списком',
  recall_incident: 'R — доля инцидентов, пойманных списком',
  precision_objects: 'P по объектам',
  precision_by_object: 'P по объектам',
  lift_precision: 'прирост P над случайным списком',
  lift_recall: 'прирост R над случайным списком',
  events_with_open_card: 'события при открытой карточке',
};

function metricLabel(name: string): string {
  return METRIC_LABELS[name] ?? name.replace(/_/g, ' ');
}

function findMetric(metrics: ResearchMetric[] | undefined, names: string[]): ResearchMetric | undefined {
  return names.map((name) => (metrics ?? []).find((metric) => metric.name === name)).find(Boolean);
}

function withCi(metric: ResearchMetric | undefined): string {
  if (!metric || metric.value === null || metric.value === undefined) return '—';
  const ci = metric.ci_low !== null && metric.ci_low !== undefined && metric.ci_high !== null && metric.ci_high !== undefined
    ? ` [${decimal(metric.ci_low)}; ${decimal(metric.ci_high)}]` : ' (ДИ не указан)';
  return `${decimal(metric.value)}${ci}`;
}

/** Ступень, которая выдаётся в продукте: модель или статистический список (первая из них в сводке). */
function productRow(scope: ResearchScope) {
  return (scope.ladder ?? []).find((row) => row.step === 'model' || row.step === 'static_list') ?? null;
}

function lift(scope: ResearchScope, name: 'precision' | 'recall'): string | null {
  const product = productRow(scope);
  const random = (scope.ladder ?? []).find((row) => row.step === 'random_list');
  const names = name === 'recall' ? ['recall_incident', 'recall'] : ['precision'];
  const a = product ? findMetric(product.metrics, names)?.value : undefined;
  const b = random ? findMetric(random.metrics, names)?.value : undefined;
  if (a === null || a === undefined || b === null || b === undefined) return null;
  const diff = a - b;
  return `${diff >= 0 ? '+' : '−'}${decimal(Math.abs(diff), 2)}`;
}

/** Один график: точка и 95% ДИ для P и R, пунктир — целевой показатель проекта. */
function CiChart({ scope }: { scope: ResearchScope }) {
  const product = productRow(scope);
  if (!product) return null;
  const rows = [
    { key: 'P', metric: findMetric(product.metrics, ['precision']), target: PROJECT_TARGET.precision },
    { key: 'R', metric: findMetric(product.metrics, ['recall_incident', 'recall']), target: PROJECT_TARGET.recall },
  ].filter((row) => row.metric?.value !== null && row.metric?.value !== undefined);
  if (!rows.length) return null;
  const width = 560;
  const left = 40;
  const right = 20;
  const x = (value: number) => left + value * (width - left - right);
  const rowHeight = 40;
  const height = rows.length * rowHeight + 34;
  return (
    <figure className="ci-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img"
        aria-label={`${scope.title}: ${rows.map((row) => `${row.key} ${withCi(row.metric)}, целевой показатель проекта ${decimal(row.target, 2)}`).join('; ')}`}>
        {[0, 0.25, 0.5, 0.75, 1].map((tick) => (
          <g key={tick}>
            <line x1={x(tick)} x2={x(tick)} y1={8} y2={height - 24} className="ci-chart__grid" />
            <text x={x(tick)} y={height - 8} textAnchor="middle" className="ci-chart__axis">{decimal(tick, 2)}</text>
          </g>
        ))}
        {rows.map((row, index) => {
          const y = 8 + index * rowHeight + rowHeight / 2;
          const metric = row.metric as ResearchMetric;
          const value = metric.value as number;
          return (
            <g key={row.key}>
              <text x={8} y={y + 4} className="ci-chart__label">{row.key}</text>
              <line x1={x(row.target)} x2={x(row.target)} y1={y - 14} y2={y + 14} className="ci-chart__target" />
              <text x={x(row.target) + 4} y={y - 8} className="ci-chart__axis">цель проекта {decimal(row.target, 2)}</text>
              {metric.ci_low !== null && metric.ci_low !== undefined && metric.ci_high !== null && metric.ci_high !== undefined ? (
                <line x1={x(metric.ci_low)} x2={x(metric.ci_high)} y1={y} y2={y} className="ci-chart__ci" />
              ) : null}
              <circle cx={x(value)} cy={y} r={5} className="ci-chart__point" />
            </g>
          );
        })}
      </svg>
      <figcaption className="muted small">
        {scope.title}: точка — оценка, отрезок — 95% ДИ, пунктир — целевые показатели проекта: P ≥ 0,70
        и R ≥ 0,50 одновременно. ТЗ §9 оставляет их на этап проектирования; 0,7 и 0,5 организаторы назвали
        плановыми по умолчанию.
      </figcaption>
    </figure>
  );
}

function BlockTable({ block }: { block: ResearchBlock }) {
  return (
    <section className="panel">
      <header className="panel__head">
        <h2>{block.title}</h2>
        <span className="panel__meta">{RESEARCH_STATUS_LABELS[block.status] ?? block.status}</span>
      </header>
      <div className="panel__body">
        {block.caption ? <p className="muted small" style={{ marginTop: 0 }}>{block.caption}</p> : null}
        {block.status === 'needs_more_data' ? <p className="muted">Нужно больше данных; числа не показываются.</p> : (
          <table className="plain-table">
            <thead><tr>{(block.columns ?? []).map((column) => <th key={column.key} scope="col">{column.label}{column.unit ? `, ${column.unit}` : ''}</th>)}</tr></thead>
            <tbody>
              {(block.rows ?? []).map((row, index) => (
                <tr key={index}>{(block.columns ?? []).map((column) => {
                  const value = row[column.key];
                  return <td key={column.key}>{typeof value === 'number' ? value.toLocaleString('ru-RU') : value ?? '—'}</td>;
                })}</tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

function Quality() {
  const { generation } = useForecastState();
  const quality = useAsync((signal) => getJournalQuality(signal), [generation]);
  if (quality.loading) return <StateMessage compact kind="loading" title="Загрузка «прогноз против факта»…" />;
  if (!quality.data) return <StateMessage compact kind="error" title="«Прогноз против факта» недоступен" error={quality.error} onRetry={quality.reload} />;
  const data = quality.data;
  const eventNames = [...new Set(data.rows.flatMap((row) => (row.event_metrics ?? []).map((metric) => `${metric.name}|${metric.definition_version}`)))];
  return (
    <section className="panel">
      <header className="panel__head">
        <h2>Прогноз против факта по журналу</h2>
        <span className="panel__meta">{EVIDENCE_SCOPE_LABELS[data.evidence_scope] ?? data.evidence_scope}</span>
      </header>
      <div className="panel__body">
        <table className="plain-table">
          <thead>
            <tr>
              <th scope="col">Неделя выдачи</th><th scope="col">Выдано</th><th scope="col">Ожидается</th>
              <th scope="col">Со событием</th><th scope="col">Без события</th><th scope="col">Неизвестно</th>
              <th scope="col">Доля k из n</th>
              {eventNames.map((key) => {
                const [name, version] = key.split('|');
                return <th key={key} scope="col">{metricLabel(name)} <span className="muted small">({version})</span></th>;
              })}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row) => (
              <tr key={`${row.period_start}-${row.target_spec_id}-${row.horizon}`}>
                <td>{fmtIsoDay(row.period_start)} — {fmtIsoDay(row.period_end)}</td>
                <td>{row.cards_issued}</td><td>{row.cards_pending}</td><td>{row.cards_realized}</td>
                <td>{row.cards_not_realized}</td><td>{row.cards_unknown}</td>
                <td>{row.card_share.numerator} из {row.card_share.denominator}{row.card_share.value !== null && row.card_share.value !== undefined ? ` (${decimal(row.card_share.value, 2)})` : ''}</td>
                {eventNames.map((key) => {
                  const [name, version] = key.split('|');
                  const metric = (row.event_metrics ?? []).find((item) => item.name === name && item.definition_version === version);
                  return <td key={key}>{metric ? `${metric.numerator} из ${metric.denominator}` : '—'}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted small">Доля k из n — карточки со событием из разрешённых; ожидающие и неизвестные в долю не входят.</p>
      </div>
    </section>
  );
}

export default function ResearchPage() {
  const summary = useAsync((signal) => getResearchSummary(signal), []);
  if (summary.error instanceof ApiError && summary.error.status === 403) {
    return <NoAccess section="Исследование" permission="research" />;
  }
  if (summary.loading) return <StateMessage kind="loading" title="Загрузка сводки исследования…" />;
  if (!summary.data) {
    return <StateMessage kind="error" title="Сводка исследования недоступна" error={summary.error} onRetry={summary.reload} />;
  }
  const data = summary.data;
  const blocks = data.blocks ?? [];
  const caveats = data.caveats ?? [];
  const sources = data.source_reports ?? [];
  const inProduct = data.scopes.filter((scope) => scope.status === 'in_product');
  return (
    <div className="research-page">
      {data.synthetic ? (
        <StateMessage compact kind="stale" title="Синтетическая сводка">
          Числа для проверки интерфейса, не результат исследования.
        </StateMessage>
      ) : null}

      <section className="panel">
        <header className="panel__head">
          <h2>Области прогноза</h2>
          <span className="panel__meta">сводка {data.version}</span>
        </header>
        <table className="plain-table research-table">
          <thead>
            <tr>
              <th scope="col">Область</th><th scope="col">Статус</th><th scope="col">Горизонт</th><th scope="col">Карточек</th>
              <th scope="col">P [95% ДИ]</th><th scope="col">R [95% ДИ]</th><th scope="col">Период оценки</th><th scope="col">Примечание</th>
            </tr>
          </thead>
          <tbody>
            {data.scopes.map((scope) => {
              const product = productRow(scope);
              const hideNumbers = scope.status === 'needs_more_data';
              const liftP = lift(scope, 'precision');
              const liftR = lift(scope, 'recall');
              const extra = (product?.metrics ?? []).filter((metric) => !['precision', 'recall', 'recall_incident'].includes(metric.name)) ?? [];
              return (
                <tr key={scope.scope_id}>
                  <td><strong>{scope.title}</strong>{scope.sensor_types?.length ? <div className="muted small">{scope.sensor_types.join(', ')}</div> : null}</td>
                  <td>{RESEARCH_STATUS_LABELS[scope.status] ?? scope.status}</td>
                  <td>{scope.horizon_days ? `${scope.horizon_days} сут` : '—'}</td>
                  <td>{scope.budget_k ?? '—'}</td>
                  <td>{hideNumbers ? '—' : withCi(product ? findMetric(product.metrics, ['precision']) : undefined)}</td>
                  <td>{hideNumbers ? '—' : withCi(product ? findMetric(product.metrics, ['recall_incident', 'recall']) : undefined)}</td>
                  <td>
                    {scope.evaluation_period}
                    {scope.period_use_count ? <div className="muted small">период использован {scope.period_use_count}-й раз</div> : null}
                  </td>
                  <td>
                    {hideNumbers ? 'Нужно больше данных; числа не показываются. ' : null}
                    {scope.note}
                    {!hideNumbers && (liftP || liftR) ? <div className="small">разность со случайным списком: P {liftP ?? '—'}, R {liftR ?? '—'}</div> : null}
                    {!hideNumbers && extra.map((metric) => (
                      <div key={metric.name} className="small">{metricLabel(metric.name)}: {withCi(metric)}</div>
                    ))}
                    {scope.events_per_year !== null && scope.events_per_year !== undefined ? <div className="muted small">событий в год: {scope.events_per_year}</div> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      {inProduct[0] ? <section className="panel"><div className="panel__body"><CiChart scope={inProduct[0]} /></div></section> : null}

      <Collapse
        items={[{
          key: 'ladder',
          label: 'Сравнение со способами-ориентирами (включая верхнюю границу при известном исходе)',
          children: (
            <table className="plain-table">
              <thead><tr><th scope="col">Область</th><th scope="col">Способ</th><th scope="col">P [95% ДИ]</th><th scope="col">R [95% ДИ]</th></tr></thead>
              <tbody>
                {data.scopes.filter((scope) => scope.status !== 'needs_more_data').flatMap((scope) => (scope.ladder ?? []).map((row) => (
                  <tr key={`${scope.scope_id}-${row.step}`}>
                    <td>{scope.title}</td>
                    <td>{LADDER_STEP_LABELS[row.step] ?? row.label}</td>
                    <td>{withCi(findMetric(row.metrics, ['precision']))}</td>
                    <td>{withCi(findMetric(row.metrics, ['recall_incident', 'recall']))}</td>
                  </tr>
                )))}
              </tbody>
            </table>
          ),
        }]}
      />

      <Quality />

      {blocks.map((block) => <BlockTable key={block.block_id} block={block} />)}

      {caveats.length || sources.length ? (
        <section className="muted small">
          {caveats.map((caveat) => <p key={caveat} style={{ margin: '2px 0' }}>{caveat}</p>)}
          {sources.length ? <p style={{ margin: '2px 0' }}>Источники: {sources.join(', ')}</p> : null}
        </section>
      ) : null}
    </div>
  );
}
