import { useEffect, useState } from 'react';
import { Alert, Button, Flex, Input, List, Spin, Typography } from 'antd';
import { addReview, getReviews, type ReviewNoteCreate, type ReviewNoteList } from '../../api/attention';
import { EMPTY_REVIEW_DRAFT, type ReviewDraft } from '../../providers/ReviewDraftsProvider';
import { storedAttentionReasons } from '../../shared/lib/attention-reasons';

const { Paragraph, Text } = Typography;

interface ReviewPanelProps {
  rowUid: string;
  asOf: string;
  receivedWatermark: number | null;
  snapshotId: string;
  policyVersion: string;
  enabled: boolean;
  mode: 'replay' | 'received';
  draft: ReviewDraft;
  onDraftChange: (draft: ReviewDraft) => void;
  onSaved?: () => void;
}

export function ReviewPanel({
  rowUid, asOf, receivedWatermark, snapshotId, policyVersion,
  enabled, mode, draft, onDraftChange, onSaved,
}: ReviewPanelProps) {
  const [history, setHistory] = useState<ReviewNoteList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setLoading(true);
    getReviews(rowUid, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setHistory(value);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setHistory(null);
        setError(cause instanceof Error ? cause.message : 'Не удалось прочитать журнал');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [enabled, revision, rowUid]);

  if (!enabled) {
    return <Alert type="warning" showIcon message="Серверное сохранение решений отключено" />;
  }

  const update = (field: 'actionText' | 'resultText' | 'reasonText', value: string) => {
    onDraftChange({ ...draft, [field]: value, pendingPayload: undefined });
  };

  const save = async () => {
    if (!history || !draft.actionText.trim() || !draft.resultText.trim() || !draft.reasonText.trim()) {
      setError('Заполните действие, результат и основание после проверки записи.');
      return;
    }
    if (mode === 'received' && receivedWatermark === null && !draft.pendingPayload) {
      setError('Ответ API не содержит границу принятого списка; сохранение остановлено.');
      return;
    }
    const payload: ReviewNoteCreate = draft.pendingPayload ?? {
      idempotency_key: crypto.randomUUID(),
      expected_revision: history.revision,
      view_as_of: asOf,
      displayed_received_watermark: mode === 'received' ? receivedWatermark : null,
      displayed_snapshot_id: snapshotId,
      displayed_policy_version: policyVersion,
      action_text: draft.actionText.trim(),
      result_text: draft.resultText.trim(),
      reason_text: draft.reasonText.trim(),
    };
    onDraftChange({ ...draft, pendingPayload: payload });
    setSaving(true);
    setError(null);
    try {
      await addReview(rowUid, payload);
      onDraftChange(EMPTY_REVIEW_DRAFT);
      setRevision((value) => value + 1);
      onSaved?.();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : 'Не удалось сохранить результат';
      setError(
        message.includes('409')
          ? 'Запись изменилась или запрос отличается от повторного. История обновлена; проверьте её и отправьте снова.'
          : `Сохранение не подтверждено (${message}). Повтор использует тот же запрос.`,
      );
      if (message.includes('409')) {
        onDraftChange({ ...draft, pendingPayload: undefined });
        setRevision((value) => value + 1);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ marginTop: 20 }}>
      <Text strong>Результат локальной проверки</Text>
      <Paragraph type="secondary">
        {mode === 'replay' ? 'Исторический replay.' : 'Локально импортированные сообщения.'} Автор «local-operator» не подтверждает личность диспетчера;
        заметка не снимает исходный alarm и не отправляет заявку.
      </Paragraph>
      {(draft.actionText || draft.resultText || draft.reasonText) && (
        <Paragraph type="secondary">
          Несохранённый ввод сохраняется при переходах по экранам этой вкладки,
          но исчезнет после её перезагрузки. В серверном журнале его пока нет.
        </Paragraph>
      )}
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      {draft.pendingPayload && (draft.pendingPayload.view_as_of !== asOf ||
        (mode === 'received' && draft.pendingPayload.displayed_received_watermark !== receivedWatermark)) && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="Повтор относится к ранее показанному списку"
          description="Измените текст заметки, чтобы подготовить новый запрос для текущего момента."
        />
      )}
      {loading && <Spin size="small" />}
      <Flex vertical gap={8}>
        <Input.TextArea
          aria-label="Действие"
          placeholder="Что проверили или кому передали"
          maxLength={500}
          value={draft.actionText}
          onChange={(event) => update('actionText', event.target.value)}
        />
        <Input.TextArea
          aria-label="Результат"
          placeholder="Что установлено, например «ничего не обнаружено»"
          maxLength={1000}
          value={draft.resultText}
          onChange={(event) => update('resultText', event.target.value)}
        />
        <Input.TextArea
          aria-label="Основание"
          placeholder="На основании чего сделан вывод"
          maxLength={1000}
          value={draft.reasonText}
          onChange={(event) => update('reasonText', event.target.value)}
        />
        <Button type="primary" loading={saving} disabled={!history || loading} onClick={save}>
          Сохранить в локальный журнал
        </Button>
      </Flex>
      <Text strong style={{ display: 'block', marginTop: 18 }}>
        История записи · ревизия {history?.revision ?? '—'}
      </Text>
      <List
        size="small"
        dataSource={history?.items ?? []}
        locale={{ emptyText: 'Сохранённых результатов нет' }}
        renderItem={(item) => (
          <List.Item key={item.note_id}>
            <div>
              <Text strong>#{item.revision} · {new Date(item.created_at).toLocaleString('ru-RU', {
                timeZone: 'Europe/Moscow',
              })} МСК</Text>
              <br /><Text>Действие: {item.action_text}</Text>
              <br /><Text>Результат: {item.result_text}</Text>
              <br /><Text type="secondary">Основание: {item.reason_text}</Text>
              <br /><Text type="secondary">Показано в очереди: {item.view_as_of
                ? `${new Date(item.view_as_of).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' })} МСК${item.displayed_received_watermark == null ? '' : ` · срез до записи №${item.displayed_received_watermark}`} · ${item.policy_version} · ${item.attention_band}`
                : 'не сохранено в ранней тестовой заметке'}</Text>
              <br /><Text type="secondary">Основание порядка при проверке: {storedAttentionReasons(item.reason_codes)}</Text>
            </div>
          </List.Item>
        )}
      />
    </div>
  );
}
