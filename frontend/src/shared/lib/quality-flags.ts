const QUALITY_FLAG_LABELS: Record<string, string> = {
  epoch_placeholder: 'Во времени или значении встретилась дата 1970; её смысл требует проверки',
  sentinel_candidate: 'Число помечено как возможное служебное значение; его смысл не установлен',
  rare_lexeme: 'Текст редок в своём месяце по правилу подготовки данных; его смысл не установлен',
  same_timestamp_conflict: 'У канала есть разные исходные тексты с одним временем источника',
  reference_unmatched: 'Канал не найден в текущем справочнике при подготовке replay',
  nonfinite_numeric: 'Числовое значение не конечно; исходный текст сохранён',
  object_mapping_from_input_unverified: 'ID объекта взят из входной партии и не сверен со справочником',
  source_clock_ahead_of_import: 'Время источника позже времени локального импорта; причина неизвестна',
};

export function qualityFlagLabel(flag: string): string {
  return QUALITY_FLAG_LABELS[flag] ?? 'Дополнительный флаг данных; значение не описано в интерфейсе';
}
