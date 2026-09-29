import { useEffect, useRef, useState } from 'react';
import { Button, Input, Radio, Select } from 'antd';
import { IconDeviceFloppy, IconPencil } from '@tabler/icons-react';
import {
  createDecision, type DecisionCode, type ForecastDecisionCreate, type ForecastDecisionList,
  type ForecastDecisionSummary, type VerificationMethod,
} from '../../api/forecast';
import { ApiError, describeApiError, errorCode } from '../../api/http';
import { useSession } from '../../providers/SessionProvider';
import { ROLE_LABELS, rolesAllowed } from '../../shared/config/permissions';
import { DECISION_OPTIONS, VERIFICATION_LABELS, decisionLabel } from '../../shared/config/labels';
import {
  DEMO_DECISION_NOT_SAVED, DEMO_NOT_SAVED_NOTE, DRAFT_FROM_RECOMMENDATION, DRAFT_NOT_SENT,
} from '../../shared/config/wording';
import { fmtDateTime } from '../../shared/lib/format';
import { mskLocalToIso, mskNowLocal, newIdempotencyKey } from '../../shared/lib/form-keys';
import type { AsyncState } from '../../shared/lib/use-async';
import { StateMessage } from '../../shared/ui/StateMessage';
import { DecisionSummary } from './DecisionSummary';
import { draftTextFor, rememberDraftText } from './draft-texts';

interface FormValues {
  code: DecisionCode | null;
  other: DecisionCode | null;
  reason: string | null;
  comment: string;
  methods: VerificationMethod[];
  notifiedTo: string;
  notifiedAt: string;
  /** R3: ждём результат до (C0.4). */
  awaitingUntil: string;
  /** R1: под наблюдением до (C0.4). */
  watchUntil: string;
  draftNote: string;
}

const EMPTY: FormValues = {
  code: null, other: null, reason: null, comment: '', methods: [], notifiedTo: '', notifiedAt: '', awaitingUntil: '', watchUntil: '', draftNote: '',
};

/** Несохранённый ввод живёт в памяти вкладки по карточке: переход, ошибка и вход заново его не стирают. */
const drafts = new Map<string, FormValues>();

function hasInput(values: FormValues | undefined): boolean {
  if (!values) return false;
  return (Object.keys(EMPTY) as (keyof FormValues)[]).some((key) => {
    const value = values[key];
    return Array.isArray(value) ? value.length > 0 : Boolean(value);
  });
}

const PRIMARY = DECISION_OPTIONS.filter((option) => option.primary);
/** «Сообщено энергетику» (C0.4): кому, когда и «ждём результат до» обязательны. */
const NOTIFY_CODE: DecisionCode = 'R3';
/** «Под наблюдением» (C0.4): «до даты» обязательно. */
const WATCH_CODE: DecisionCode = 'R1';
const OTHER = DECISION_OPTIONS.filter((option) => !option.primary);

type SaveState =
  | { kind: 'idle' }
  | { kind: 'saving' }
  | { kind: 'saved'; decision: ForecastDecisionSummary }
  | { kind: 'error'; error: unknown };

/**
 * Решение по карточке (C0.4; В-5 аудита диспетчера 29.09): одно текущее решение с «Как проверено»
 * и текстом черновика; форма открыта, пока решения нет, и сворачивается после сохранения в
 * «Изменить решение» — второе решение случайно не записывается. Несохранённый ввод раскрывает форму.
 */
export function DecisionPanel({
  forecastId, decisions, recommendedCode, recommendationText, listState, onSaved,
}: {
  forecastId: string;
  decisions: AsyncState<ForecastDecisionList>;
  recommendedCode: DecisionCode | null | undefined;
  /** Текст рекомендации сервера: из него можно подставить черновик для энергетика. */
  recommendationText?: string | null;
  listState: string | null | undefined;
  /** Решение сохранено (в том числе в демонстрации, где сервер его не записывает). */
  onSaved?: (decision: ForecastDecisionSummary) => void;
}) {
  const { can, roles } = useSession();
  const [values, setValues] = useState<FormValues>(() => drafts.get(forecastId) ?? EMPTY);
  const [editing, setEditing] = useState(() => hasInput(drafts.get(forecastId)));
  const [touched, setTouched] = useState(false);
  const [save, setSave] = useState<SaveState>({ kind: 'idle' });
  const [seenRevision, setSeenRevision] = useState<number | null>(null);
  const attempt = useRef<{ key: string; body: string } | null>(null);

  useEffect(() => {
    drafts.set(forecastId, values);
  }, [forecastId, values]);

  // Ревизия, которую диспетчер видел: фиксируется при первом чтении и меняется только явно.
  const latest = decisions.data?.items[0] ?? null;
  useEffect(() => {
    if (decisions.data && seenRevision === null) setSeenRevision(latest?.revision ?? 0);
  }, [decisions.data, latest, seenRevision]);

  const option = DECISION_OPTIONS.find((item) => item.code === values.code) ?? null;
  const reason = option?.reasons.find((item) => item.code === values.reason) ?? null;
  // «Кому и когда сообщено» и «ждём результат до» — только для «Сообщено энергетику»; «до даты» — для «Под наблюдением».
  const asksNotify = option?.code === NOTIFY_CODE;
  const asksWatch = option?.code === WATCH_CODE;
  const reasonText = reason ? `${reason.text}${values.comment.trim() ? ` · ${values.comment.trim()}` : ''}` : '';
  const commentMax = Math.max(0, 300 - (reason?.text.length ?? 0) - 3);
  // Текущее московское время для datetime-local: время уведомления не может быть в будущем.
  const nowLocal = mskNowLocal();

  const problems: string[] = [];
  if (!option) problems.push('выберите действие');
  if (option && !reason) problems.push('укажите основание');
  if (!values.methods.length) problems.push('отметьте, как проверено');
  if (asksNotify && (!values.notifiedTo.trim() || !values.notifiedAt)) problems.push('укажите, кому и когда сообщено');
  if (asksNotify && values.notifiedAt && values.notifiedAt > nowLocal) problems.push('время уведомления в будущем');
  if (asksNotify && !values.awaitingUntil) problems.push('укажите, до какого срока ждём результат');
  if (asksNotify && values.awaitingUntil && values.notifiedAt && values.awaitingUntil <= values.notifiedAt) {
    problems.push('срок результата должен быть позже времени уведомления');
  }
  if (asksWatch && !values.watchUntil) problems.push('укажите, до какой даты под наблюдением');
  if (asksWatch && values.watchUntil && values.watchUntil <= nowLocal) problems.push('срок наблюдения должен быть позже текущего времени');

  const patch = (next: Partial<FormValues>) => setValues((current) => ({ ...current, ...next }));

  // Показывается одно решение: только что сохранённое (в демонстрации сервер его не вернёт) или
  // последнее с сервера, если оно новее.
  const justSaved = save.kind === 'saved' && (!latest || latest.revision <= save.decision.revision) ? save.decision : null;
  const shown = justSaved ?? latest;
  const formOpen = editing || (!shown && !decisions.loading);

  if (!can('decide')) {
    return (
      <div className="decision-panel">
        <h2>Решение</h2>
        {latest ? <DecisionSummary decision={latest} /> : <p className="muted">Решения пока нет.</p>}
        <StateMessage compact kind="denied" title="Решение записывают другие роли">
          Доступно: {rolesAllowed('decide')}. Ваша роль: {roles.map((role) => ROLE_LABELS[role]).join(', ')}.
        </StateMessage>
      </div>
    );
  }

  const submit = async () => {
    setTouched(true);
    if (problems.length || !option || !reason) return;
    const notify = asksNotify;
    const payloadBase: Omit<ForecastDecisionCreate, 'idempotency_key'> = {
      decision_code: option.code,
      reason_code: reason.code,
      reason_text: reasonText.slice(0, 300),
      verification_methods: values.methods,
      expected_revision: seenRevision ?? latest?.revision ?? 0,
      draft_note: option.draft && values.draftNote.trim() ? values.draftNote.trim().slice(0, 500) : null,
      notified_to: notify ? values.notifiedTo.trim().slice(0, 200) : null,
      notified_at: notify ? mskLocalToIso(values.notifiedAt) : null,
      awaiting_result_until: notify ? mskLocalToIso(values.awaitingUntil) : null,
      watch_until: asksWatch ? mskLocalToIso(values.watchUntil) : null,
    };
    const body = JSON.stringify(payloadBase);
    // Повтор того же ввода после сбоя сети идёт с тем же ключом — сервер не запишет дубль.
    if (!attempt.current || attempt.current.body !== body) attempt.current = { key: newIdempotencyKey(), body };
    setSave({ kind: 'saving' });
    try {
      const saved = await createDecision(forecastId, { ...payloadBase, idempotency_key: attempt.current.key });
      attempt.current = null;
      rememberDraftText(forecastId, saved.revision, saved.draft_id ? payloadBase.draft_note : null);
      setSave({ kind: 'saved', decision: saved });
      setValues(EMPTY);
      setTouched(false);
      setEditing(false);
      drafts.delete(forecastId);
      onSaved?.(saved);
      if (!saved.simulated) {
        setSeenRevision(saved.revision);
        decisions.reload();
      }
    } catch (error) {
      setSave({ kind: 'error', error });
      if (error instanceof ApiError && error.status === 409) decisions.reload();
    }
  };

  const errorText = (error: unknown): string => {
    if (!(error instanceof ApiError)) return describeApiError(error);
    if (error.kind === 'network') return 'Нет связи с сервером. Введённый текст сохранён; повтор безопасен — отправится тот же запрос.';
    if (error.status === 409) {
      return 'Пока форма была открыта, решение по карточке изменили. Ваш текст сохранён: посмотрите новое решение выше и сохраните снова.';
    }
    if (error.status === 403) return 'Нет права записывать решение для вашей роли. Текст сохранён.';
    if (error.detail === 'deadline_before_decision') {
      return 'Срок («ждём результат до» или «под наблюдением до») должен быть позже времени решения. Исправьте дату — остальной ввод сохранён.';
    }
    if (error.detail === 'notified_after_decision') {
      return 'Время уведомления позже времени решения на сервере. Проверьте «когда сообщено» — ввод сохранён.';
    }
    if (error.status === 503) return `${describeApiError(error)}. Введённый текст сохранён в этой вкладке.`;
    return `${describeApiError(error)}. Введённый текст сохранён.`;
  };

  const conflict = save.kind === 'error' && save.error instanceof ApiError && save.error.status === 409;

  return (
    <div className="decision-panel">
      <h2>Решение</h2>
      {decisions.loading ? <StateMessage compact kind="loading" title="Загрузка решений…" /> : null}
      {decisions.error && !decisions.data ? (
        <StateMessage compact kind="error" title="Решения не загружены" error={decisions.error} onRetry={decisions.reload}>
          Сохранение возможно; сервер проверит ревизию.
        </StateMessage>
      ) : null}
      {shown ? (
        <div className={justSaved && !formOpen ? 'decision-panel__saved' : 'decision-panel__current'} role={justSaved && !formOpen ? 'status' : undefined}>
          {justSaved && !formOpen ? (
            <span className="decision-panel__saved-head">
              <strong>{justSaved.simulated ? DEMO_DECISION_NOT_SAVED : 'Решение сохранено'}</strong>
              <span className="muted small">
                {justSaved.simulated
                  ? DEMO_NOT_SAVED_NOTE
                  : `${justSaved.actor_id}, ${fmtDateTime(justSaved.decided_at)}; записано в историю решений карточки.`}
              </span>
            </span>
          ) : <span className="muted small">Текущее решение</span>}
          <DecisionSummary decision={shown} compact draftText={draftTextFor(forecastId, shown.revision)} />
        </div>
      ) : decisions.data ? <p className="muted">Решения по карточке пока нет.</p> : null}
      {listState && listState !== 'open' ? (
        <p className="muted small">Карточка уже не в списке ({listState === 'released' ? 'снята по событию' : 'срок истёк'}); решение можно дописать в журнал.</p>
      ) : null}

      {!formOpen ? (
        shown ? (
          <Button className="decision-panel__edit" icon={<IconPencil size={16} />} onClick={() => setEditing(true)}>
            Изменить решение
          </Button>
        ) : null
      ) : (
        <form className="decision-form" onSubmit={(event) => { event.preventDefault(); void submit(); }} noValidate>
          <fieldset>
            <legend>Действие</legend>
            <Radio.Group
              value={option?.primary ? option.code : option ? 'other' : null}
              onChange={(event) => {
                const value = event.target.value as DecisionCode | 'other';
                if (value === 'other') patch({ code: values.other ?? OTHER[0].code, reason: null });
                else patch({ code: value, reason: null });
              }}
              className="decision-form__codes"
            >
              {PRIMARY.map((item) => (
                <Radio.Button key={item.code} value={item.code}>{item.label}</Radio.Button>
              ))}
              <Radio.Button value="other">Другое</Radio.Button>
            </Radio.Group>
            {option && !option.primary ? (
              <Select<DecisionCode>
                className="decision-form__other"
                value={option.code}
                onChange={(value) => patch({ code: value, other: value, reason: null })}
                options={OTHER.map((item) => ({ value: item.code, label: `${item.label}${item.draft ? ' (черновик)' : ''}` }))}
                aria-label="Другое действие"
              />
            ) : null}
            {recommendedCode ? (
              <p className="muted small">Рекомендуемое действие: {decisionLabel(recommendedCode)} — выбор за диспетчером.</p>
            ) : null}
          </fieldset>

          <label className="decision-form__field">
            <span>Основание</span>
            <Select<string>
              value={values.reason ?? undefined}
              onChange={(value) => patch({ reason: value })}
              placeholder={option ? 'Выберите основание' : 'Сначала выберите действие'}
              disabled={!option}
              options={(option?.reasons ?? []).map((item) => ({ value: item.code, label: item.text }))}
              aria-label="Основание"
            />
            <Input.TextArea rows={2} value={values.comment} maxLength={commentMax} showCount placeholder="Уточнение основания (необязательно)"
              onChange={(event) => patch({ comment: event.target.value })} aria-label="Уточнение основания" />
          </label>

          <label className="decision-form__field">
            <span>Как проверено</span>
            <Select<VerificationMethod[]>
              mode="multiple"
              value={values.methods}
              onChange={(checked) => patch({ methods: checked })}
              placeholder="Выберите один или несколько способов"
              options={Object.entries(VERIFICATION_LABELS).map(([value, label]) => ({ value: value as VerificationMethod, label }))}
              aria-label="Как проверено"
              className="decision-form__methods"
            />
          </label>

          {asksNotify ? (
            <fieldset>
              <legend>Кому и когда сообщено</legend>
              <div className="decision-form__notify">
                <Input value={values.notifiedTo} maxLength={200}
                  placeholder="например, дежурный инженер или энергетик эксплуатирующей организации"
                  onChange={(event) => patch({ notifiedTo: event.target.value })} aria-label="Кому сообщено" />
                <Input type="datetime-local" value={values.notifiedAt} max={nowLocal}
                  onChange={(event) => patch({ notifiedAt: event.target.value })} aria-label="Когда сообщено, МСК" />
              </div>
              <label className="decision-form__field">
                <span className="decision-form__sublabel">Ждём результат до, МСК</span>
                <Input type="datetime-local" value={values.awaitingUntil} min={values.notifiedAt || undefined}
                  onChange={(event) => patch({ awaitingUntil: event.target.value })} aria-label="Ждём результат до, МСК" />
              </label>
            </fieldset>
          ) : null}

          {asksWatch ? (
            <label className="decision-form__field">
              <span>Под наблюдением до, МСК</span>
              <Input type="datetime-local" value={values.watchUntil} min={nowLocal}
                onChange={(event) => patch({ watchUntil: event.target.value })} aria-label="Под наблюдением до, МСК" />
            </label>
          ) : null}

          {option?.draft ? (
            <div className="decision-form__field">
              <span className="decision-form__draft-head">
                <span>Черновик для энергетика <span className="text-label text-label--draft">{DRAFT_NOT_SENT}</span></span>
                {recommendationText && !values.draftNote.trim() ? (
                  <Button type="link" size="small" onClick={() => patch({ draftNote: recommendationText.slice(0, 500) })}>
                    {DRAFT_FROM_RECOMMENDATION}
                  </Button>
                ) : null}
              </span>
              <Input.TextArea rows={3} value={values.draftNote} maxLength={500} showCount
                onChange={(event) => patch({ draftNote: event.target.value })} aria-label="Текст черновика" />
              <span className="muted small">Черновик сохраняется вместе с решением и никуда не отправляется.</span>
            </div>
          ) : null}

          {touched && problems.length ? (
            <p className="decision-form__problems" role="alert">Чтобы сохранить: {problems.join('; ')}.</p>
          ) : null}

          <div className="decision-form__actions">
            <Button type="primary" htmlType="submit" icon={<IconDeviceFloppy size={18} />} loading={save.kind === 'saving'}>
              {shown ? 'Сохранить новое решение' : 'Сохранить решение'}
            </Button>
            {shown ? (
              <Button onClick={() => {
                setValues(EMPTY);
                setTouched(false);
                setEditing(false);
                drafts.delete(forecastId);
                if (save.kind === 'error') setSave({ kind: 'idle' });
              }}>Отмена</Button>
            ) : null}
          </div>
          <p className="muted small">Решение не снимает карточку и не меняет исходные сообщения.</p>
        </form>
      )}

      {save.kind === 'error' ? (
        <StateMessage compact kind="error" title="Решение не сохранено"
          action={conflict && latest ? (
            <Button onClick={() => { setSeenRevision(latest.revision); setSave({ kind: 'idle' }); }}>
              Учесть решение ревизии {latest.revision}
            </Button>
          ) : undefined}>
          {errorText(save.error)}{errorCode(save.error) ? <span className="small"> · {errorCode(save.error)}</span> : null}
        </StateMessage>
      ) : null}
    </div>
  );
}
