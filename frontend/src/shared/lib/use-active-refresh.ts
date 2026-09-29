import { useEffect, type Dispatch, type SetStateAction } from 'react';

type RefreshWindow = Pick<Window, 'setInterval' | 'clearInterval' | 'addEventListener' | 'removeEventListener'>;
type RefreshDocument = Pick<Document, 'visibilityState' | 'addEventListener' | 'removeEventListener'>;

export function subscribeActiveRefresh(
  browserWindow: RefreshWindow,
  browserDocument: RefreshDocument,
  onRefresh: () => void,
  now: () => number = Date.now,
): () => void {
  let lastRefreshAt = now();
  const refresh = () => {
    if (browserDocument.visibilityState !== 'visible') return;
    const current = now();
    if (current - lastRefreshAt < 1_000) return;
    lastRefreshAt = current;
    onRefresh();
  };
  const timer = browserWindow.setInterval(refresh, 30_000);
  browserWindow.addEventListener('focus', refresh);
  browserDocument.addEventListener('visibilitychange', refresh);
  return () => {
    browserWindow.clearInterval(timer);
    browserWindow.removeEventListener('focus', refresh);
    browserDocument.removeEventListener('visibilitychange', refresh);
  };
}

/** Poll while visible and recheck immediately after the operator returns. */
export function useActiveRefresh(
  enabled: boolean,
  setRevision: Dispatch<SetStateAction<number>>,
): void {
  useEffect(() => {
    if (!enabled) return;
    return subscribeActiveRefresh(window, document, () => setRevision((value) => value + 1));
  }, [enabled, setRevision]);
}
