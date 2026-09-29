type ObjectBinding = { object_id?: string | null };

export function objectBindingEvidence(message: ObjectBinding, received: boolean): string {
  if (!message.object_id) return 'Привязка к объекту неизвестна.';
  if (received) return 'ID объекта указан во входной партии; справочником не подтверждён.';
  return 'ID объекта взят из текущего справочника; принадлежность на дату сообщения не подтверждена.';
}
