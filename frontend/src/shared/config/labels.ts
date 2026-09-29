/**
 * Русские подписи кодов contract. Коды приходят из API; здесь только их отображение.
 * Неизвестный код показывается как есть, без трактовки.
 */

export const FEEDER_KIND_LABELS: Record<string, string> = {
  lighting: 'освещение',
  ventilation: 'вентиляция',
  pumps: 'насосы',
  ozk: 'ОЗК',
  other: 'прочее',
};

/** Полосы мнемосхемы: группа линий электропитания по типу из названия канала. */
export const LINE_GROUP_LABELS: Record<string, string> = {
  lighting: 'Освещение',
  ventilation: 'Вентиляция',
  pumps: 'Насосы',
  ozk: 'ОЗК',
  other: 'Прочее',
};

/** Заголовок полосы сведений о линии. */
export const LINE_TITLES: Record<string, string> = {
  lighting: 'Линия освещения',
  ventilation: 'Линия вентиляции',
  pumps: 'Линия насосов',
  ozk: 'Линия ОЗК',
  other: 'Линия электропитания',
};

export const PICKET_BASIS_LABELS: Record<string, string> = {
  reference: 'из справочника',
  channel_name: 'из названия канала',
  synthetic: 'условный (демонстрация)',
};

/**
 * Подсказки групп линий (F5; подписи групп не меняются). РО, АО и АНС — СП 265.1325800.2016,
 * п. 3.1.3 и 3.2; расшифровки ФРО, ФАО, ГРО, ФВ/В#, ФАНС, ОЗК, ЩАП и межсекционного автомата
 * подтверждены заказчиком 28.09, он же расшифровал ФТС (фидер теплосети) и ПУИ (пульт управления
 * индикацией). Слово «фидер» в интерфейсе не используется (C0.3): «линия электропитания».
 */
export const LINE_GROUP_HINTS: Record<string, string> = {
  lighting: 'Освещение: РО — рабочее, АО — аварийное (СП 265.1325800.2016). ФРО и ФАО — линии электропитания освещения, ГРО — группы рабочего освещения (подтверждено заказчиком)',
  ventilation: 'Вентиляция: ФВ — линия электропитания вентиляции; В# (например, В23) — вентилятор (подтверждено заказчиком)',
  pumps: 'Насосы: ФАНС — линия электропитания АНС, автоматической насосной станции (СП 265.1325800.2016; подтверждено заказчиком)',
  ozk: 'ОЗК — огнезадерживающие клапаны вентиляции (подтверждено заказчиком)',
  other: 'Прочее: ФТС — линия электропитания теплосети, ПУИ — пульт управления индикацией (подтверждено заказчиком); у остальных тип по названию канала не определён',
};
/** Короткое пояснение группы в легенде схемы. */
export const LINE_GROUP_LEGEND: Record<string, string> = {
  lighting: 'РО — рабочее, АО — аварийное (СП 265.1325800.2016)',
  ventilation: 'ФВ — линия электропитания вентиляции, В# — вентилятор',
  pumps: 'ФАНС — линия электропитания АНС, автоматической насосной станции (СП 265.1325800.2016)',
  ozk: 'огнезадерживающие клапаны (подтверждено заказчиком)',
  other: 'ФТС — линия электропитания теплосети, ПУИ — пульт управления индикацией; у остальных тип по названию канала',
};

/** Подсказки узлов схемы (F5; подписи узлов не меняются). */
export const LANDMARK_KIND_HINTS: Record<string, string> = {
  ats: 'АВР — автоматический ввод резерва',
  panel: 'ЩАП — щит аварийного питания с АВР (подтверждено заказчиком)',
  other: 'Межсекционный автомат — секционный автомат между секциями щита, ввод 1 и ввод 2 (подтверждено заказчиком)',
};

export const LANDMARK_KIND_LABELS: Record<string, string> = {
  input: 'ввод',
  ats: 'АВР',
  panel: 'щит',
  other: 'межсекционный',
};

export const LIST_STATE_LABELS: Record<string, string> = {
  open: 'открыта',
  released: 'снята по событию',
  expired: 'срок истёк',
};

/** Итог карточки в журнале (формулировки технолога 27.09). */
export const OUTCOME_LABELS: Record<string, string> = {
  pending: 'срок не истёк',
  realized: 'сбылась',
  not_realized: 'истекла без события',
  unknown: 'исход неизвестен',
  event_without_forecast: 'событие, прогноз не выдан',
  no_event_without_forecast: 'без события, прогноз не выдан',
};

export const UNKNOWN_REASON_LABELS: Record<string, string> = {
  source_coverage: 'нет данных источника за окно',
  policy_day: 'день исключён правилом',
  data_end: 'данные закончились до конца окна',
  label_spec_changed: 'изменилось определение события',
};

export const ABSTENTION_LABELS: Record<string, string> = {
  insufficient_history: 'недостаточно истории',
  stale_or_missing_input: 'данные не поступали или устарели',
  lookback_in_excluded_period: 'история попала в исключённый период',
  sensor_type_out_of_scope: 'тип датчика вне прогноза',
  incompatible_bundle: 'несовместимая версия расчёта',
  channel_disabled: 'канал выведен из работы',
  no_object_binding: 'канал не привязан к объекту',
};

export const COVERAGE_LABELS: Record<string, string> = {
  complete: 'полная',
  partial: 'частичная',
  insufficient: 'недостаточная',
  unknown: 'неизвестна',
};

export const VERIFICATION_LABELS: Record<string, string> = {
  source_records: 'исходные записи в сервисе',
  remote_poll: 'удалённый опрос контроллера',
  call_collector: 'звонок в коллектор',
  video: 'видеонаблюдение',
  field_visit: 'выезд на объект',
  not_checked: 'не проверялось',
};

/** Способ выдачи списка; любой другой код contract показывается как «модель» без названия библиотеки. */
export const SCORER_LABELS: Record<string, string> = {
  static_list: 'статистический список',
  persistence: 'повтор прошлого периода',
};

export function scorerLabel(scorer: string): string {
  return SCORER_LABELS[scorer] ?? 'модель';
}

export const MODE_LABELS: Record<string, string> = {
  fixture: 'Демонстрация: синтетические данные',
  replay: 'Исторический повтор, не в реальном времени',
  received: 'Источник: загрузки файлов и API',
  live: 'Источник: поток данных',
};

/**
 * Режимы, о которых шапка предупреждает всех: данные не настоящие или не в реальном времени.
 * Рабочие режимы (`received`, `live`) — служебная подпись только для администратора (замечание
 * владельца 29.09: «Загруженные файлы» диспетчеру и аналитику непонятно).
 */
const WARNING_MODES = new Set(['fixture', 'replay']);

/** Подпись режима данных в шапке для роли или `null` — не показывать. */
export function headerModeLabel(mode: string | null | undefined, admin: boolean, fromBuild = false): string | null {
  if (!mode) return admin ? 'Режим данных неизвестен' : null;
  if (!admin && !WARNING_MODES.has(mode)) return null;
  const label = MODE_LABELS[mode] ?? mode;
  return fromBuild ? `${label} (по настройке сборки)` : label;
}

export interface DecisionOption {
  code: 'R1' | 'R2' | 'R3' | 'R4' | 'R5' | 'R6' | 'R7';
  label: string;
  /** Основные действия — кнопки; остальные — в «Другое». */
  primary: boolean;
  /** R3/R4 создают черновик «не отправлен». */
  draft: boolean;
  reasons: { code: string; text: string }[];
}

/**
 * Решения по карточке «обесточивание электрооборудования объекта». Подписи — `DECISION_LABELS`
 * contract C0.4 (R3 «Сообщено энергетику»); причины — поправки технолога 27.09.
 * Основные варианты: «передано энергетику» (внутренний черновик «не отправлен»),
 * «под наблюдением», «уже известно / в работе», «нет оснований»; остальное — «другое».
 * Сопоставление с кодами R1–R7 contract и причины — предложение на согласование:
 * R3 → передано энергетику, R1 → под наблюдением, R5 → уже известно / в работе,
 * R7 → нет оснований. Причина R5.5 новая, в справочнике технолога v1 её нет.
 * Решение карточку не закрывает.
 */
export const DECISION_OPTIONS: DecisionOption[] = [
  {
    code: 'R3', label: 'Сообщено энергетику', primary: true, draft: true,
    reasons: [
      { code: 'R3.1', text: 'устойчивая или повторная потеря связи линий электропитания' },
      { code: 'R3.3', text: 'удалённая проверка не восстановила связь' },
    ],
  },
  {
    code: 'R1', label: 'Под наблюдением', primary: true, draft: false,
    reasons: [
      { code: 'R1.1', text: 'действий сейчас не требуется' },
      { code: 'R1.3', text: 'нужны новые данные' },
    ],
  },
  {
    code: 'R5', label: 'Уже известно / в работе', primary: true, draft: false,
    reasons: [
      { code: 'R5.5', text: 'уже есть заявка или работа ведётся' },
      { code: 'R5.1', text: 'ППР или ТО по графику' },
      { code: 'R5.2', text: 'работы подрядчика' },
      { code: 'R5.4', text: 'плановое отключение питания' },
    ],
  },
  {
    code: 'R7', label: 'Нет оснований', primary: true, draft: false,
    reasons: [
      { code: 'R7.1', text: 'данные устарели или неполны' },
      { code: 'R7.2', text: 'ошибка привязки канала или объекта' },
      { code: 'R7.3', text: 'канал выведен из работы («Выключен»)' },
      { code: 'R7.4', text: 'дублирует другую карточку' },
    ],
  },
  {
    code: 'R2', label: 'Проверить удалённо', primary: false, draft: false,
    reasons: [
      { code: 'R2.1', text: 'повторные потери связи линий электропитания' },
      { code: 'R2.2', text: 'линии электропитания объекта теряли связь вместе' },
      { code: 'R2.3', text: 'событие объекта на нескольких каналах' },
    ],
  },
  {
    code: 'R4', label: 'Включить объект в плановый осмотр до срока', primary: false, draft: true,
    reasons: [
      { code: 'R4.1', text: 'повторяется, не срочно' },
      { code: 'R4.2', text: 'проверить общий узел при обходе' },
    ],
  },
  {
    code: 'R6', label: 'Передать смене', primary: false, draft: false,
    reasons: [
      { code: 'R6.1', text: 'не успели разобрать' },
      { code: 'R6.2', text: 'ждём информацию' },
    ],
  },
];

/** Результат проверки после решения — `CHECK_RESULT_LABELS` contract C0.4. */
export const CHECK_RESULT_LABELS: Record<string, string> = {
  awaiting: 'ждём результат',
  fixed: 'устранено',
  no_violation: 'нарушений не найдено',
  not_done: 'проверка не проведена',
};

/** Что устранено (только при «устранено»; перечень подтверждён заказчиком 28.09) — `FOUND_LABELS` C0.4. */
export const FOUND_LABELS: Record<string, string> = {
  breaker: 'автомат',
  cable: 'кабель',
  contactor: 'контактор',
  comm_module: 'модуль связи',
  cabinet_power: 'питание шкафа',
  other: 'другое',
};

/** Причина события — только у карточки, снятой по событию (`EVENT_CAUSE_LABELS` C0.4). */
export const EVENT_CAUSE_LABELS: Record<string, string> = {
  planned_outage: 'плановое отключение',
  protection_trip: 'срабатывание защиты',
  external_grid: 'внешняя сеть',
  smvu_channel: 'канал связи СМВУ',
  unknown: 'неизвестно',
};

/**
 * Группа строки колокольчика по `rule_id` политики сервера (ответ заказчика 28.09: «свести к
 * нескольким группам»). Метка рисуется только на клиенте: contract C0.5 запрещает «пожар» в
 * `title`, и `title` не меняется. Неизвестное правило — без метки. Ключи и группы сверяет с
 * `NOTIFICATION_POLICY` сервера `backend/tests/test_notifications.py`.
 */
export const ALARM_GROUP_LABELS: Record<string, string> = {
  fire_smoke: 'Пожар',
  fire_heat: 'Пожар',
  fire_manual: 'Пожар',
  fire_test_series: 'Пожар',
  gas_detected: 'Газ',
  gas_calibration_series: 'Газ',
  flood_pump: 'Затопление',
  flood_sensor: 'Затопление',
};

export function alarmGroupLabel(ruleId: string | null | undefined): string | null {
  return ruleId ? ALARM_GROUP_LABELS[ruleId] ?? null : null;
}

export function decisionLabel(code: string | null | undefined): string {
  if (!code) return 'нет решения';
  return DECISION_OPTIONS.find((option) => option.code === code)?.label ?? code;
}

export const IMPORT_STATUS_LABELS: Record<string, string> = {
  queued: 'в очереди',
  parsing: 'разбор файла',
  imported: 'записи загружены',
  recomputing: 'пересчёт прогноза',
  published: 'опубликовано',
  duplicate: 'повтор уже загруженного файла',
  failed: 'ошибка',
};

export const IMPORT_STAGE_LABELS: Record<string, string> = {
  store: 'приём файла',
  parse: 'разбор строк',
  load: 'запись в базу',
  detect: 'поиск потерь связи',
  score: 'расчёт списка',
  publish: 'публикация',
};

/** Виды файлов, которые загружаются на странице «Загрузка» (форма выбора). */
export const UPLOAD_FORMAT_LABELS = {
  journal_csv: 'журнал событий (CSV или XLSX)',
  reference_channels_csv: 'справочник каналов (CSV или XLSX)',
  reference_objects_csv: 'справочник объектов (CSV или XLSX)',
  reference_states_csv: 'справочник состояний (CSV или XLSX)',
} as const;

/**
 * Вид загрузки в истории и отчёте: все `ImportFormat` contract плюс пачки
 * `POST /api/v1/observations` (QA 29.09, D6). У пачки формат `journal_json` и в JSON, и в XML;
 * `journal_xml` — ключ подписи для `source_container = xml` (`importFormatLabel`), не формат contract.
 */
export const IMPORT_FORMAT_LABELS: Record<string, string> = {
  ...UPLOAD_FORMAT_LABELS,
  journal_json: 'пачка API (JSON)',
  journal_xml: 'пачка API (XML)',
};

/** Подпись вида загрузки; пачка API в XML различается по `source_container`. */
export function importFormatLabel(item: { format: string; source_container?: string | null }): string {
  const key = item.format === 'journal_json' && item.source_container === 'xml' ? 'journal_xml' : item.format;
  return IMPORT_FORMAT_LABELS[key] ?? item.format;
}

export const QUARANTINE_LABELS: Record<string, string> = {
  bad_column_count: 'неверное число столбцов',
  bad_date: 'неверная дата',
  bad_time: 'неверное время',
  bad_bool: 'неверная отметка «тревожное»',
  empty_channel: 'пустой канал',
  bad_encoding: 'неверная кодировка',
  value_too_long: 'слишком длинное значение',
};

export const IMPORT_ERROR_LABELS: Record<string, string> = {
  file_too_large: 'файл больше 50 МБ',
  unknown_format: 'формат не распознан',
  bad_header: 'заголовок файла не совпадает с форматом',
  bad_encoding: 'неверная кодировка',
  no_valid_rows: 'нет ни одной корректной строки',
  reference_missing: 'не загружен справочник',
  recompute_failed: 'пересчёт прогноза не выполнен',
  internal_error: 'внутренняя ошибка обработки',
};
