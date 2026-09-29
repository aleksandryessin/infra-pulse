import { useEffect, useState } from 'react';
import { alarmFlagMissing, alarmFlagText } from '../../shared/lib/alarm-flag';
import { Alert, Button, Card, Empty, Flex, List, Segmented, Space, Spin, Tag, Typography } from 'antd';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  getAttention,
  getCapabilities,
  getReviewJournal,
  type AttentionList,
  type Capabilities,
  type ReviewJournalList,
} from '../../api/attention';
import { PageHeading } from '../../widgets/page-heading/PageHeading';
import { objectBindingEvidence } from '../../shared/lib/object-binding';
import { storedAttentionReasons } from '../../shared/lib/attention-reasons';
import { linkedReplayAsOf } from '../../shared/lib/replay-time';
import { linkedReceivedSnapshot } from '../../shared/lib/received-snapshot';
import { useActiveRefresh } from '../../shared/lib/use-active-refresh';

const { Paragraph, Text } = Typography;
const PAGE_SIZE = 25;

interface ReceivedPageSnapshot {
  asOf: string;
  watermark: number;
}

function inMoscow(value: string): string {
  return new Intl.DateTimeFormat('ru-RU', {
    dateStyle: 'short',
    timeStyle: 'medium',
    timeZone: 'Europe/Moscow',
  }).format(new Date(value));
}

export function ReplayJournalPage() {
  const received = import.meta.env.VITE_DATA_MODE === 'received';
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const rowUid = params.get('row_uid');
  const objectId = params.get('object');
  const channelId = params.get('channel');
  const requestedAsOf = params.get('as_of');
  const requestedWatermark = params.get('received_watermark');
  const linkedReceived = received
    ? linkedReceivedSnapshot(requestedAsOf, requestedWatermark) : null;
  const journalView = params.get('tab') === 'messages' ? 'messages' : 'notes';
  const alarmOnly = params.get('alarm') === 'true';
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [data, setData] = useState<ReviewJournalList | null>(null);
  const [sourceData, setSourceData] = useState<AttentionList | null>(null);
  const [offset, setOffset] = useState(0);
  const [receivedPageSnapshot, setReceivedPageSnapshot] = useState<ReceivedPageSnapshot | null>(null);
  const activeReceivedSnapshot = receivedPageSnapshot ?? linkedReceived;
  const [notePageWatermark, setNotePageWatermark] = useState<number | null>(null);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setOffset(0), [rowUid, objectId, channelId, journalView, alarmOnly]);
  useEffect(() => setReceivedPageSnapshot(null), [rowUid, objectId, channelId, requestedAsOf, requestedWatermark]);
  useEffect(() => setNotePageWatermark(null), [rowUid, objectId, channelId]);

  useActiveRefresh(received && journalView === 'messages' && !activeReceivedSnapshot, setRevision);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setData(null);
    setSourceData(null);
    getCapabilities(controller.signal)
      .then(async (value) => {
        if (controller.signal.aborted) return null;
        if (value.mode !== (received ? 'received' : 'replay') || !value.observations_ready) {
          throw new Error('Режим данных в API не готов');
        }
        setCapabilities(value);
        if (journalView === 'notes') {
          if (!value.local_reviews_enabled) return null;
          return { kind: 'notes' as const, page: await getReviewJournal(offset, PAGE_SIZE, {
            rowUid: rowUid ?? undefined,
            objectId: objectId ?? undefined,
            channelId: channelId ?? undefined,
          }, controller.signal, notePageWatermark ?? undefined) };
        }
        if (!received && (!value.replay_window_start || !value.replay_window_end)) {
          throw new Error('Окно replay недоступно');
        }
        const asOf = received ? activeReceivedSnapshot?.asOf
          : linkedReplayAsOf(requestedAsOf, value.replay_window_start!, value.replay_window_end!)
            ?? value.replay_window_end!;
        return { kind: 'messages' as const, page: await getAttention(
          asOf,
          offset,
          PAGE_SIZE,
          objectId ?? undefined,
          'all',
          channelId ?? undefined,
          rowUid ?? undefined,
          'any',
          alarmOnly ? true : undefined,
          controller.signal,
          activeReceivedSnapshot?.watermark,
        ) };
      })
      .then((value) => {
        if (controller.signal.aborted) return;
        if (value?.kind === 'notes') setData(value.page);
        if (value?.kind === 'messages') setSourceData(value.page);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setData(null);
        setSourceData(null);
        setCapabilities(null);
        setError(cause instanceof Error ? cause.message : 'Не удалось прочитать журнал');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [alarmOnly, channelId, journalView, notePageWatermark, objectId, offset, received,
    activeReceivedSnapshot?.asOf, activeReceivedSnapshot?.watermark, requestedAsOf, requestedWatermark, revision, rowUid]);

  return (
    <>
      <PageHeading
        title={received ? 'Журнал · локально полученные сообщения' : 'Журнал · исторический replay'}
        description="Исходные сообщения и сохранённые результаты локальных проверок."
      />
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={journalView === 'messages'
          ? 'Исходные сообщения доступны независимо от локальных заметок'
          : received
            ? 'Время заметки и локального импорта сохраняются отдельно от времени источника'
            : 'Время заметки — настоящее локальное время, время сообщения — историческое'}
        description={journalView === 'messages'
          ? `alarm=true — поле источника, не подтверждённый физический отказ. Сообщения без заметок тоже сохраняются. ${received
            ? 'Время локального импорта отделено от времени источника.'
            : 'В replay время доступности симулировано.'}`
          : 'Автор local-operator не подтверждает личность диспетчера. Заметки не снимают исходный alarm и не отправляют заявки.'}
      />
      <Flex justify="space-between" align="center" wrap gap={12} style={{ marginBottom: 16 }}>
        <Segmented
          aria-label="Раздел журнала"
          value={journalView}
          options={[
            { value: 'notes', label: 'Локальные заметки' },
            { value: 'messages', label: 'Исходные сообщения' },
          ]}
          onChange={(value) => {
            setOffset(0);
            setParams((current) => {
              const next = new URLSearchParams(current);
              if (value === 'messages') next.set('tab', 'messages');
              else {
                next.delete('tab');
                next.delete('alarm');
              }
              return next;
            });
          }}
        />
        <Button onClick={() => {
          if (journalView === 'messages' && activeReceivedSnapshot) {
            setOffset(0);
            setReceivedPageSnapshot(null);
            setRevision((value) => value + 1);
            if (linkedReceived) setParams((current) => {
              const next = new URLSearchParams(current);
              next.delete('as_of');
              next.delete('received_watermark');
              next.delete('row_uid');
              return next;
            });
          } else if (journalView === 'notes' && notePageWatermark !== null) {
            setOffset(0);
            setNotePageWatermark(null);
          } else setRevision((value) => value + 1);
        }}>{journalView === 'messages' && activeReceivedSnapshot
          ? 'К новым сообщениям'
          : journalView === 'notes' && notePageWatermark !== null
            ? 'К новым заметкам'
            : 'Обновить'}</Button>
      </Flex>
      {journalView === 'messages' && !received && requestedAsOf &&
        capabilities?.replay_window_start && capabilities.replay_window_end &&
        !linkedReplayAsOf(requestedAsOf, capabilities.replay_window_start, capabilities.replay_window_end) && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="Момент из ссылки вне окна replay"
            description="Исходные сообщения показаны на конец загруженного окна."
          />
        )}
      {journalView === 'messages' && received &&
        (requestedAsOf !== null || requestedWatermark !== null) && !linkedReceived && (
          <Alert type="warning" showIcon style={{ marginBottom: 16 }}
            message="Неполная ссылка на срез сообщений"
            description="Показан актуальный список. Для закрепления нужны время и номер принятой записи." />
        )}
      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="Журнал недоступен"
          description={error}
          action={<Button onClick={() => setRevision((value) => value + 1)}>Повторить</Button>}
        />
      )}
      {journalView === 'notes' && capabilities && !capabilities.local_reviews_enabled && !loading && (
        <Alert
          type="warning"
          showIcon
          message="Локальное сохранение проверок выключено на сервере"
          action={<Button onClick={() => setParams((current) => {
            const next = new URLSearchParams(current);
            next.set('tab', 'messages');
            return next;
          })}>Исходные сообщения</Button>}
        />
      )}
      {loading && <Spin tip="Читаем журнал"><div style={{ height: 80 }} /></Spin>}
      {data && !loading && (
        <Card
          title={<>Сохранённые заметки <Tag>{data.total}</Tag></>}
          extra={(rowUid || objectId || channelId) && <Button onClick={() => {
            setOffset(0);
            setParams((current) => {
              const next = new URLSearchParams(current);
              next.delete('row_uid');
              next.delete('object');
              next.delete('channel');
              return next;
            });
          }}>Все заметки</Button>}
        >
          {notePageWatermark !== null && <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="Страницы заметок зафиксированы"
            description="Новые сохранённые результаты не сдвигают этот список. Чтобы увидеть их, нажмите «К новым заметкам»."
          />}
          {(rowUid || objectId || channelId) && <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
            Фильтр: {[
              rowUid ? `исходная запись ${rowUid.slice(0, 12)}…` : null,
              objectId === '__unknown__' ? 'без привязки к объекту' : objectId ? `объект ${objectId}` : null,
              channelId ? `канал ${channelId}` : null,
            ].filter(Boolean).join(' · ')}. Показаны сохранённые заметки всего активного {received ? 'потока' : 'snapshot'}.
          </Text>}
          {data.items.length === 0 ? <Empty description={rowUid
            ? 'У этой записи пока нет сохранённых заметок'
            : objectId || channelId
              ? 'По выбранному объекту или каналу пока нет сохранённых заметок'
              : 'В этом срезе пока нет сохранённых заметок'} /> : (
            <List
              dataSource={data.items}
              renderItem={({ note, message }) => (
                <List.Item key={note.note_id}>
                  <div style={{ width: '100%' }}>
                    <Flex justify="space-between" align="start" wrap gap={8}>
                      <Text strong>{message.sensor_type ?? 'Тип неизвестен'} · канал {message.channel_id}</Text>
                      <Tag color={message.alarm ? 'error' : 'default'} className={alarmFlagMissing(message.alarm) ? 'tag--muted' : undefined}>{alarmFlagText(message.alarm)}</Tag>
                    </Flex>
                    <Paragraph style={{ marginBottom: 4 }}>
                      Объект: {message.object_id ?? 'привязка неизвестна'} · Система: {message.system_type ?? 'неизвестна'}
                    </Paragraph>
                    <Paragraph type="secondary" style={{ marginBottom: 4 }}>
                      {objectBindingEvidence(message, received)}
                    </Paragraph>
                    <Paragraph style={{ marginBottom: 4 }}>
                      Исходный текст: <Text code>{JSON.stringify(message.value_raw)}</Text>
                    </Paragraph>
                    <Paragraph type="secondary" style={{ marginBottom: 4 }}>
                      {received ? 'Локальный импорт' : 'Доступно в replay (симулировано)'}: {inMoscow(message.available_at)} МСК
                      {' '}· время источника: {inMoscow(message.event_at)} МСК
                      {' '}· заметка: {inMoscow(note.created_at)} МСК · ревизия {note.revision}
                    </Paragraph>
                    <Paragraph style={{ marginBottom: 4 }}>Действие: {note.action_text}</Paragraph>
                    <Paragraph style={{ marginBottom: 4 }}>Результат: {note.result_text}</Paragraph>
                    <Paragraph type="secondary" style={{ marginBottom: 0 }}>
                      Основание: {note.reason_text} · автор: {note.actor_id} · запись: {message.row_uid.slice(0, 12)}…
                    </Paragraph>
                    <Paragraph type="secondary" style={{ marginBottom: 0 }}>
                      Показано в очереди: {note.view_as_of
                        ? `${inMoscow(note.view_as_of)} МСК${note.displayed_received_watermark == null ? '' : ` · срез до записи №${note.displayed_received_watermark}`} · ${note.policy_version} · ${note.attention_band}`
                        : 'не сохранено в ранней тестовой заметке'}
                    </Paragraph>
                    <Paragraph type="secondary" style={{ marginBottom: 0 }}>
                      Основание порядка при проверке: {storedAttentionReasons(note.reason_codes)}
                    </Paragraph>
                    <Button type="link" style={{ paddingLeft: 0 }} onClick={() => {
                      const query = new URLSearchParams({ view: 'all', row_uid: message.row_uid });
                      if (!received) query.set('as_of', note.view_as_of ?? message.available_at);
                      else if (note.view_as_of && note.displayed_received_watermark != null) {
                        query.set('as_of', note.view_as_of);
                        query.set('received_watermark', String(note.displayed_received_watermark));
                      }
                      navigate(`/queue?${query}`);
                    }}>Открыть исходную запись</Button>
                  </div>
                </List.Item>
              )}
            />
          )}
          <Space style={{ marginTop: 12 }}>
            <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Назад</Button>
            <Text>{data.total === 0 ? 0 : offset + 1}–{offset + data.items.length} из {data.total}</Text>
            <Button disabled={offset + PAGE_SIZE >= data.total} onClick={() => {
              if (notePageWatermark === null) setNotePageWatermark(data.review_watermark);
              setOffset(offset + PAGE_SIZE);
            }}>Далее</Button>
          </Space>
        </Card>
      )}
      {sourceData && !loading && (
        <Card
          title={<>Исходные сообщения <Tag>{sourceData.total}</Tag></>}
          extra={(rowUid || objectId || channelId) && <Button onClick={() => {
            setOffset(0);
            setParams((current) => {
              const next = new URLSearchParams(current);
              next.delete('row_uid');
              next.delete('object');
              next.delete('channel');
              return next;
            });
          }}>Сбросить фильтр записи</Button>}
        >
          <Flex justify="space-between" align="center" wrap gap={12} style={{ marginBottom: 12 }}>
            <Segmented
              aria-label="Фильтр исходных сообщений по alarm"
              value={alarmOnly ? 'alarm' : 'all'}
              options={[
                { value: 'all', label: 'Все сообщения' },
                { value: 'alarm', label: 'alarm=true' },
              ]}
              onChange={(value) => {
                setOffset(0);
                setParams((current) => {
                  const next = new URLSearchParams(current);
                  if (value === 'alarm') next.set('alarm', 'true');
                  else next.delete('alarm');
                  return next;
                });
              }}
            />
            <Text type="secondary">На {inMoscow(sourceData.as_of)} МСК · {received
              ? activeReceivedSnapshot ? 'фиксированный список' : 'локальный импорт'
              : 'исторический срез'}</Text>
          </Flex>
          {activeReceivedSnapshot && <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="Исходные сообщения зафиксированы на выбранном моменте"
            description={`Новые партии не меняют этот список. ${capabilities?.received_rows != null && capabilities.received_rows > activeReceivedSnapshot.watermark
              ? 'После этого момента был новый локальный импорт.' : 'Чтобы увидеть новые сообщения, нажмите «К новым сообщениям».'}`}
          />}
          {(rowUid || objectId || channelId) && <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
            Фильтр: {[
              rowUid ? `исходная запись ${rowUid.slice(0, 12)}…` : null,
              objectId === '__unknown__' ? 'без привязки к объекту' : objectId ? `объект ${objectId}` : null,
              channelId ? `канал ${channelId}` : null,
            ].filter(Boolean).join(' · ')}.
          </Text>}
          {sourceData.items.length === 0 ? <Empty description={rowUid
            ? 'Исходная запись недоступна к выбранному моменту или не проходит фильтр'
            : alarmOnly
              ? 'Исходных сообщений с alarm=true в выбранном срезе нет'
              : 'Исходных сообщений в выбранном срезе нет'} /> : (
            <List
              dataSource={sourceData.items}
              renderItem={({ message, local_review_revision }) => (
                <List.Item key={message.row_uid}>
                  <div style={{ width: '100%' }}>
                    <Flex justify="space-between" align="start" wrap gap={8}>
                      <Text strong>{message.sensor_type ?? 'Тип неизвестен'} · канал {message.channel_id}</Text>
                      <Tag color={message.alarm ? 'error' : 'default'} className={alarmFlagMissing(message.alarm) ? 'tag--muted' : undefined}>{alarmFlagText(message.alarm)}</Tag>
                    </Flex>
                    <Paragraph style={{ marginBottom: 4 }}>
                      Объект: {message.object_id ?? 'привязка неизвестна'} · Система: {message.system_type ?? 'неизвестна'}
                    </Paragraph>
                    <Paragraph type="secondary" style={{ marginBottom: 4 }}>
                      {objectBindingEvidence(message, received)}
                    </Paragraph>
                    <Paragraph style={{ marginBottom: 4 }}>
                      Исходный текст: <Text code>{JSON.stringify(message.value_raw)}</Text>
                    </Paragraph>
                    <Paragraph type="secondary" style={{ marginBottom: 4 }}>
                      {received ? 'Локальный импорт' : 'Доступно в replay (симулировано)'}: {inMoscow(message.available_at)} МСК
                      {' '}· время источника: {inMoscow(message.event_at)} МСК
                      {' '}· локальная заметка: {local_review_revision > 0 ? `ревизия ${local_review_revision}` : 'нет'}
                    </Paragraph>
                    <Button type="link" style={{ paddingLeft: 0 }} onClick={() => {
                      const query = new URLSearchParams({ view: 'all', row_uid: message.row_uid });
                      query.set('as_of', sourceData.as_of);
                      if (received && sourceData.received_watermark != null) {
                        query.set('received_watermark', String(sourceData.received_watermark));
                      }
                      navigate(`/queue?${query}`);
                    }}>Открыть исходную карточку</Button>
                  </div>
                </List.Item>
              )}
            />
          )}
          <Space style={{ marginTop: 12 }}>
            <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Назад</Button>
            <Text>{sourceData.total === 0 ? 0 : offset + 1}–{offset + sourceData.items.length} из {sourceData.total}</Text>
            <Button disabled={offset + PAGE_SIZE >= sourceData.total} onClick={() => {
              if (received && !activeReceivedSnapshot) {
                if (sourceData.received_watermark == null) {
                  setError('Ответ API не содержит границу принятого импорта');
                  return;
                }
                setReceivedPageSnapshot({
                  asOf: sourceData.as_of,
                  watermark: sourceData.received_watermark,
                });
              }
              setOffset(offset + PAGE_SIZE);
            }}>Далее</Button>
          </Space>
        </Card>
      )}
    </>
  );
}
