/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DATA_MODE?: 'fixture' | 'replay' | 'received';
  /** '1' — показать ссылку «Графики сигналов» (Grafana) аналитику и администратору. */
  readonly VITE_GRAFANA_LINK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
