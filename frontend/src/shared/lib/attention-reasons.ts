export function storedAttentionReasons(codes: readonly string[] | null | undefined): string {
  if (!codes?.length) return 'для ранней заметки не сохранено';
  return codes.map((code) => {
    if (code === 'source_alarm_true') return 'исходное поле «тревожное» = true';
    if (code === 'exact_text_candidate') return 'точная пара типа датчика и исходного текста из каталога кандидатов';
    if (code === 'received_order') return 'внутри вида — по времени доступности; тяжесть не оценивалась';
    return `неизвестный код: ${code}`;
  }).join(' · ');
}
