import type { components } from './schema';
import { apiRequest, query } from './http';

type S = components['schemas'];
export type ImportFile = S['ImportFile'];
export type ImportList = S['ImportList'];
export type ImportFormat = ImportFile['format'];
export type ImportStatus = ImportFile['status'];

/** Предел сервера (`MAX_IMPORT_BYTES`), проверяется и до отправки. */
export const MAX_IMPORT_BYTES = 50 * 1024 * 1024;
export const TERMINAL_IMPORT_STATUSES: ImportStatus[] = ['published', 'duplicate', 'failed'];

export function uploadImport(file: File, format: ImportFormat): Promise<ImportFile> {
  const form = new FormData();
  form.set('file', file);
  form.set('format', format);
  return apiRequest<ImportFile>('/api/v1/imports', { method: 'POST', form });
}

export function listImports(cursor?: string, signal?: AbortSignal): Promise<ImportList> {
  return apiRequest<ImportList>(`/api/v1/imports${query({ cursor, limit: 25 })}`, { signal });
}

export function getImport(importId: string, signal?: AbortSignal): Promise<ImportFile> {
  return apiRequest<ImportFile>(`/api/v1/imports/${encodeURIComponent(importId)}`, { signal });
}
