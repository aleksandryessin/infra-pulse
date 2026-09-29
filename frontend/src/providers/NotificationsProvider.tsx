import {
  createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode,
} from 'react';
import { listAllForecasts, type ForecastCardView } from '../api/forecast';
import { isAbort } from '../api/http';
import { getNotifications, type NotificationItem, type NotificationSummary } from '../api/notifications';
import { NOTIFICATIONS_STORAGE_KEY, NOTIFICATION_WINDOW_HOURS } from '../shared/config/notifications';
import { bellRows, type BellRow } from '../shared/lib/bell';
import { isNewCard } from '../shared/lib/forecast';
import { subscribeActiveRefresh } from '../shared/lib/use-active-refresh';
import { useForecastState } from './ForecastStateProvider';

interface ReadState {
  /** Карточки прогноза, уже виденные в списке. */
  cards: string[];
  /** Прочитанные критические сообщения (row_uid). */
  messages: string[];
  /** Первый визит: карточки прежних расчётов не считаются новыми. */
  baselined: boolean;
}

const EMPTY: ReadState = { cards: [], messages: [], baselined: false };
const MAX_REMEMBERED = 1000;

function loadRead(): ReadState {
  try {
    const raw = localStorage.getItem(NOTIFICATIONS_STORAGE_KEY);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as Partial<ReadState>;
    return {
      cards: Array.isArray(parsed.cards) ? parsed.cards.filter((item) => typeof item === 'string') : [],
      messages: Array.isArray(parsed.messages) ? parsed.messages.filter((item) => typeof item === 'string') : [],
      baselined: parsed.baselined === true,
    };
  } catch {
    return EMPTY;
  }
}

function saveRead(state: ReadState): void {
  try {
    localStorage.setItem(NOTIFICATIONS_STORAGE_KEY, JSON.stringify({
      cards: state.cards.slice(-MAX_REMEMBERED),
      messages: state.messages.slice(-MAX_REMEMBERED),
      baselined: state.baselined,
    }));
  } catch {
    /* localStorage недоступен — прочитанное живёт до перезагрузки */
  }
}

export type CriticalItem = BellRow<NotificationItem>;

interface NotificationsValue {
  /** Непрочитанные критические сообщения источника (серии проверок — одной строкой). */
  critical: CriticalItem[];
  /** Последний ответ `/notifications`: срез сообщений источника (`as_of`), версия политики. */
  summary: NotificationSummary | null;
  error: unknown;
  markRead: (items: CriticalItem[]) => void;
  /** «новые прогнозы» у пункта меню: открытые карточки, не виденные в списке. */
  newForecasts: number;
  markForecastsSeen: (cardIds: string[]) => void;
}

const Ctx = createContext<NotificationsValue | null>(null);

/**
 * Уведомления ТЗ §10 без всплывающих окон. Колокольчик — только критические исходные
 * сообщения; новые прогнозы — отдельный тихий счётчик. Прочитанное — в localStorage браузера.
 */
export function NotificationsProvider({ children }: { children: ReactNode }) {
  const { generation, state } = useForecastState();
  const reference = state?.data_as_of ?? null;
  const [read, setRead] = useState<ReadState>(loadRead);
  const [cards, setCards] = useState<ForecastCardView[]>([]);
  const [runs, setRuns] = useState<Parameters<typeof isNewCard>[1]>([]);
  const [summary, setSummary] = useState<NotificationSummary | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);

  const update = useCallback((change: (current: ReadState) => ReadState) => {
    setRead((current) => {
      const next = change(current);
      saveRead(next);
      return next;
    });
  }, []);

  useEffect(() => {
    if (generation === null) return;
    const controller = new AbortController();
    listAllForecasts('open', controller.signal)
      .then((list) => {
        setCards(list.items);
        setRuns(list.runs);
      })
      .catch(() => {
        /* счётчик прогнозов просто не растёт; ошибка списка видна на странице прогноза */
      });
    return () => controller.abort();
  }, [generation]);

  useEffect(() => subscribeActiveRefresh(window, document, () => setTick((value) => value + 1)), []);
  // Серверный колокольчик (N1): окно — сутки до водяного знака опубликованного прогноза.
  useEffect(() => {
    if (!reference) return undefined;
    const controller = new AbortController();
    const since = new Date(Date.parse(reference) - NOTIFICATION_WINDOW_HOURS * 3600 * 1000).toISOString();
    getNotifications(since, controller.signal)
      .then((value) => {
        setSummary(value);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted && !isAbort(cause)) setError(cause);
      });
    return () => controller.abort();
  }, [tick, reference, generation]);

  // Первый визит: карточки прежних расчётов считаются уже известными.
  useEffect(() => {
    if (read.baselined || !cards.length) return;
    const known = cards.filter((view) => !isNewCard(view, runs)).map((view) => view.card.id);
    update((current) => ({ ...current, cards: [...current.cards, ...known], baselined: true }));
  }, [cards, runs, read.baselined, update]);

  const critical = useMemo(() => {
    const seen = new Set(read.messages);
    return bellRows(summary?.items ?? []).filter((row) => row.refIds.some((id) => !seen.has(id)));
  }, [summary, read.messages]);

  const newForecasts = useMemo(() => {
    if (!read.baselined) return 0;
    const seen = new Set(read.cards);
    return cards.filter((view) => !seen.has(view.card.id)).length;
  }, [cards, read]);

  const markRead = useCallback((items: CriticalItem[]) => {
    update((current) => ({ ...current, messages: [...current.messages, ...items.flatMap((item) => item.refIds)] }));
  }, [update]);

  const markForecastsSeen = useCallback((cardIds: string[]) => {
    update((current) => {
      const seen = new Set(current.cards);
      const fresh = cardIds.filter((id) => !seen.has(id));
      return fresh.length || !current.baselined
        ? { ...current, cards: [...current.cards, ...fresh], baselined: true }
        : current;
    });
  }, [update]);

  const value = useMemo<NotificationsValue>(() => ({
    critical, summary, error, markRead, newForecasts, markForecastsSeen,
  }), [critical, summary, error, markRead, newForecasts, markForecastsSeen]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useNotifications(): NotificationsValue {
  const value = useContext(Ctx);
  if (!value) throw new Error('NotificationsProvider is missing');
  return value;
}
