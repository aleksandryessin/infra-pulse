import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode,
} from 'react';
import { getForecastState, type ForecastState } from '../api/forecast';
import { isAbort } from '../api/http';
import { listSchemes } from '../api/scheme';
import { subscribeActiveRefresh } from '../shared/lib/use-active-refresh';
import { useSession } from './SessionProvider';

interface ForecastStateValue {
  state: ForecastState | null;
  /** Ошибка последнего опроса; прежнее состояние остаётся на экране с пометкой. */
  error: unknown;
  lastSuccessAt: Date | null;
  lastErrorAt: Date | null;
  loading: boolean;
  /** Меняется при новой публикации; страницы перечитывают списки только по нему. */
  generation: number | null;
  /** «новых N»: карточки, открытые последним расчётом (HorizonState.new_cards). */
  newCards: number;
  releasedSincePrevious: number;
  objectNames: Map<string, string>;
  objectName: (objectId: string | null | undefined) => string;
  /**
   * Объекты со схемой (`/schemes`: схема строится по линиям электропитания); null — список
   * ещё не получен. Колокольчик не ведёт на «Схему» объекта без неё (обкатка 29.09).
   */
  schemeObjects: ReadonlySet<string> | null;
  refresh: () => void;
}

const Ctx = createContext<ForecastStateValue | null>(null);

/** Опрос `/forecast-state` раз в 30 с (и при возврате к вкладке), UI-03. */
export function ForecastStateProvider({ children }: { children: ReactNode }) {
  const { status } = useSession();
  const [state, setState] = useState<ForecastState | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [lastSuccessAt, setLastSuccessAt] = useState<Date | null>(null);
  const [lastErrorAt, setLastErrorAt] = useState<Date | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [objectNames, setObjectNames] = useState<Map<string, string>>(new Map());
  const [schemeObjects, setSchemeObjects] = useState<ReadonlySet<string> | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const ready = status === 'ready';

  useEffect(() => {
    if (!ready) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    getForecastState(controller.signal)
      .then((value) => {
        setState(value);
        setError(null);
        setLastSuccessAt(new Date());
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted || isAbort(cause)) return;
        setError(cause);
        setLastErrorAt(new Date());
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [ready, tick]);

  useEffect(() => {
    if (!ready) return;
    return subscribeActiveRefresh(window, document, () => setTick((value) => value + 1));
  }, [ready]);

  const generation = state?.generation ?? null;

  // Названия объектов: в карточке прогноза есть только object_id (запрос на contract в передаче F1).
  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController();
    listSchemes(controller.signal)
      .then((value) => {
        setObjectNames(new Map(value.items
          .filter((item) => item.object_name)
          .map((item) => [item.object_id, item.object_name as string])));
        setSchemeObjects(new Set(value.items.map((item) => item.object_id)));
      })
      .catch(() => {
        /* без названий показывается object_id */
      });
    return () => controller.abort();
  }, [ready, generation]);

  const refresh = useCallback(() => setTick((value) => value + 1), []);

  const value = useMemo<ForecastStateValue>(() => ({
    state,
    error,
    lastSuccessAt,
    lastErrorAt,
    loading,
    generation,
    newCards: (state?.horizons ?? []).reduce((sum, item) => sum + item.new_cards, 0) ?? 0,
    releasedSincePrevious: (state?.horizons ?? []).reduce((sum, item) => sum + item.released_since_previous, 0) ?? 0,
    objectNames,
    objectName: (objectId) => (objectId ? objectNames.get(objectId) ?? objectId : 'объект не привязан'),
    schemeObjects,
    refresh,
  }), [state, error, lastSuccessAt, lastErrorAt, loading, generation, objectNames, schemeObjects, refresh]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useForecastState(): ForecastStateValue {
  const value = useContext(Ctx);
  if (!value) throw new Error('ForecastStateProvider is missing');
  return value;
}
