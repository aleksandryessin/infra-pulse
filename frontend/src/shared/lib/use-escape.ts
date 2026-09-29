import { useEffect } from 'react';

function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
  return Boolean(target.closest('.ant-select, .ant-picker, [role="combobox"], [role="listbox"], .ant-modal'));
}

/** Esc вне полей ввода: закрыть полосу сведений или вернуться из карточки. */
export function useEscape(handler: () => void, enabled = true): void {
  useEffect(() => {
    if (!enabled) return;
    const listener = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || isEditable(event.target)) return;
      handler();
    };
    window.addEventListener('keydown', listener);
    return () => window.removeEventListener('keydown', listener);
  }, [handler, enabled]);
}
