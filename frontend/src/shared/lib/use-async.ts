import { useCallback, useEffect, useState, type DependencyList } from 'react';
import { isAbort } from '../../api/http';

export interface AsyncState<T> {
  data: T | null;
  error: unknown;
  /** Первая загрузка: данных ещё нет. */
  loading: boolean;
  /** Повторное чтение поверх прежних данных. */
  refreshing: boolean;
  /** Время последнего успешного ответа. */
  loadedAt: Date | null;
  reload: () => void;
}

/**
 * Чтение API с отменой. При ошибке повторного чтения прежние данные остаются
 * на экране, а ошибка показывается рядом (stale), без подмены данных.
 */
export function useAsync<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
  enabled = true,
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(enabled);
  const [refreshing, setRefreshing] = useState(false);
  const [loadedAt, setLoadedAt] = useState<Date | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setRefreshing(true);
    load(controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setData(value);
        setError(null);
        setLoadedAt(new Date());
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted || isAbort(cause)) return;
        setError(cause);
      })
      .finally(() => {
        if (controller.signal.aborted) return;
        setLoading(false);
        setRefreshing(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, enabled]);

  const reload = useCallback(() => setTick((value) => value + 1), []);
  return { data, error, loading, refreshing, loadedAt, reload };
}
