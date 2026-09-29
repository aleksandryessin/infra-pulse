import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { Input } from 'antd';
import { IconCalendar, IconChevronLeft, IconChevronRight } from '@tabler/icons-react';
import {
  RU_MONTHS, RU_WEEKDAYS, calendarDays, formatRuDate, parseRuDate, ruDayLabel, shiftIsoDay, shiftMonth,
} from '../lib/ru-date';

const ARROW_DAYS: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };

/**
 * Дата `дд.мм.гггг` независимо от языка браузера; значение — ISO-день или пусто.
 * Кнопка открывает свой календарь под полем (замечание владельца 29.09: системный `showPicker`
 * скрытого поля в Safari не закрывался кликом вне). Календарь закрывается кликом вне поля, Esc,
 * уходом фокуса и выбором дня; Esc не доходит до обработчиков страницы («назад»).
 */
export function RuDateInput({
  id, value, onChange, label, defaultMonth,
}: {
  /** Для `<label htmlFor>`: подпись связывается с текстовым полем. */
  id?: string;
  value: string;
  onChange: (iso: string | null) => void;
  /** Доступное имя поля, например «Выдано с». */
  label: string;
  /** ISO-день, месяц которого календарь показывает при пустом поле (например, срез данных). */
  defaultMonth?: string | null;
}) {
  const [text, setText] = useState(() => formatRuDate(value));
  const [invalid, setInvalid] = useState(false);
  const [open, setOpen] = useState(false);
  const [month, setMonth] = useState('');
  const [focused, setFocused] = useState('');
  const root = useRef<HTMLSpanElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const grid = useRef<HTMLDivElement>(null);
  // Фокус переносится на день только при открытии и стрелками, не при перелистывании кнопками.
  const moveFocus = useRef(false);

  // Значение сменили снаружи (сброс фильтров, календарь) — показываем его.
  useEffect(() => {
    setText(formatRuDate(value));
    setInvalid(false);
  }, [value]);

  // Клик вне поля и календаря и Esc закрывают календарь. Esc перехватывается раньше
  // обработчиков страницы (`useEscape` пропускает событие с `defaultPrevented`).
  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      toggle.current?.focus();
    };
    document.addEventListener('pointerdown', onPointer, true);
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('pointerdown', onPointer, true);
      document.removeEventListener('keydown', onKey, true);
    };
  }, [open]);

  // Фокус на выбранном (или первом) дне после открытия и после шага стрелкой.
  useEffect(() => {
    if (!open || !focused || !moveFocus.current) return;
    moveFocus.current = false;
    grid.current?.querySelector<HTMLButtonElement>(`[data-iso="${focused}"]`)?.focus();
  }, [open, focused, month]);

  const start = () => {
    const anchor = value || defaultMonth || new Date().toISOString().slice(0, 10);
    setMonth(anchor.slice(0, 7));
    setFocused(value || `${anchor.slice(0, 7)}-01`);
    moveFocus.current = true;
    setOpen(true);
  };

  const choose = (iso: string) => {
    setOpen(false);
    toggle.current?.focus();
    if (iso !== value) onChange(iso);
  };

  const onGridKey = (event: ReactKeyboardEvent) => {
    const step = ARROW_DAYS[event.key];
    if (!step || !focused) return;
    event.preventDefault();
    const next = shiftIsoDay(focused, step);
    if (!next) return;
    moveFocus.current = true;
    setFocused(next);
    setMonth(next.slice(0, 7));
  };

  const [year, monthIndex] = month ? month.split('-').map(Number) : [0, 1];
  const days = open ? calendarDays(month) : [];
  // Один день сетки доступен по Tab: выбранный стрелками, иначе первое число месяца.
  const tabbable = days.some((day) => day.iso === focused) ? focused : `${month}-01`;

  return (
    <span
      className="ru-date"
      ref={root}
      onBlur={(event) => {
        // Фокус ушёл из поля и календаря (Tab) — календарь закрывается.
        if (open && event.relatedTarget && !root.current?.contains(event.relatedTarget as Node)) setOpen(false);
      }}
    >
      <Input
        id={id}
        value={text}
        placeholder="дд.мм.гггг"
        inputMode="numeric"
        maxLength={10}
        status={invalid ? 'error' : undefined}
        aria-label={label}
        aria-invalid={invalid || undefined}
        title={invalid ? 'Дата в формате дд.мм.гггг, например 01.06.2026' : undefined}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          const parsed = parseRuDate(next);
          if (parsed === undefined) return;
          setInvalid(false);
          if (parsed && open) {
            setMonth(parsed.slice(0, 7));
            setFocused(parsed);
          }
          if ((parsed ?? '') !== value) onChange(parsed);
        }}
        onBlur={() => setInvalid(parseRuDate(text) === undefined)}
        suffix={(
          <button type="button" ref={toggle} className="ru-date__calendar" onClick={() => (open ? setOpen(false) : start())}
            aria-label={`${label}: календарь`} aria-haspopup="dialog" aria-expanded={open} title="Календарь">
            <IconCalendar size={16} aria-hidden />
          </button>
        )}
      />
      {open ? (
        <div className="ru-date__popup" role="dialog" aria-label={`${label}: выбор даты`}>
          <div className="ru-date__head">
            <button type="button" className="ru-date__nav" onClick={() => setMonth(shiftMonth(month, -1))} aria-label="Предыдущий месяц">
              <IconChevronLeft size={16} aria-hidden />
            </button>
            <span className="ru-date__month" aria-live="polite">{RU_MONTHS[monthIndex - 1]} {year}</span>
            <button type="button" className="ru-date__nav" onClick={() => setMonth(shiftMonth(month, 1))} aria-label="Следующий месяц">
              <IconChevronRight size={16} aria-hidden />
            </button>
          </div>
          <div className="ru-date__grid" ref={grid} onKeyDown={onGridKey}>
            {RU_WEEKDAYS.map((day) => <span key={day} className="ru-date__weekday" aria-hidden>{day}</span>)}
            {days.map((day) => (
              <button
                key={day.iso}
                type="button"
                data-iso={day.iso}
                className={`ru-date__day${day.inMonth ? '' : ' ru-date__day--other'}${day.iso === value ? ' ru-date__day--selected' : ''}`}
                tabIndex={day.iso === tabbable ? 0 : -1}
                aria-label={ruDayLabel(day.iso)}
                aria-pressed={day.iso === value}
                onClick={() => choose(day.iso)}
              >
                {day.day}
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </span>
  );
}
