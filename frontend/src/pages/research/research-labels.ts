/** Подписи раздела «Исследование» — только в его чанке. */

export const RESEARCH_STATUS_LABELS: Record<string, string> = {
  in_product: 'в продукте',
  research_only: 'только исследование',
  needs_more_data: 'нужно больше данных',
};

/** Ступени сравнения — нейтральные названия без внутреннего жаргона. */
export const LADDER_STEP_LABELS: Record<string, string> = {
  ceiling: 'верхняя граница при известных событиях',
  pair_oracle: 'верхняя граница при известном исходе',
  model: 'модель',
  static_list: 'статистический список',
  persistence: 'повтор прошлого периода',
  random_list: 'случайный список',
};

export const EVIDENCE_SCOPE_LABELS: Record<string, string> = {
  fixture: 'синтетика для проверки интерфейса',
  demo_period: 'иллюстрация на истории, не метрика',
  live_uploads: 'по загруженным данным; выборки малы',
};
