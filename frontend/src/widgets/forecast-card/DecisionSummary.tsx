import type { ForecastDecisionSummary } from '../../api/forecast';
import { ROLE_LABELS } from '../../shared/config/permissions';
import { VERIFICATION_LABELS, decisionLabel } from '../../shared/config/labels';
import { DEMO_MARK, DEMO_MARK_HINT, DRAFT_NOT_SENT, DRAFT_TEXT_TAB_NOTE, actorText } from '../../shared/config/wording';
import { fmtDateTime } from '../../shared/lib/format';

/**
 * Одно решение: действие, основание, как проверено, кому сообщено (для «Сообщено энергетику»),
 * сроки, черновик, автор и время. `draftText` — текст черновика, если он известен этой вкладке
 * (сервер в ответе отдаёт только отметку «не отправлен»).
 */
export function DecisionSummary({
  decision, compact, draftText,
}: {
  decision: ForecastDecisionSummary;
  compact?: boolean;
  draftText?: string | null;
}) {
  const role = ROLE_LABELS[decision.actor_role as keyof typeof ROLE_LABELS] ?? decision.actor_role;
  return (
    <div className={`decision-summary${compact ? ' decision-summary--compact' : ''}`}>
      <div className="decision-summary__head">
        <strong>{decisionLabel(decision.decision_code)}</strong>
        <span className="muted"> · ревизия {decision.revision}</span>
        {decision.simulated ? <span className="text-label" title={DEMO_MARK_HINT}>{DEMO_MARK}</span> : null}
      </div>
      <dl className="decision-summary__fields">
        <dt>Основание</dt>
        <dd>{decision.reason_text} <span className="muted small">({decision.reason_code})</span></dd>
        <dt>Как проверено</dt>
        <dd>{decision.verification_methods?.length
          ? decision.verification_methods.map((method) => VERIFICATION_LABELS[method] ?? method).join(', ')
          : 'не указано'}</dd>
        {decision.notified_to || decision.decision_code === 'R3' ? (
          <>
            <dt>Кому сообщено</dt>
            <dd>{decision.notified_to ? `${decision.notified_to}, ${fmtDateTime(decision.notified_at)}` : 'не сообщалось'}</dd>
          </>
        ) : null}
        {decision.awaiting_result_until ? (<><dt>Ждём результат до</dt><dd>{fmtDateTime(decision.awaiting_result_until)}</dd></>) : null}
        {decision.watch_until ? (<><dt>Под наблюдением до</dt><dd>{fmtDateTime(decision.watch_until)}</dd></>) : null}
        {decision.draft_id ? (
          <>
            <dt>Черновик для энергетика</dt>
            <dd className="decision-summary__draft">
              <span className="text-label text-label--draft">{DRAFT_NOT_SENT}</span>
              {draftText ? (
                <>
                  <blockquote className="decision-summary__draft-text">{draftText}</blockquote>
                  {decision.simulated ? null : <span className="muted small">{DRAFT_TEXT_TAB_NOTE}</span>}
                </>
              ) : null}
            </dd>
          </>
        ) : null}
        <dt>Автор</dt>
        <dd>{actorText(decision.actor_id, decision.simulated)} ({role}), {fmtDateTime(decision.decided_at)}</dd>
      </dl>
    </div>
  );
}
