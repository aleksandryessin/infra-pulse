import { useRef, useState } from 'react';
import { Button, Input, Radio, Select } from 'antd';
import { IconClipboardCheck } from '@tabler/icons-react';
import {
  createCheckResult, type CheckResultStatus, type EventCause, type ForecastCheckResult, type ForecastCheckResultCreate,
  type ForecastCheckResultList, type FoundItem,
} from '../../api/forecast';
import { ApiError, describeApiError, errorCode } from '../../api/http';
import { useSession } from '../../providers/SessionProvider';
import { ROLE_LABELS, rolesAllowed } from '../../shared/config/permissions';
import { CHECK_RESULT_LABELS, EVENT_CAUSE_LABELS, FOUND_LABELS } from '../../shared/config/labels';
import {
  CHECK_RESULT_AFTER_DECISION, DEMO_MARK, DEMO_MARK_HINT, DEMO_NOT_SAVED_NOTE, DEMO_RESULT_NOT_SAVED, actorText,
} from '../../shared/config/wording';
import { fmtDateTime } from '../../shared/lib/format';
import { mskLocalToIso, mskNowLocal, newIdempotencyKey } from '../../shared/lib/form-keys';
import type { AsyncState } from '../../shared/lib/use-async';
import { StateMessage } from '../../shared/ui/StateMessage';

/** Перечень «что устранено» заказчик признал достаточным (ответ 28.09); пометка — только в форме. */
const FOUND_NOTE = 'перечень подтверждён заказчиком 28.09';

function ResultSummary({ result }: { result: ForecastCheckResult }) {
  const role = ROLE_LABELS[result.actor_role as keyof typeof ROLE_LABELS] ?? result.actor_role;
  const found = (result.found ?? []).map((item) => (item === 'other' && result.found_other_text ? `другое: ${result.found_other_text}` : FOUND_LABELS[item] ?? item));
  return (
    <div className="decision-summary">
      <div className="decision-summary__head">
        <strong>{CHECK_RESULT_LABELS[result.check_result] ?? result.check_result}</strong>
        <span className="muted"> · ревизия {result.revision}</span>
        {result.simulated ? <span className="text-label" title={DEMO_MARK_HINT}>{DEMO_MARK}</span> : null}
      </div>
      <dl className="decision-summary__fields">
        {found.length ? (<><dt>Что устранено</dt><dd>{found.join(', ')}</dd></>) : null}
        {result.event_cause ? (<><dt>Причина события</dt><dd>{EVENT_CAUSE_LABELS[result.event_cause] ?? result.event_cause}</dd></>) : null}
        {result.comment ? (<><dt>Комментарий</dt><dd>{result.comment}</dd></>) : null}
        <dt>Результат на</dt><dd>{fmtDateTime(result.result_at)}</dd>
        <dt>Автор</dt><dd>{actorText(result.actor_id, result.simulated)} ({role}), {fmtDateTime(result.recorded_at)}</dd>
      </dl>
    </div>
  );
}

interface Values {
  status: CheckResultStatus | null;
  found: FoundItem[];
  foundOther: string;
  resultAt: string;
  comment: string;
  cause: EventCause | null;
}

const EMPTY: Values = { status: null, found: [], foundOther: '', resultAt: '', comment: '', cause: null };

/**
 * «Результат проверки» (C0.4): отдельное действие по карточке после решения, ревизии с
 * аудитом. «Что устранено» — только при «устранено» и с пометкой «не подтверждено
 * заказчиком»; «Причина события» — только у карточки, снятой по событию.
 * Пока решения нет, кнопка неактивна и рядом сказано почему (M5 аудита 29.09); если решения
 * не загрузились (`hasDecision === null`), кнопку не блокируем — сервер решение не требует.
 */
export function CheckResultPanel({
  forecastId, results, hasDecision, eventRegistered,
}: {
  forecastId: string;
  results: AsyncState<ForecastCheckResultList>;
  /** Есть ли решение по карточке; null — неизвестно (решения не загружены). */
  hasDecision: boolean | null;
  eventRegistered: boolean;
}) {
  const { can, roles } = useSession();
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Values>(EMPTY);
  const [touched, setTouched] = useState(false);
  const [state, setState] = useState<{ kind: 'idle' } | { kind: 'saving' } | { kind: 'saved'; result: ForecastCheckResult } | { kind: 'error'; error: unknown }>({ kind: 'idle' });
  const attempt = useRef<{ key: string; body: string } | null>(null);
  const latest = results.data?.items[0] ?? null;
  const nowLocal = mskNowLocal();
  const needsDecision = hasDecision === false && !latest;
  const hintId = `check-result-hint-${forecastId}`;
  // Сохранённый на сервере результат после перезагрузки списка — это и есть последний:
  // показываем его один раз, в блоке «Результат сохранён» (QA 29.09, D2). Демонстрационный
  // результат сервер не хранит — последний с сервера остаётся рядом с ним. Если на сервере
  // уже более новая ревизия (сохранили в другом окне), блок «сохранён» не показывается.
  const saved = state.kind === 'saved' ? state.result : null;
  const justSaved = saved && (saved.simulated || !latest || latest.revision <= saved.revision) ? saved : null;
  const savedIsLatest = justSaved !== null && !justSaved.simulated;

  const problems: string[] = [];
  if (!values.status) problems.push('выберите результат');
  if (!values.resultAt) problems.push('укажите, когда получен результат');
  if (values.resultAt && values.resultAt > nowLocal) problems.push('время результата в будущем');
  if (values.found.includes('other') && !values.foundOther.trim()) problems.push('опишите «другое»');

  const patch = (next: Partial<Values>) => {
    setValues((current) => ({ ...current, ...next }));
    if (state.kind === 'saved') setState({ kind: 'idle' });
  };

  const submit = async () => {
    setTouched(true);
    if (problems.length || !values.status) return;
    const fixed = values.status === 'fixed';
    const found = fixed ? values.found : [];
    const base: Omit<ForecastCheckResultCreate, 'idempotency_key'> = {
      check_result: values.status,
      found,
      found_other_text: found.includes('other') ? values.foundOther.trim().slice(0, 200) : null,
      result_at: mskLocalToIso(values.resultAt),
      comment: values.comment.trim() ? values.comment.trim().slice(0, 500) : null,
      event_cause: eventRegistered ? values.cause : null,
      expected_revision: latest?.revision ?? 0,
    };
    const body = JSON.stringify(base);
    if (!attempt.current || attempt.current.body !== body) attempt.current = { key: newIdempotencyKey(), body };
    setState({ kind: 'saving' });
    try {
      const saved = await createCheckResult(forecastId, { ...base, idempotency_key: attempt.current.key });
      attempt.current = null;
      setState({ kind: 'saved', result: saved });
      setValues(EMPTY);
      setTouched(false);
      setOpen(false);
      if (!saved.simulated) results.reload();
    } catch (error) {
      setState({ kind: 'error', error });
      if (error instanceof ApiError && error.status === 409) results.reload();
    }
  };

  const errorText = (error: unknown): string => {
    const code = error instanceof ApiError ? error.detail : null;
    if (code === 'check_result_revision_conflict') return 'Результат по карточке изменили в другом окне. Посмотрите последнюю ревизию и сохраните снова — ввод сохранён.';
    if (code === 'result_after_record') return 'Время результата позже времени записи на сервере. Проверьте дату — ввод сохранён.';
    if (code === 'event_cause_requires_event') return 'Причину события можно указать только у карточки, снятой по событию.';
    if (error instanceof ApiError && error.status === 403) return 'Нет права записывать результат проверки для вашей роли.';
    return `${describeApiError(error)}. Ввод сохранён.`;
  };

  return (
    <div className="decision-panel check-result-panel">
      <h2>Результат проверки</h2>
      {results.loading ? <StateMessage compact kind="loading" title="Загрузка результатов…" /> : null}
      {results.error && !results.data ? <StateMessage compact kind="error" title="Результаты проверки не загружены" error={results.error} onRetry={results.reload} /> : null}
      {savedIsLatest ? null
        : latest ? <ResultSummary result={latest} /> : results.data ? <p className="muted">Результата проверки пока нет.</p> : null}
      {justSaved ? (
        <div className="decision-panel__saved" role="status">
          <strong>{justSaved.simulated ? DEMO_RESULT_NOT_SAVED : 'Результат сохранён'}</strong>
          <span className="muted small">{justSaved.simulated ? ` ${DEMO_NOT_SAVED_NOTE}` : ''}</span>
          <ResultSummary result={justSaved} />
        </div>
      ) : null}

      {!can('decide') ? (
        <StateMessage compact kind="denied" title="Результат записывают другие роли">
          Доступно: {rolesAllowed('decide')}. Ваша роль: {roles.map((role) => ROLE_LABELS[role]).join(', ')}.
        </StateMessage>
      ) : !open ? (
        <>
          <Button icon={<IconClipboardCheck size={18} />} onClick={() => setOpen(true)} disabled={needsDecision}
            aria-describedby={needsDecision ? hintId : undefined}>
            Внести результат проверки
          </Button>
          {needsDecision ? <p id={hintId} className="muted small check-result-panel__hint">{CHECK_RESULT_AFTER_DECISION}</p> : null}
        </>
      ) : (
        <form className="decision-form" onSubmit={(event) => { event.preventDefault(); void submit(); }} noValidate>
          <fieldset>
            <legend>Результат</legend>
            <Radio.Group value={values.status} onChange={(event) => patch({ status: event.target.value as CheckResultStatus })}
              className="decision-form__codes">
              {Object.entries(CHECK_RESULT_LABELS).map(([value, label]) => <Radio.Button key={value} value={value}>{label}</Radio.Button>)}
            </Radio.Group>
          </fieldset>
          {values.status === 'fixed' ? (
            <label className="decision-form__field">
              <span>Что устранено <span className="text-label">{FOUND_NOTE}</span></span>
              <Select<FoundItem[]> mode="multiple" value={values.found} onChange={(found) => patch({ found })}
                placeholder="Выберите, если известно" aria-label="Что устранено"
                options={Object.entries(FOUND_LABELS).map(([value, label]) => ({ value: value as FoundItem, label }))} />
              {values.found.includes('other') ? (
                <Input value={values.foundOther} maxLength={200} placeholder="Что именно" aria-label="Что устранено: другое"
                  onChange={(event) => patch({ foundOther: event.target.value })} />
              ) : null}
            </label>
          ) : null}
          {eventRegistered ? (
            <label className="decision-form__field">
              <span>Причина события <span className="muted">(необязательно)</span></span>
              <Select<EventCause> allowClear value={values.cause ?? undefined} onChange={(cause) => patch({ cause: cause ?? null })}
                placeholder="Если известна" aria-label="Причина события"
                options={Object.entries(EVENT_CAUSE_LABELS).map(([value, label]) => ({ value: value as EventCause, label }))} />
            </label>
          ) : null}
          <label className="decision-form__field">
            <span>Результат получен, МСК</span>
            <Input type="datetime-local" value={values.resultAt} max={nowLocal} aria-label="Результат получен, МСК"
              onChange={(event) => patch({ resultAt: event.target.value })} />
          </label>
          <label className="decision-form__field">
            <span>Комментарий <span className="muted">(необязательно)</span></span>
            <Input.TextArea rows={2} value={values.comment} maxLength={500} showCount aria-label="Комментарий к результату"
              onChange={(event) => patch({ comment: event.target.value })} />
          </label>
          {touched && problems.length ? <p className="decision-form__problems" role="alert">Чтобы сохранить: {problems.join('; ')}.</p> : null}
          <div className="check-result-panel__actions">
            <Button type="primary" htmlType="submit" loading={state.kind === 'saving'}>Сохранить результат</Button>
            <Button onClick={() => setOpen(false)}>Отмена</Button>
          </div>
        </form>
      )}
      {state.kind === 'error' ? (
        <StateMessage compact kind="error" title="Результат не сохранён">
          {errorText(state.error)}{errorCode(state.error) ? <span className="small"> · {errorCode(state.error)}</span> : null}
        </StateMessage>
      ) : null}
      {(results.data?.items.length ?? 0) > 1 ? (
        <details>
          <summary className="small">История результатов: {results.data?.items.length}</summary>
          <div className="stack">{results.data?.items.slice(1).map((item) => <ResultSummary key={item.revision} result={item} />)}</div>
        </details>
      ) : null}
    </div>
  );
}
