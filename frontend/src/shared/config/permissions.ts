import type { Role } from '../../api/auth';

export type Permission = 'read' | 'decide' | 'import' | 'research' | 'ingest' | 'report';

/**
 * Зеркало `PERMISSIONS` из `infra_pulse_core.contracts.auth` только для меню и экранов.
 * Решает сервер: каждый маршрут проверяет право и отвечает 403. Чтобы не держать копию,
 * запрошено добавить список прав в `Me` (см. передачу F1).
 */
export const PERMISSIONS: Record<Permission, readonly Role[]> = {
  read: ['dispatcher', 'analyst', 'admin'],
  decide: ['dispatcher', 'admin'],
  import: ['admin'],
  research: ['analyst', 'admin'],
  // C0.2: поток данных внешней системы и отчёты руководству; меню F1 их пока не показывает.
  ingest: ['integration', 'admin'],
  report: ['analyst', 'admin'],
};

export const ROLE_LABELS: Record<Role, string> = {
  dispatcher: 'диспетчер',
  analyst: 'аналитик',
  admin: 'администратор',
  integration: 'интеграция',
};

export function can(roles: readonly Role[], permission: Permission): boolean {
  return roles.some((role) => PERMISSIONS[permission].includes(role));
}

export function rolesAllowed(permission: Permission): string {
  return PERMISSIONS[permission].map((role) => ROLE_LABELS[role]).join(', ');
}
