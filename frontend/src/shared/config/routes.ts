import type { Permission } from './permissions';
import { SCHEME_PAGE_TITLE } from './wording';

/** Разделы SPA. Доступ к разделу задаёт право; сервер проверяет его повторно. */
export const ROUTES = {
  forecast: '/forecast',
  scheme: '/scheme',
  journal: '/journal',
  research: '/research',
  upload: '/upload',
  login: '/login',
  /** Исходные сообщения replay/received (прежняя очередь), без пункта меню. */
  messages: '/queue',
  messagesJournal: '/queue/journal',
} as const;

export type SectionKey = 'forecast' | 'scheme' | 'journal' | 'research' | 'upload' | 'messages';

export interface Section {
  key: SectionKey;
  path: string;
  title: string;
  /** Заголовок страницы, если он длиннее пункта меню («Схема» → «Схема объектов»). */
  heading?: string;
  permission: Permission;
  /**
   * Показывать в меню. «Исходные сообщения» (`/queue`) — служебная страница replay/received с
   * отладочными полями: ни меню, ни разделы диспетчера на неё не ссылаются (журнал ведёт на схему,
   * I4 ТЗ-аудита 29.09); открывается только прямым адресом.
   */
  inMenu: boolean;
}

export const SECTIONS: Section[] = [
  { key: 'forecast', path: ROUTES.forecast, title: 'Прогноз', permission: 'read', inMenu: true },
  { key: 'scheme', path: ROUTES.scheme, title: 'Схема', heading: SCHEME_PAGE_TITLE, permission: 'read', inMenu: true },
  { key: 'journal', path: ROUTES.journal, title: 'Журнал', permission: 'read', inMenu: true },
  { key: 'research', path: ROUTES.research, title: 'Исследование', permission: 'research', inMenu: true },
  { key: 'upload', path: ROUTES.upload, title: 'Загрузка', permission: 'import', inMenu: true },
  { key: 'messages', path: ROUTES.messages, title: 'Исходные сообщения', permission: 'read', inMenu: false },
];

export function sectionFor(pathname: string): Section | undefined {
  return SECTIONS.find((section) => pathname === section.path || pathname.startsWith(`${section.path}/`));
}

/** Исходные сообщения и журнал заметок работают только с PostgreSQL (replay/received). */
export const SOURCE_MESSAGES_AVAILABLE =
  import.meta.env.VITE_DATA_MODE === 'replay' || import.meta.env.VITE_DATA_MODE === 'received';

/** Grafana на стенде (профиль `analytics`, `deploy/README.md` §13): Caddy отдаёт её по этому пути. */
export const GRAFANA_PATH = '/grafana/';
/**
 * Ссылка «Графики сигналов» (замечание владельца 29.09): дашборд «Сигналы: текстовые состояния»
 * (на стенде числовых каналов нет) за 21.05.2026 00:00 … 01.06.2026 23:59:59 МСК — в этом окне
 * видны переходы «Обесточен» / «Норма». Тот же период — окно по умолчанию в
 * `deploy/grafana/dashboards/*.json`; время в ссылке — мс эпохи (UTC), показ — по Москве.
 */
export const GRAFANA_SIGNALS_URL =
  `${GRAFANA_PATH}d/infrapulse-signals-state?from=1779310800000&to=1780347599000&timezone=Europe%2FMoscow`;
/**
 * F-07: ссылка «Графики сигналов» скрыта, пока дашборды Grafana не исправлены (B-5);
 * включается сборкой с `VITE_GRAFANA_LINK=1`.
 */
export const GRAFANA_LINK_ENABLED = import.meta.env.VITE_GRAFANA_LINK === '1';
