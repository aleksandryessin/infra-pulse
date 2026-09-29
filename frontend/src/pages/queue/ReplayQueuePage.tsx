import { useEffect, useMemo, useState } from 'react';
import { alarmFlagMissing, alarmFlagText } from '../../shared/lib/alarm-flag';
import { Alert, Button, Card, Flex, List, Segmented, Slider, Space, Spin, Tag, Typography } from 'antd';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  getAttention,
  getCapabilities,
  getObjectAttention,
  type AttentionEntry,
  type AttentionList,
  type Capabilities,
  type ObjectAttentionList,
} from '../../api/attention';
import { PageHeading } from '../../widgets/page-heading/PageHeading';
import { linkedReplayAsOf } from '../../shared/lib/replay-time';
import { linkedReceivedSnapshot } from '../../shared/lib/received-snapshot';
import { elapsedSinceAt, sourceImportTimeDifference } from '../../shared/lib/observation-age';
import { objectBindingEvidence } from '../../shared/lib/object-binding';
import { qualityFlagLabel } from '../../shared/lib/quality-flags';
import { useActiveRefresh } from '../../shared/lib/use-active-refresh';
import { EMPTY_REVIEW_DRAFT, useReviewDrafts } from '../../providers/ReviewDraftsProvider';
import { ReviewPanel } from '../../widgets/review-panel/ReviewPanel';

const { Text, Title, Paragraph } = Typography;
const PAGE_SIZE = 25;

interface QueueSelection {
  entry: AttentionEntry;
  scopeKey: string;
  checkedAsOf: string;
  checkedWatermark: number | null;
}

interface ReceivedPageSnapshot {
  asOf: string;
  watermark: number;
}

interface ChannelContextState {
  scopeKey: string;
  data: AttentionList | null;
  loading: boolean;
  error: boolean;
}

interface CandidateLanesState {
  scopeKey: string;
  alarm: AttentionList | null;
  text: AttentionList | null;
  objects: ObjectAttentionList | null;
  loading: boolean;
  error: boolean;
}

function inMoscow(value: string): string {
  return new Intl.DateTimeFormat('ru-RU', {
    dateStyle: 'short',
    timeStyle: 'medium',
    timeZone: 'Europe/Moscow',
  }).format(new Date(value));
}

function reason(entry: AttentionEntry): string {
  if (entry.attention_band === 'source_alarm') {
    return 'Исходное поле «тревожное» = true; предварительный порядок проверки';
  }
  if (entry.attention_band === 'watch_text') {
    return 'Точный текст и тип из каталога кандидатов; приоритет не утверждён';
  }
  return 'Порядок по времени доступности; приоритет не утверждён';
}

function reviewDraftKey(entry: AttentionEntry): string {
  return JSON.stringify([
    entry.message.source_namespace,
    entry.message.snapshot_id,
    entry.message.row_uid,
  ]);
}

function recordCount(value: number): string {
  const lastTwo = value % 100;
  const last = value % 10;
  const word = lastTwo >= 11 && lastTwo <= 14 ? 'записей'
    : last === 1 ? 'запись' : last >= 2 && last <= 4 ? 'записи' : 'записей';
  return `${value} ${word}`;
}

export function ReplayQueuePage() {
  const received = import.meta.env.VITE_DATA_MODE === 'received';
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const objectId = params.get('object') ?? undefined;
  const channelId = params.get('channel') ?? undefined;
  const rowUid = params.get('row_uid') ?? undefined;
  const requestedAsOf = params.get('as_of');
  const requestedWatermark = params.get('received_watermark');
  const requestedAfterWatermark = params.get('after_received_watermark');
  const afterWatermarkNumber = requestedAfterWatermark === null ? NaN : Number(requestedAfterWatermark);
  const afterReceivedWatermark = received && requestedAfterWatermark !== null &&
    /^\d+$/.test(requestedAfterWatermark) && Number.isSafeInteger(afterWatermarkNumber)
    ? afterWatermarkNumber : null;
  const view = params.get('view') === 'all' ? 'all' : 'attention';
  const newSourceAlarmsOnly = afterReceivedWatermark !== null && view === 'all';
  const newCandidatesOnly = afterReceivedWatermark !== null && view === 'attention';
  const linkedReceived = received
    ? linkedReceivedSnapshot(requestedAsOf, requestedWatermark) : null;
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [data, setData] = useState<AttentionList | null>(null);
  const [channelContextState, setChannelContextState] = useState<ChannelContextState | null>(null);
  const [sameTimeContextState, setSameTimeContextState] = useState<ChannelContextState | null>(null);
  const [recent, setRecent] = useState<AttentionList | null>(null);
  const [recentError, setRecentError] = useState(false);
  const [recentLoading, setRecentLoading] = useState(false);
  const [candidateLanes, setCandidateLanes] = useState<CandidateLanesState | null>(null);
  const [progress, setProgress] = useState(100);
  const [offset, setOffset] = useState(0);
  const [receivedPageSnapshot, setReceivedPageSnapshot] = useState<ReceivedPageSnapshot | null>(null);
  const activeReceivedSnapshot = receivedPageSnapshot ?? linkedReceived;
  const candidateKind = view === 'attention' && params.get('candidate') === 'alarm' ? 'alarm'
    : view === 'attention' && params.get('candidate') === 'text' ? 'text' : 'all';
  const candidateAlarm = newSourceAlarmsOnly || candidateKind === 'alarm'
    ? true : candidateKind === 'text' ? false : undefined;
  const localNote = params.get('local_note') === 'present' ? 'present'
    : params.get('local_note') === 'absent' ? 'absent' : 'any';
  const [selection, setSelection] = useState<QueueSelection | null>(null);
  const [loading, setLoading] = useState(true);
  const [capabilitiesError, setCapabilitiesError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [autoRevision, setAutoRevision] = useState(0);
  const [manualRevision, setManualRevision] = useState(0);
  const error = capabilitiesError ?? listError;
  const { drafts: reviewDrafts, setDraft } = useReviewDrafts();

  useEffect(() => {
    setOffset(0);
    setReceivedPageSnapshot(null);
  }, [rowUid, objectId, channelId, requestedAsOf, requestedWatermark, requestedAfterWatermark]);

  useEffect(() => {
    const controller = new AbortController();
    getCapabilities(controller.signal, received ? activeReceivedSnapshot?.watermark : undefined)
      .then((value) => {
        if (controller.signal.aborted) return;
        if (value.mode !== (received ? 'received' : 'replay') || !value.observations_ready ||
            (!received && (!value.replay_window_start || !value.replay_window_end))) {
          throw new Error('Режим данных в API не готов');
        }
        setCapabilities(value);
        setCapabilitiesError(null);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setCapabilitiesError(cause instanceof Error ? cause.message : 'Не удалось прочитать состояние API');
        setCapabilities(null);
        setData(null);
        setRecent(null);
        setLoading(false);
      });
    return () => controller.abort();
  }, [received, activeReceivedSnapshot?.watermark, autoRevision, manualRevision]);

  useActiveRefresh(received, setAutoRevision);

  const linkedAsOf = useMemo(() => {
    if (received || !capabilities?.replay_window_start || !capabilities.replay_window_end) return null;
    return linkedReplayAsOf(requestedAsOf, capabilities.replay_window_start, capabilities.replay_window_end);
  }, [capabilities, received, requestedAsOf]);
  const asOf = useMemo(() => {
    if (received) return activeReceivedSnapshot?.asOf;
    if (!capabilities?.replay_window_start || !capabilities.replay_window_end) return null;
    if (linkedAsOf) return linkedAsOf;
    const start = Date.parse(capabilities.replay_window_start);
    const end = Date.parse(capabilities.replay_window_end);
    return new Date(start + ((end - start) * progress) / 100).toISOString();
  }, [capabilities, linkedAsOf, progress, received, activeReceivedSnapshot?.asOf]);
  const sliderProgress = useMemo(() => {
    if (!linkedAsOf || !capabilities?.replay_window_start || !capabilities.replay_window_end) return progress;
    const start = Date.parse(capabilities.replay_window_start);
    const span = Date.parse(capabilities.replay_window_end) - start;
    return span > 0 ? Math.max(0, Math.min(100, ((Date.parse(linkedAsOf) - start) / span) * 100)) : 100;
  }, [capabilities, linkedAsOf, progress]);
  const observationsReady = capabilities?.observations_ready ?? false;
  const pinnedUpdates = activeReceivedSnapshot &&
    capabilities?.received_after_watermark === activeReceivedSnapshot.watermark
    ? capabilities : null;
  const recordsAfterPinnedSnapshot = pinnedUpdates?.received_rows_after_watermark ?? null;
  const sourceAlarmsAfterPinnedSnapshot =
    pinnedUpdates?.received_source_alarms_after_watermark ?? null;
  const candidatesAfterPinnedSnapshot =
    pinnedUpdates?.received_candidates_after_watermark ?? null;
  const candidateGroupsAfterPinnedSnapshot =
    pinnedUpdates?.received_candidate_groups_after_watermark ?? null;
  const listAutoRevision = activeReceivedSnapshot ? 0 : autoRevision;
  const scopeKey = JSON.stringify([
    objectId ?? null, channelId ?? null, rowUid ?? null, localNote, candidateKind,
    received ? 'received' : asOf, activeReceivedSnapshot?.asOf, activeReceivedSnapshot?.watermark,
    afterReceivedWatermark,
  ]);
  const showCandidateLanes = Boolean(data && data.items.length > 0 && view === 'attention' &&
    candidateKind === 'all' && !objectId && !channelId && !rowUid &&
    localNote === 'any' && afterReceivedWatermark === null);
  const candidateLanesScopeKey = showCandidateLanes && data
    ? JSON.stringify([data.mode, data.items[0]?.message.snapshot_id, data.as_of, data.received_watermark,
      data.policy_version]) : null;

  useEffect(() => {
    if (!observationsReady || (!received && !asOf)) return;
    const controller = new AbortController();
    setLoading(true);
    setRecent(null);
    setRecentError(false);
    setRecentLoading(received && view === 'attention' && !newCandidatesOnly);
    getAttention(asOf ?? undefined, offset, PAGE_SIZE, objectId, view, channelId, rowUid,
      localNote, candidateAlarm, controller.signal, activeReceivedSnapshot?.watermark,
      undefined, afterReceivedWatermark ?? undefined)
      .then((value) => {
        if (controller.signal.aborted) return;
        setData(value);
        setListError(null);
        setSelection((current) => {
          const sameSource = current && value.items[0]
            ? current.entry.message.source_namespace === value.items[0].message.source_namespace &&
              current.entry.message.snapshot_id === value.items[0].message.snapshot_id
            : value.all_records_total > 0;
          const previous = current?.scopeKey === scopeKey && sameSource ? current : null;
          const refreshed = value.items.find((item) =>
            item.message.row_uid === previous?.entry.message.row_uid);
          if (refreshed) return {
            entry: refreshed, scopeKey, checkedAsOf: value.as_of,
            checkedWatermark: value.received_watermark ?? null,
          };
          if (previous) return previous;
          return value.items[0]
            ? {
              entry: value.items[0], scopeKey, checkedAsOf: value.as_of,
              checkedWatermark: value.received_watermark ?? null,
            }
            : null;
        });
        if (received && view === 'attention' && !newCandidatesOnly) {
          getAttention(value.as_of, 0, 5, objectId, 'all', channelId, rowUid, 'any', undefined, controller.signal, value.received_watermark ?? undefined)
            .then((latest) => {
              if (!controller.signal.aborted) setRecent(latest);
            })
            .catch(() => {
              if (!controller.signal.aborted) setRecentError(true);
            })
            .finally(() => {
              if (!controller.signal.aborted) setRecentLoading(false);
            });
        }
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setListError(cause instanceof Error ? cause.message : 'Не удалось прочитать очередь');
        setData(null);
        setRecentLoading(false);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [asOf, candidateAlarm, channelId, localNote, objectId, observationsReady, offset, received, activeReceivedSnapshot?.watermark, listAutoRevision, manualRevision, rowUid, scopeKey, view, afterReceivedWatermark, newCandidatesOnly]);

  useEffect(() => {
    if (!candidateLanesScopeKey || !data) {
      setCandidateLanes(null);
      return;
    }
    const controller = new AbortController();
    const scopeAsOf = data.as_of;
    const watermark = data.received_watermark ?? undefined;
    setCandidateLanes({ scopeKey: candidateLanesScopeKey,
      alarm: null, text: null, objects: null, loading: true, error: false });
    Promise.allSettled([
      getAttention(scopeAsOf, 0, 1, undefined, 'attention', undefined, undefined,
        'any', true, controller.signal, watermark),
      getAttention(scopeAsOf, 0, 1, undefined, 'attention', undefined, undefined,
        'any', false, controller.signal, watermark),
      getObjectAttention(scopeAsOf, 0, 5, 'candidate', controller.signal, watermark),
    ]).then(([alarms, texts, objects]) => {
      if (controller.signal.aborted) return;
      setCandidateLanes({
        scopeKey: candidateLanesScopeKey,
        alarm: alarms.status === 'fulfilled' ? alarms.value : null,
        text: texts.status === 'fulfilled' ? texts.value : null,
        objects: objects.status === 'fulfilled' ? objects.value : null,
        loading: false,
        error: alarms.status === 'rejected' || texts.status === 'rejected' || objects.status === 'rejected',
      });
    });
    return () => controller.abort();
  }, [candidateLanesScopeKey]);

  const activeCandidateLanes = candidateLanes?.scopeKey === candidateLanesScopeKey
    ? candidateLanes : null;
  const overviewObjects = activeCandidateLanes?.objects;
  const selected = selection?.scopeKey === scopeKey ? selection.entry : null;
  const selectedOutsidePage = Boolean(selected && data && !data.items.some((item) =>
    item.message.row_uid === selected.message.row_uid));
  const channelContextKey = selected && selection ? JSON.stringify([
    selected.message.source_namespace, selected.message.snapshot_id,
    selected.message.object_id, selected.message.channel_id,
    selection.checkedAsOf, selection.checkedWatermark,
  ]) : null;
  const activeChannelContext = channelContextState?.scopeKey === channelContextKey
    ? channelContextState : null;
  const contextData = activeChannelContext?.data;
  const contextAsOf = selection?.checkedAsOf;
  const contextWatermark = selection?.checkedWatermark;
  const contextObjectId = selected?.message.object_id;
  const contextChannelId = selected?.message.channel_id;
  const contextEventAt = selected?.message.event_at;
  const sameTimeContextKey = channelContextKey && contextEventAt
    ? JSON.stringify([channelContextKey, contextEventAt]) : null;
  const activeSameTimeContext = sameTimeContextState?.scopeKey === sameTimeContextKey
    ? sameTimeContextState : null;
  const sameTimeData = activeSameTimeContext?.data;

  useEffect(() => {
    if (!channelContextKey || !contextAsOf || !contextChannelId) {
      setChannelContextState(null);
      return;
    }
    if (received && contextWatermark === null) {
      setChannelContextState({ scopeKey: channelContextKey, data: null, loading: false, error: true });
      return;
    }
    const controller = new AbortController();
    setChannelContextState({ scopeKey: channelContextKey, data: null, loading: true, error: false });
    getAttention(
      contextAsOf, 0, 5, contextObjectId ?? '__unknown__',
      'all', contextChannelId, undefined, 'any', undefined,
      controller.signal, received ? contextWatermark ?? undefined : undefined,
    )
      .then((value) => {
        if (!controller.signal.aborted) setChannelContextState({
          scopeKey: channelContextKey, data: value, loading: false, error: false,
        });
      })
      .catch(() => {
        if (!controller.signal.aborted) setChannelContextState({
          scopeKey: channelContextKey, data: null, loading: false, error: true,
        });
      });
    return () => controller.abort();
  }, [channelContextKey, contextAsOf, contextWatermark, contextObjectId,
    contextChannelId, received]);

  useEffect(() => {
    if (!sameTimeContextKey || !contextAsOf || !contextChannelId || !contextEventAt) {
      setSameTimeContextState(null);
      return;
    }
    if (received && contextWatermark === null) {
      setSameTimeContextState({ scopeKey: sameTimeContextKey, data: null, loading: false, error: true });
      return;
    }
    const controller = new AbortController();
    setSameTimeContextState({ scopeKey: sameTimeContextKey, data: null, loading: true, error: false });
    getAttention(
      contextAsOf, 0, 25, contextObjectId ?? '__unknown__',
      'all', contextChannelId, undefined, 'any', undefined,
      controller.signal, received ? contextWatermark ?? undefined : undefined,
      contextEventAt,
    )
      .then((value) => {
        if (!controller.signal.aborted) setSameTimeContextState({
          scopeKey: sameTimeContextKey, data: value, loading: false, error: false,
        });
      })
      .catch(() => {
        if (!controller.signal.aborted) setSameTimeContextState({
          scopeKey: sameTimeContextKey, data: null, loading: false, error: true,
        });
      });
    return () => controller.abort();
  }, [sameTimeContextKey, contextAsOf, contextWatermark, contextObjectId,
    contextChannelId, contextEventAt, received]);

  function openContextItem(item: AttentionEntry, context: AttentionList) {
    const query = new URLSearchParams({
      view: 'all', row_uid: item.message.row_uid,
      object: item.message.object_id ?? '__unknown__',
      channel: item.message.channel_id,
      as_of: context.as_of,
    });
    if (received && context.received_watermark != null) {
      query.set('received_watermark', String(context.received_watermark));
    }
    setOffset(0);
    setParams(query);
  }

  function openCandidateLane(kind: 'alarm' | 'text', lane: AttentionList) {
    const query = new URLSearchParams({ candidate: kind, as_of: lane.as_of });
    if (received && lane.received_watermark != null) {
      query.set('received_watermark', String(lane.received_watermark));
    }
    setOffset(0);
    setSelection(null);
    setParams(query);
  }

  function openRecent(item: AttentionEntry) {
    const checkedAsOf = recent?.as_of ?? data?.as_of;
    if (!checkedAsOf) return;
    setOffset(0);
    setSelection(null);
    setParams((current) => {
      const next = new URLSearchParams(current);
      next.set('view', 'all');
      next.set('row_uid', item.message.row_uid);
      next.set('as_of', checkedAsOf);
      const watermark = recent?.received_watermark ?? data?.received_watermark;
      if (watermark != null) next.set('received_watermark', String(watermark));
      next.delete('candidate');
      next.delete('local_note');
      return next;
    });
  }

  function linkObservationScope(query: URLSearchParams, checkedAsOf: string, receivedWatermark?: number | null) {
    if (!data) return;
    query.set('as_of', checkedAsOf);
    if (received && (receivedWatermark ?? data.received_watermark) != null) {
      query.set('received_watermark', String(receivedWatermark ?? data.received_watermark));
    }
  }

  return (
    <>
      <PageHeading
        title={received ? 'Исходные сообщения · локально полученные' : 'Исходные сообщения · исторический replay'}
        description="Зарегистрированные сообщения; порядок проверки предварительный и не означает тяжесть события."
      />
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={received ? 'Время доступности — время локального импорта' : 'Историческая запись · доставка симулирована по времени события'}
        description={received
          ? `${activeReceivedSnapshot
            ? 'Срез закреплён; новые сообщения откройте кнопкой «К новым сообщениям».'
            : 'Обновление каждые 30 секунд и при возврате к вкладке.'} Это не прямое подключение к системе заказчика; время доставки до локального импорта неизвестно. Неисправность не подтверждена; прогноз показан отдельно в разделе «Прогноз».`
          : 'Это не live-мониторинг. Физическая неисправность не подтверждена; прогноз показан отдельно в разделе «Прогноз». Локальные заметки доступны только при включении на сервере.'}
      />
      {(objectId || channelId || rowUid) && (
        <Alert
          type="info"
          style={{ marginBottom: 16 }}
          message={[
            objectId === '__unknown__' ? 'Без привязки к объекту' : objectId ? `Объект: ${objectId}` : null,
            channelId ? `Канал: ${channelId}` : null,
            rowUid ? `Исходная запись: ${rowUid.slice(0, 12)}…` : null,
          ].filter(Boolean).join(' · ')}
          action={<Button onClick={() => {
            setOffset(0);
            setParams((current) => {
              const next = new URLSearchParams(current);
              next.delete('object');
              next.delete('channel');
              next.delete('row_uid');
              return next;
            });
          }}>Показать все</Button>}
        />
      )}
      {received && (capabilities?.received_inbox_failure_count ?? 0) > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={`Локальная папка: не приняты файлы (${capabilities?.received_inbox_failure_count})`}
          description={`Последняя ошибка импорта: ${capabilities?.received_inbox_last_failure_at
            ? `${inMoscow(capabilities.received_inbox_last_failure_at)} МСК`
            : 'время неизвестно'}. Очередь показывает только принятые записи; отсутствие новых событий не означает исправность.`}
        />
      )}
      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="Операционные данные недоступны"
          description={error}
          action={<Button onClick={() => setManualRevision((value) => value + 1)}>Повторить</Button>}
        />
      )}
      {!received && requestedAsOf && capabilities && !linkedAsOf && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="Момент из ссылки вне окна replay"
          description="Показан момент, установленный ползунком; проверьте выбранный срез."
        />
      )}
      {received && (requestedAsOf !== null || requestedWatermark !== null) && !linkedReceived && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="Неполная ссылка на срез сообщений"
          description="Показан актуальный список. Для закрепления нужны время и номер принятой записи." />
      )}
      {received && requestedAfterWatermark !== null && afterReceivedWatermark === null && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="Некорректная граница новых сообщений"
          description="Показан обычный список; номер записи в ссылке должен быть целым неотрицательным числом." />
      )}
      {afterReceivedWatermark !== null && (
        <Alert type="info" showIcon style={{ marginBottom: 16 }}
          message={newSourceAlarmsOnly
            ? `Исходные alarm=true после записи №${afterReceivedWatermark} во всём локальном потоке`
            : `Предварительные кандидаты после записи №${afterReceivedWatermark} во всём локальном потоке`}
          description={newSourceAlarmsOnly
            ? 'Это зарегистрированные сообщения, а не подтверждённые отказы или оценка их тяжести.'
            : 'Исходный alarm=true и точные пары типа/текста при alarm=false. Перечень и порядок предварительные; это не диагноз или оценка тяжести.'}
          action={<Button onClick={() => {
            setOffset(0);
            setReceivedPageSnapshot(null);
            setData(null);
            setSelection(null);
            setParams((current) => {
              const next = new URLSearchParams(current);
              next.delete('after_received_watermark');
              next.delete('as_of');
              next.delete('received_watermark');
              next.delete('row_uid');
              next.set('view', 'all');
              next.delete('object');
              next.delete('channel');
              next.delete('candidate');
              next.delete('local_note');
              return next;
            });
          }}>Все сообщения</Button>} />
      )}
      {capabilities && received && !error && (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Flex justify="space-between" align="center" wrap gap={16}>
            <div>
              <Text strong>Последний локальный импорт: {capabilities.received_last_at
                ? `${inMoscow(capabilities.received_last_at)} МСК`
                : 'неизвестен'} · всего записей: {capabilities.received_rows ?? 0}</Text>
              <br /><Text type="secondary">Последний просмотр входной папки: {capabilities.received_inbox_last_scan_at
                ? `${inMoscow(capabilities.received_inbox_last_scan_at)} МСК${capabilities.received_status_checked_at
                  ? ` · прошло ${elapsedSinceAt(capabilities.received_status_checked_at, capabilities.received_inbox_last_scan_at)} на момент проверки локального API`
                  : ''}`
                : 'не подтверждён'}</Text>
              {data && !loading && <><br /><Text type="secondary">
                {activeReceivedSnapshot
                  ? `Список зафиксирован на ${inMoscow(data.as_of)} МСК: новые партии не сдвигают страницы.`
                  : `Последний успешный ответ API: ${inMoscow(data.as_of)} МСК. Он не подтверждает работу входного импорта или связь с каналом; отсутствие новых сообщений не доказывает исправность.`}
              </Text></>}
              {activeReceivedSnapshot && recordsAfterPinnedSnapshot !== null && recordsAfterPinnedSnapshot > 0 &&
                <><br /><Text strong>
                  После закрепления во всём локальном потоке импортировано {recordCount(recordsAfterPinnedSnapshot)}.
                  {sourceAlarmsAfterPinnedSnapshot !== null &&
                    ` Из них с исходным alarm=true: ${sourceAlarmsAfterPinnedSnapshot}; это не оценка тяжести.`}
                  {candidatesAfterPinnedSnapshot !== null && sourceAlarmsAfterPinnedSnapshot !== null &&
                    ` Предварительных кандидатов: ${candidatesAfterPinnedSnapshot} (в том числе ${candidatesAfterPinnedSnapshot - sourceAlarmsAfterPinnedSnapshot} с точным текстом при alarm=false).`}
                  {candidateGroupsAfterPinnedSnapshot !== null && candidatesAfterPinnedSnapshot !== null && candidatesAfterPinnedSnapshot > 0 &&
                    ` Число сочетаний указанных во входе объекта и канала среди них: ${candidateGroupsAfterPinnedSnapshot}; привязка объекта не подтверждена.`}
                  {' '}Эти исходные записи не входят в показанный срез; их можно открыть кнопкой «К новым сообщениям».
                </Text></>}
              {activeReceivedSnapshot && (candidatesAfterPinnedSnapshot ?? 0) > 0 &&
                <><br /><Button type="link" style={{ paddingInline: 0 }} onClick={() => {
                  const lowerWatermark = activeReceivedSnapshot.watermark;
                  setOffset(0);
                  setReceivedPageSnapshot(null);
                  setData(null);
                  setSelection(null);
                  setParams((current) => {
                    const next = new URLSearchParams(current);
                    next.delete('view');
                    next.set('after_received_watermark', String(lowerWatermark));
                    next.delete('as_of');
                    next.delete('received_watermark');
                    next.delete('row_uid');
                    next.delete('object');
                    next.delete('channel');
                    next.delete('candidate');
                    next.delete('local_note');
                    return next;
                  });
                }}>Открыть новые предварительные кандидаты</Button></>}
              {activeReceivedSnapshot && (sourceAlarmsAfterPinnedSnapshot ?? 0) > 0 &&
                <><br /><Button type="link" style={{ paddingInline: 0 }} onClick={() => {
                  const lowerWatermark = activeReceivedSnapshot.watermark;
                  setOffset(0);
                  setReceivedPageSnapshot(null);
                  setData(null);
                  setSelection(null);
                  setParams((current) => {
                    const next = new URLSearchParams(current);
                    next.set('view', 'all');
                    next.set('after_received_watermark', String(lowerWatermark));
                    next.delete('as_of');
                    next.delete('received_watermark');
                    next.delete('row_uid');
                    next.delete('object');
                    next.delete('channel');
                    next.delete('candidate');
                    next.delete('local_note');
                    return next;
                  });
                }}>Открыть новые исходные alarm=true</Button></>}
            </div>
            <Button onClick={() => {
              if (activeReceivedSnapshot) {
                setOffset(0);
                setReceivedPageSnapshot(null);
                setManualRevision((value) => value + 1);
                if (linkedReceived) setParams((current) => {
                  const next = new URLSearchParams(current);
                  next.delete('as_of');
                  next.delete('received_watermark');
                  next.delete('row_uid');
                  return next;
                });
              } else setManualRevision((value) => value + 1);
            }}>{activeReceivedSnapshot ? 'К новым сообщениям' : 'Обновить'}</Button>
          </Flex>
        </Card>
      )}
      {capabilities && !received && asOf && (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Flex justify="space-between" align="center" wrap gap={16}>
            <div>
              <Text strong>Момент replay: {inMoscow(asOf)} МСК</Text>
              <br />
              <Text type="secondary">
                Окно: {inMoscow(capabilities.replay_window_start!)} —{' '}
                {inMoscow(capabilities.replay_window_end!)} МСК
              </Text>
            </div>
            <div style={{ minWidth: 240, flex: 1, maxWidth: 440 }}>
              <Text>Прокрутка исторического окна</Text>
              <Slider
                min={0}
                max={100}
                value={sliderProgress}
                onChange={(value) => {
                  setProgress(value);
                  setOffset(0);
                  if (requestedAsOf) setParams((current) => {
                    const next = new URLSearchParams(current);
                    next.delete('as_of');
                    return next;
                  });
                }}
                aria-label="Момент исторического replay"
              />
            </div>
          </Flex>
        </Card>
      )}
      {loading && <Spin tip="Читаем зарегистрированные сообщения"><div style={{ height: 80 }} /></Spin>}
      {received && view === 'attention' && !newCandidatesOnly && data && !loading && (
        <Card
          size="small"
          title="Последние поступившие записи"
          extra={<Text type="secondary">{activeReceivedSnapshot
            ? 'До фиксированного момента · вне предварительного приоритета'
            : 'По времени локального импорта · вне предварительного приоритета'}</Text>}
          style={{ marginBottom: 16 }}
        >
          {recentLoading && <Spin size="small" aria-label="Читаем последние записи" />}
          {recentError && <Alert type="warning" showIcon message="Последние поступления недоступны; полный журнал можно открыть через «Все исходные»." />}
          {recent && <List
            size="small"
            dataSource={recent.items}
            locale={{ emptyText: 'К этому моменту записи не поступили' }}
            renderItem={(item) => (
              <List.Item key={item.message.row_uid} style={{ flexWrap: 'wrap', gap: 8 }}>
                <Button
                  type="link"
                  aria-label={`Открыть исходную запись канала ${item.message.channel_id}: ${item.message.value_raw}`}
                  onClick={() => openRecent(item)}
                  style={{ height: 'auto', maxWidth: '100%', whiteSpace: 'normal', textAlign: 'left' }}
                >
                  {item.message.object_id ?? 'Объект неизвестен'} · {item.message.channel_id} · {item.message.value_raw}
                </Button>
                <Text type="secondary">
                  {alarmFlagText(item.message.alarm)} · импорт {inMoscow(item.message.available_at)} МСК · источник {inMoscow(item.message.event_at)} МСК
                </Text>
              </List.Item>
            )}
          />}
        </Card>
      )}
      {showCandidateLanes && data && !loading && (
        <Card size="small" title="Оба вида кандидатов" style={{ marginBottom: 16 }}>
          <Paragraph type="secondary">
            По одному последнему сообщению каждого вида в том же срезе. Виды показаны рядом
            для навигации; их тяжесть и порядок между ними не утверждены.
          </Paragraph>
          {activeCandidateLanes?.loading && <Spin size="small" aria-label="Читаем оба вида кандидатов" />}
          {activeCandidateLanes?.error && <Alert type="warning" showIcon
            message="Часть обзора кандидатов недоступна; полные списки остаются ниже и на схеме" />}
          <Flex wrap gap={12}>
            {([
              { kind: 'alarm', title: 'Исходный alarm=true' },
              { kind: 'text', title: 'Точный текст · alarm=false' },
            ] as const).map(({ kind, title }) => {
              const lane = activeCandidateLanes?.[kind] ?? null;
              const first = lane?.items[0];
              return <Card key={kind} size="small" title={title}
                style={{ flex: '1 1 290px', minWidth: 260 }}>
                <Text type="secondary">В этом срезе: {lane ? recordCount(lane.total)
                  : activeCandidateLanes?.loading ? 'читаем' : 'недоступно'}</Text>
                {lane && lane.total === 0 && <Paragraph>Таких сообщений нет.</Paragraph>}
                {first && <>
                  <Paragraph style={{ margin: '8px 0' }}>
                    <Text strong>{first.message.object_id ?? 'Объект неизвестен'} · {first.message.channel_id}</Text>
                    <br /><Text code>{JSON.stringify(first.message.value_raw)}</Text>
                    <br /><Text type="secondary">
                      {received ? 'Локальный импорт' : 'Доступно в replay'}: {inMoscow(first.message.available_at)} МСК
                      {' '}· источник: {inMoscow(first.message.event_at)} МСК
                    </Text>
                  </Paragraph>
                  <Button type="link" onClick={() => openContextItem(first, lane)}>
                    Открыть исходную запись
                  </Button>
                </>}
                {lane && lane.total > 0 && <Button type="link" onClick={() => openCandidateLane(kind, lane)}>
                  Все записи этого вида
                </Button>}
              </Card>;
            })}
            {overviewObjects && <Card size="small"
              title={`Объекты с кандидатами · ${overviewObjects.total}`}
              style={{ flex: '1 1 290px', minWidth: 260 }}>
              <Text type="secondary">До пяти групп по времени последнего кандидата. Счёт исходных записей, не инцидентов.</Text>
              <List size="small" dataSource={overviewObjects.items}
                renderItem={(item) => <List.Item key={item.object_id ?? '__unknown__'}>
                  <div>
                    <Button type="link" onClick={() => {
                      const query = new URLSearchParams({
                        filter: 'candidate', object: item.object_id ?? '__unknown__',
                      });
                      linkObservationScope(query, overviewObjects.as_of,
                        overviewObjects.received_watermark);
                      navigate(`/map?${query}`);
                    }}>
                      {item.object_id ? `Объект ${item.object_id}` : 'Без привязки к объекту'}
                    </Button>
                    <br /><Text type="secondary">
                      кандидатов: {item.candidate_count} · alarm=true: {item.source_alarm_count}
                      {' '}· точный текст при alarm=false: {item.candidate_count - item.source_alarm_count}
                      {' '}· каналов: {item.channel_count}
                    </Text>
                  </div>
                </List.Item>}
              />
              {overviewObjects.total > overviewObjects.items.length &&
                <Button type="link" onClick={() => {
                  const query = new URLSearchParams({ filter: 'candidate' });
                  linkObservationScope(query, overviewObjects.as_of,
                    overviewObjects.received_watermark);
                  navigate(`/map?${query}`);
                }}>Все группы на схеме</Button>}
            </Card>}
          </Flex>
        </Card>
      )}
      {data && !loading && (
        <Flex gap={16} align="start" wrap>
          <Card
            title={<>{newSourceAlarmsOnly ? 'Новые исходные alarm=true' : newCandidatesOnly ? 'Новые предварительные кандидаты' : view === 'attention' ? 'Кандидаты на разбор' : 'Все исходные · сначала новые'} <Tag>{recordCount(data.total)}</Tag></>}
            style={{ flex: '1 1 420px', minWidth: 320 }}
          >
            <Flex justify="space-between" align="center" wrap gap={8} style={{ marginBottom: 12 }}>
              {afterReceivedWatermark === null && <Segmented
                aria-label="Вид записей"
                value={view}
                options={[
                  { value: 'attention', label: 'Кандидаты' },
                  { value: 'all', label: 'Все исходные' },
                ]}
                onChange={(value) => {
                  setOffset(0);
                  setParams((current) => {
                    const next = new URLSearchParams(current);
                    if (value === 'all') {
                      next.set('view', 'all');
                      next.delete('candidate');
                    }
                    else next.delete('view');
                    return next;
                  });
                }}
              />}
              <Text type="secondary">{newSourceAlarmsOnly ? 'Новых исходных alarm=true'
                : newCandidatesOnly ? 'Новых исходных в выбранном фильтре'
                : candidateKind === 'all' ? 'Всего исходных' : 'Исходных с выбранным alarm'}: {data.all_records_total}</Text>
            </Flex>
            {view === 'attention' && !newSourceAlarmsOnly && <Segmented
              aria-label="Фильтр кандидатов по исходному признаку"
              value={candidateKind}
              options={[
                { value: 'all', label: 'Все кандидаты' },
                { value: 'alarm', label: 'Исходный alarm=true' },
                { value: 'text', label: 'Точный текст · alarm=false' },
              ]}
              onChange={(value) => {
                setOffset(0);
                setParams((current) => {
                  const next = new URLSearchParams(current);
                  if (value === 'all') next.delete('candidate');
                  else next.set('candidate', value);
                  return next;
                });
              }}
              style={{ marginBottom: 12 }}
            />}
            <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
              {view === 'attention' ? `Правило порядка: ${data.policy_version}` : 'Порядок: время доступности'}
            </Text>
            <Flex align="center" wrap gap={8} style={{ marginBottom: 12 }}>
              <Text type="secondary">Состав выбранного списка:</Text>
              <Tag color="error">alarm=true: {data.source_alarm_count}</Tag>
              <Tag color="processing">Точный текст при alarm=false: {data.watch_text_count}</Tag>
              {view === 'all' && <Tag>Прочие сообщения: {data.chronological_count}</Tag>}
              <Text type="secondary">Это число записей, не оценка тяжести.</Text>
            </Flex>
            <Button style={{ marginBottom: 12 }} onClick={() => {
              const filter = newSourceAlarmsOnly || candidateKind === 'alarm' ? 'alarm'
                : candidateKind === 'text' ? 'text'
                  : view === 'attention' ? 'candidate' : 'all';
              const query = new URLSearchParams();
              if (filter !== 'all') query.set('filter', filter);
              if (objectId) query.set('object', objectId);
              if (objectId && channelId) query.set('channel', channelId);
              linkObservationScope(query, data.as_of);
              navigate(`/map?${query}`);
            }}>
              Группы по объектам и каналам
            </Button>
            <Flex align="center" wrap gap={8} style={{ marginBottom: 12 }}>
              <Text>Локальная заметка:</Text>
              <Segmented
                aria-label="Фильтр по наличию локальной заметки"
                value={localNote}
                options={[
                  { value: 'any', label: 'Любая запись' },
                  { value: 'absent', label: 'Без заметки' },
                  { value: 'present', label: 'С заметкой' },
                ]}
                onChange={(value) => {
                  setOffset(0);
                  setParams((current) => {
                    const next = new URLSearchParams(current);
                    if (value === 'any') next.delete('local_note');
                    else next.set('local_note', value);
                    return next;
                  });
                }}
              />
            </Flex>
            {localNote !== 'any' && <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
              Фильтр показывает наличие записи в локальном журнале сейчас.
              {!received && ' В replay это не состояние журнала на выбранный момент.'}
              {' '}Заметка не снимает отметку «тревожное» и не подтверждает устранение.
            </Text>}
            <List
              dataSource={data.items}
              locale={{ emptyText: localNote !== 'any' && data.total === 0
                ? `Записей ${localNote === 'absent' ? 'без локальной заметки' : 'с локальной заметкой'} в выбранном списке нет`
                : newSourceAlarmsOnly && data.all_records_total === 0
                ? 'После указанной границы исходных alarm=true нет'
                : newCandidatesOnly && data.total === 0
                ? 'После указанной границы предварительных кандидатов с выбранным фильтром нет'
                : data.all_records_total === 0
                ? (objectId || channelId || rowUid
                  ? 'Для выбранного фильтра записи не поступили'
                  : 'К этому моменту записи не поступили')
                : view === 'attention'
                  ? 'В списке кандидатов пусто; исходные сообщения доступны во вкладке «Все исходные»'
                  : 'В выбранном фильтре исходных записей нет' }}
              renderItem={(item) => (
                <List.Item
                  key={item.message.row_uid}
                  tabIndex={0}
                  role="button"
                  aria-pressed={item.message.row_uid === selected?.message.row_uid}
                  onClick={() => setSelection({ entry: item, scopeKey, checkedAsOf: data.as_of,
                    checkedWatermark: data.received_watermark ?? null })}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setSelection({ entry: item, scopeKey, checkedAsOf: data.as_of,
                        checkedWatermark: data.received_watermark ?? null });
                    }
                  }}
                  style={{ cursor: 'pointer' }}
                >
                  <div style={{ width: '100%' }}>
                    <Flex justify="space-between" gap={12}>
                      <Text strong>{item.review_order}. {item.message.channel_id}</Text>
                      <Tag color={item.message.alarm ? 'error' : 'default'} className={alarmFlagMissing(item.message.alarm) ? 'tag--muted' : undefined}>
                        {alarmFlagText(item.message.alarm)}
                      </Tag>
                    </Flex>
                    <Text>{item.message.sensor_type ?? 'Тип неизвестен'} ·{' '}
                      {item.message.object_id ?? 'Привязка к объекту неизвестна'}</Text>
                    {item.message.object_id && <><br /><Text type="secondary">
                      {objectBindingEvidence(item.message, received)}
                    </Text></>}
                    <br />
                    <Text code>{JSON.stringify(item.message.value_raw)}</Text>
                    <br />
                    <Text type="secondary">
                      {`${received ? 'Локальный импорт' : 'Доступно в replay'}: ${inMoscow(item.message.available_at)} МСК · с поступления: ${elapsedSinceAt(data.as_of, item.message.available_at)} · время источника: ${inMoscow(item.message.event_at)} МСК`}
                      {view === 'attention' ? ` · ${reason(item)}` : ''}
                    </Text>
                    {item.local_review_revision > 0 && (
                      <><br /><Tag color="blue">Локальная заметка · ревизия {item.local_review_revision}</Tag></>
                    )}
                  </div>
                </List.Item>
              )}
            />
            <Space style={{ marginTop: 12 }}>
              <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                Назад
              </Button>
              <Text>{data.total === 0 ? 0 : offset + 1}–{offset + data.items.length} из {data.total}</Text>
              <Button disabled={offset + PAGE_SIZE >= data.total} onClick={() => {
                if (received && !activeReceivedSnapshot) {
                  if (data.received_watermark == null) {
                    setListError('Ответ API не содержит границу принятого импорта');
                    return;
                  }
                  setReceivedPageSnapshot({ asOf: data.as_of, watermark: data.received_watermark });
                }
                setOffset(offset + PAGE_SIZE);
              }}>
                Далее
              </Button>
            </Space>
          </Card>
          <Card title="Исходная запись" style={{ flex: '1 1 420px', minWidth: 320 }}>
            {selected ? (
              <>
                {selectedOutsidePage && <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16 }}
                  message="Выбранная запись вышла за пределы текущей страницы"
                  description={`Карточка сохранена по чтению на ${inMoscow(selection?.checkedAsOf ?? data.as_of)} МСК. Позиция могла измениться; сервер проверит ревизию заметки и версию правила при сохранении.`}
                  action={<Button onClick={() => setSelection(data.items[0]
                    ? { entry: data.items[0], scopeKey, checkedAsOf: data.as_of,
                      checkedWatermark: data.received_watermark ?? null }
                    : null)}>К текущему списку</Button>}
                />}
                <Title level={4}>{selected.message.sensor_type ?? 'Тип неизвестен'} · {selected.message.channel_id}</Title>
                <Paragraph><Text strong>Объект: </Text>{selected.message.object_id ?? 'привязка неизвестна'}</Paragraph>
                <Paragraph><Text strong>Основание привязки: </Text>{objectBindingEvidence(selected.message, received)}</Paragraph>
                <Paragraph><Text strong>Система: </Text>{selected.message.system_type ?? 'неизвестна'}</Paragraph>
                <Paragraph><Text strong>Точный текст: </Text><Text code>{JSON.stringify(selected.message.value_raw)}</Text></Paragraph>
                <Paragraph><Text strong>Исходный alarm: </Text>{alarmFlagMissing(selected.message.alarm) ? <Text type="secondary">не передан</Text> : String(selected.message.alarm)}</Paragraph>
                <Paragraph><Text strong>Время источника: </Text>{inMoscow(selected.message.event_at)} МСК</Paragraph>
                <Paragraph><Text strong>{received ? 'Локальный импорт: ' : 'Доступно в replay: '}</Text>{inMoscow(selected.message.available_at)} МСК
                  {' '}({selected.message.availability_basis})</Paragraph>
                {received && <Paragraph><Text strong>Разница временных меток: </Text>
                  {sourceImportTimeDifference(selected.message.event_at, selected.message.available_at)}.
                  {' '}Это не измерение доставки от системы заказчика и не оценка состояния датчика.
                </Paragraph>}
                <Paragraph><Text strong>На момент просмотра: </Text>
                  с поступления — {elapsedSinceAt(selection?.checkedAsOf ?? data.as_of, selected.message.available_at)};
                  {' '}с времени источника — {elapsedSinceAt(selection?.checkedAsOf ?? data.as_of, selected.message.event_at)}.
                  {' '}Это возраст записи, не оценка состояния канала.</Paragraph>
                <Paragraph><Text strong>{view === 'attention' ? 'Основание очередности: ' : 'Признак сообщения: '}</Text>{reason(selected)}</Paragraph>
                {selected.local_last_review_at && (
                  <Paragraph><Text strong>Последняя локальная заметка: </Text>
                    {inMoscow(selected.local_last_review_at)} МСК · ревизия {selected.local_review_revision}
                  </Paragraph>
                )}
                <Paragraph><Text strong>Версия правила: </Text>{selected.policy_version} ({selected.policy_status})</Paragraph>
                <div style={{ marginBottom: 16 }}>
                  <Text strong>Отметки о данных:</Text>
                  {selected.message.quality_flags?.length ? <List size="small"
                    dataSource={selected.message.quality_flags}
                    renderItem={(flag) => <List.Item key={flag}>
                      {qualityFlagLabel(flag)} <Text type="secondary" code>{flag}</Text>
                    </List.Item>}
                  /> : <Paragraph type="secondary" style={{ marginBottom: 0 }}>
                    Автоматические флаги не назначены; это не подтверждает состояние канала.
                  </Paragraph>}
                </div>
                <Paragraph><Text strong>Происхождение: </Text>
                  {selected.message.source_namespace} · {selected.message.snapshot_id.slice(0, 12)}… ·{' '}
                  {selected.message.row_uid.slice(0, 12)}…</Paragraph>
                <Space wrap style={{ marginBottom: 8 }}>
                  <Button onClick={() => {
                    const query = new URLSearchParams({ object: selected.message.object_id ?? '__unknown__' });
                    query.set('channel', selected.message.channel_id);
                    linkObservationScope(query, selection?.checkedAsOf ?? data.as_of, selection?.checkedWatermark);
                    navigate(`/map?${query}`);
                  }}>Объект и каналы на схеме</Button>
                  <Button onClick={() => {
                    const query = new URLSearchParams({ tab: 'messages', row_uid: selected.message.row_uid });
                    linkObservationScope(query, selection?.checkedAsOf ?? data.as_of, selection?.checkedWatermark);
                    navigate(`/queue/journal?${query}`);
                  }}>Исходное сообщение в журнале</Button>
                  {capabilities?.local_reviews_enabled && <Button onClick={() => {
                    const query = new URLSearchParams({ row_uid: selected.message.row_uid });
                    if (!received) query.set('as_of', selection?.checkedAsOf ?? data.as_of);
                    navigate(`/queue/journal?${query}`);
                  }}>Журнал проверки записи</Button>}
                </Space>
                {(activeSameTimeContext?.loading || activeSameTimeContext?.error ||
                  (sameTimeData && sameTimeData.total > 1)) && <div style={{ marginBottom: 16 }}>
                  <Text strong>Сообщения канала с тем же временем источника</Text>
                  <Paragraph type="secondary" style={{ marginBottom: 8 }}>
                    Совпадение времени не задаёт порядок событий и не доказывает общий инцидент.
                    Показаны исходные тексты и отдельный alarm в том же объекте и срезе.
                  </Paragraph>
                  {activeSameTimeContext?.loading && <Spin size="small" aria-label="Читаем одновременные сообщения" />}
                  {activeSameTimeContext?.error && <Alert type="warning" showIcon
                    message="Одновременные сообщения недоступны"
                    description="Выбранная исходная карточка остаётся доступной." />}
                  {sameTimeData && sameTimeData.total > 1 && <>
                    <Text type="secondary">
                      {recordCount(sameTimeData.total)} с временем источника {inMoscow(selected.message.event_at)} МСК
                      {sameTimeData.total > sameTimeData.items.length
                        ? ` · показаны первые ${sameTimeData.items.length}; полная история доступна на схеме` : ''}
                    </Text>
                    <List size="small" dataSource={sameTimeData.items}
                      renderItem={(item) => <List.Item key={item.message.row_uid}>
                        <Button type="link" style={{ paddingLeft: 0, height: 'auto', whiteSpace: 'normal', textAlign: 'left' }}
                          onClick={() => openContextItem(item, sameTimeData)}>
                          {JSON.stringify(item.message.value_raw)} · {alarmFlagText(item.message.alarm)}
                          {' '}· {received ? 'импорт' : 'доступно'} {inMoscow(item.message.available_at)} МСК
                          {item.message.row_uid === selected.message.row_uid ? ' · выбрано' : ''}
                        </Button>
                      </List.Item>}
                    />
                  </>}
                </div>}
                <div style={{ marginBottom: 16 }}>
                  <Text strong>Последние исходные сообщения этого канала в объекте</Text>
                  <Paragraph type="secondary" style={{ marginBottom: 8 }}>
                    До пяти записей в выбранном срезе, включая сообщения вне списка кандидатов.
                    Это история сообщений, не число инцидентов и не подтверждённое состояние канала.
                    При совпадении времени доступности порядок строк не задаёт последовательность событий.
                  </Paragraph>
                  {activeChannelContext?.loading && <Spin size="small" aria-label="Читаем историю канала" />}
                  {activeChannelContext?.error && <Alert type="warning" showIcon
                    message="История канала недоступна"
                    description="Выбранная исходная карточка остаётся доступной." />}
                  {contextData && <>
                    <Text type="secondary">В выбранном срезе: {recordCount(contextData.total)}</Text>
                    <List
                      size="small"
                      dataSource={contextData.items}
                      locale={{ emptyText: 'В выбранном срезе нет сообщений этого канала' }}
                      renderItem={(item) => <List.Item key={item.message.row_uid}>
                        <Button type="link" style={{ paddingLeft: 0, height: 'auto', whiteSpace: 'normal', textAlign: 'left' }}
                          onClick={() => openContextItem(item, contextData)}>
                          {JSON.stringify(item.message.value_raw)} · {alarmFlagText(item.message.alarm)}
                          {' '}· {received ? 'импорт' : 'доступно'} {inMoscow(item.message.available_at)} МСК
                          {' '}· источник {inMoscow(item.message.event_at)} МСК
                        </Button>
                      </List.Item>}
                    />
                  </>}
                </div>
                <ReviewPanel
                  key={reviewDraftKey(selected)}
                  rowUid={selected.message.row_uid}
                  asOf={selection?.checkedAsOf ?? data.as_of}
                  receivedWatermark={selection?.checkedWatermark ?? data.received_watermark ?? null}
                  snapshotId={selected.message.snapshot_id}
                  policyVersion={selected.policy_version}
                  enabled={Boolean(capabilities?.local_reviews_enabled)}
                  mode={received ? 'received' : 'replay'}
                  draft={reviewDrafts[reviewDraftKey(selected)] ?? EMPTY_REVIEW_DRAFT}
                  onDraftChange={(draft) => setDraft(reviewDraftKey(selected), draft)}
                  onSaved={() => setManualRevision((value) => value + 1)}
                />
              </>
            ) : <Text type="secondary">Выберите запись из очереди.</Text>}
          </Card>
        </Flex>
      )}
    </>
  );
}
