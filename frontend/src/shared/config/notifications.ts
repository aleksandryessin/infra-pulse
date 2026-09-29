/**
 * Критические исходные сообщения (решение технолога 27.09). Колокольчик берёт их с сервера
 * (`/notifications`, политика N1); клиентские правила ниже нужны блоку «Сейчас», когда в
 * данных нет отметки «тревожное» (CSV Приложения 1 ТЗ, `alarm` = NULL) — политика v1.
 *
 * Правила — временная константа до уточнения технолога. Сравнение по точному тексту
 * записи (без учёта регистра и пробелов по краям) и, где указано, по типу датчика.
 * Отметка «тревожное» не учитывается: «Обнаружен газ» входит и при alarm=false.
 * Не входят: «Неисправен», «Обесточен», двери и люки, пороги температуры. Охранные сработки и
 * аномальная температура — аварии по классификации заказчика (28.09), но в правило v1 не входят:
 * это следующий шаг вместе с сервером. Тип насоса — «Состояние насоса», как в журнале и в
 * правиле сервера `flood_pump`; правило (а) сервера (без «Обесточен»/«Неисправен» в ±10 мин)
 * клиент не проверяет.
 * Тексты срабатывания теплового и ручного извещателей и датчика затопления («Не замкнут»)
 * взяты из инвентаризации сигналов 2025 (data-science/reports/dispatch-signal-inventory-2026-09-24.json),
 * справочником состояний не подтверждены.
 */
export interface CriticalRule {
  text: string;
  /** null — любой тип датчика. */
  sensorTypes: string[] | null;
  label: string;
}

export const CRITICAL_SOURCE_RULES: CriticalRule[] = [
  { text: 'Обнаружен дым', sensorTypes: null, label: 'дым' },
  { text: 'Обнаружен газ', sensorTypes: null, label: 'газ' },
  { text: 'Не замкнут', sensorTypes: ['Тепловой датчик'], label: 'срабатывание теплового извещателя' },
  { text: 'Не замкнут', sensorTypes: ['Ручной извещатель'], label: 'срабатывание ручного извещателя' },
  { text: 'Затоплен', sensorTypes: ['Состояние насоса', 'Датчик затопления'], label: 'затопление' },
  { text: 'Не замкнут', sensorTypes: ['Датчик затопления'], label: 'срабатывание датчика затопления' },
];

function norm(value: string | null | undefined): string {
  return (value ?? '').trim().toLocaleLowerCase('ru');
}

export function criticalRule(message: { value_raw: string; sensor_type?: string | null }): CriticalRule | null {
  const text = norm(message.value_raw);
  const type = norm(message.sensor_type);
  return CRITICAL_SOURCE_RULES.find((rule) => norm(rule.text) === text
    && (rule.sensorTypes === null || rule.sensorTypes.some((item) => norm(item) === type))) ?? null;
}

/** Серия: столько и более датчиков объекта в пределах окна — «похоже на проверку». Не скрывается. */
export const SERIES_MIN_CHANNELS = 3;
export const SERIES_WINDOW_SECONDS = 60;

/** Сколько последних исходных записей просматривается на клиенте (блок «Сейчас» без отметки «тревожное»). */
export const NOTIFICATION_ATTENTION_WINDOW = 100;
/** Колокольчик читает серверный `/notifications` за это окно до водяного знака прогноза. */
export const NOTIFICATION_WINDOW_HOURS = 24;
export const NOTIFICATIONS_STORAGE_KEY = 'infra-notifications-v3';
