/**
 * Текст черновика для энергетика, сохранённый в этой вкладке (В-4/В-5 аудита диспетчера 29.09).
 * Сервер хранит текст вместе с решением, но в ответе и истории решений отдаёт только отметку
 * «не отправлен» (`draft_id`); поэтому после сохранения текст показывается из того, что ушло в
 * запрос. После перезагрузки страницы остаётся только отметка.
 */
const texts = new Map<string, string>();

const key = (forecastId: string, revision: number) => `${forecastId}#${revision}`;

export function rememberDraftText(forecastId: string, revision: number, text: string | null | undefined): void {
  if (text) texts.set(key(forecastId, revision), text);
}

export function draftTextFor(forecastId: string, revision: number): string | null {
  return texts.get(key(forecastId, revision)) ?? null;
}
