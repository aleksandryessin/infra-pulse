import { useEffect, useRef, useState } from 'react';
import { Button, Select } from 'antd';
import { IconFileUpload } from '@tabler/icons-react';
import { Link } from 'react-router-dom';
import { ApiError } from '../../api/http';
import {
  MAX_IMPORT_BYTES, TERMINAL_IMPORT_STATUSES, getImport, listImports, uploadImport, type ImportFile, type ImportFormat,
} from '../../api/imports';
import { useForecastState } from '../../providers/ForecastStateProvider';
import {
  IMPORT_ERROR_LABELS, IMPORT_STAGE_LABELS, IMPORT_STATUS_LABELS, QUARANTINE_LABELS, UPLOAD_FORMAT_LABELS,
  importFormatLabel,
} from '../../shared/config/labels';
import { ROUTES } from '../../shared/config/routes';
import { DEMO_MARK, DEMO_MARK_HINT } from '../../shared/config/wording';
import { fmtBytes, fmtCount, fmtDateTime, fmtSeconds, fmtShortDateTime } from '../../shared/lib/format';
import { useAsync } from '../../shared/lib/use-async';
import { StateMessage } from '../../shared/ui/StateMessage';
import './UploadPage.css';

const POLL_MS = 2000;
const STATUS_ORDER = ['queued', 'parsing', 'imported', 'recomputing', 'published'] as const;

/** Справочник не пересчитывает прогноз: «новых 0, снято 0» у него ничего не значит (P3 29.09). */
function isReference(item: ImportFile): boolean {
  return item.format.startsWith('reference_');
}

function changesText(item: ImportFile): string {
  if (item.status === 'duplicate') return 'без изменений';
  if (item.status !== 'published') return '—';
  if (isReference(item)) return 'новая версия справочника';
  return `новых ${(item.new_card_ids ?? []).length}, снято ${(item.released_card_ids ?? []).length}`;
}

function elapsedSeconds(item: ImportFile): number | null {
  if (!item.finished_at) return null;
  return (Date.parse(item.finished_at) - Date.parse(item.uploaded_at)) / 1000;
}

function CardLinks({ ids }: { ids: string[] }) {
  return (
    <ul className="upload-links">
      {ids.map((id) => (
        <li key={id}><Link to={`${ROUTES.forecast}/${encodeURIComponent(id)}?back=${encodeURIComponent(ROUTES.upload)}`}>карточка {id}</Link></li>
      ))}
    </ul>
  );
}

/** Отчёт одной загрузки: этапы со временем → итог строк → «Что изменилось». */
function ImportReport({ item }: { item: ImportFile }) {
  const elapsed = elapsedSeconds(item);
  const timings = item.timings ?? [];
  const reasons = item.quarantine_reasons ?? {};
  const sample = item.quarantine_sample ?? [];
  const releasedIds = item.released_card_ids ?? [];
  const newIds = item.new_card_ids ?? [];
  const statusIndex = STATUS_ORDER.indexOf(item.status as (typeof STATUS_ORDER)[number]);
  return (
    <div className="import-report">
      <div className="import-report__head">
        <strong>{item.file_name}</strong>
        <span className="muted"> · {importFormatLabel(item)} · {fmtBytes(item.size_bytes)}</span>
        {item.simulated ? <span className="text-label" title={DEMO_MARK_HINT}>{DEMO_MARK}</span> : null}
      </div>

      <section>
        <h3>Этапы</h3>
        <ol className="import-steps">
          {STATUS_ORDER.map((status, index) => (
            <li key={status} className={statusIndex >= index ? 'is-done' : ''} aria-current={item.status === status ? 'step' : undefined}>
              {IMPORT_STATUS_LABELS[status]}
            </li>
          ))}
        </ol>
        {item.status === 'duplicate' || item.status === 'failed' ? (
          <p><strong>{IMPORT_STATUS_LABELS[item.status]}</strong>
            {item.status === 'failed' && item.error_code ? `: ${IMPORT_ERROR_LABELS[item.error_code] ?? item.error_code}` : ''}
            {item.status === 'duplicate' ? ` — тот же файл (SHA-256) уже загружен как ${item.duplicate_of}; ничего не изменилось.` : ''}
          </p>
        ) : null}
        {timings.length ? (
          <table className="plain-table upload-timings">
            <thead><tr><th scope="col">Этап</th><th scope="col">Время</th></tr></thead>
            <tbody>
              {timings.map((timing) => (
                <tr key={timing.stage}><td>{IMPORT_STAGE_LABELS[timing.stage] ?? timing.stage}</td><td>{fmtSeconds(timing.seconds)}</td></tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <p className="muted small">
          Загружен {fmtDateTime(item.uploaded_at)} ({item.uploaded_by})
          {item.finished_at
            ? `, завершён ${fmtDateTime(item.finished_at)}; замерено от загрузки до завершения: ${fmtSeconds(elapsed)}`
            : item.simulated ? ', в демонстрации файл не обрабатывается' : ', обработка идёт'}.
        </p>
      </section>

      {item.rows_total !== null && item.rows_total !== undefined ? (
        <section>
          <h3>Строки</h3>
          <dl className="kv">
            <dt>Всего</dt><dd>{fmtCount(item.rows_total)}</dd>
            <dt>Принято</dt><dd>{fmtCount(item.rows_accepted)}</dd>
            <dt>Дубли</dt><dd>{fmtCount(item.rows_duplicate)}</dd>
            <dt>Отклонено</dt>
            <dd>
              {fmtCount(item.rows_quarantined)}
              {Object.keys(reasons).length ? (
                <span className="muted"> — {Object.entries(reasons).map(([reason, count]) => `${QUARANTINE_LABELS[reason] ?? reason}: ${count}`).join(', ')}</span>
              ) : null}
            </dd>
            <dt>Неизвестные каналы</dt><dd>{fmtCount(item.unknown_channels)}{item.unknown_channels ? ' (записи сохранены без объекта)' : ''}</dd>
            <dt>Период событий</dt><dd>{item.event_from ? `${fmtDateTime(item.event_from)} — ${fmtDateTime(item.event_to)}` : '—'}</dd>
            <dt>Справочник</dt><dd>{item.reference_version ?? '—'}</dd>
            <dt>SHA-256</dt><dd className="upload-sha">{item.sha256}</dd>
          </dl>
          {sample.length ? (
            <details>
              <summary>Примеры отклонённых строк ({sample.length})</summary>
              <table className="plain-table">
                <thead><tr><th scope="col">Строка</th><th scope="col">Причина</th><th scope="col">Фрагмент</th></tr></thead>
                <tbody>
                  {sample.map((sample) => (
                    <tr key={sample.line_no}><td>{sample.line_no}</td><td>{QUARANTINE_LABELS[sample.reason] ?? sample.reason}</td><td><code>{sample.raw_excerpt}</code></td></tr>
                  ))}
                </tbody>
              </table>
            </details>
          ) : null}
        </section>
      ) : null}

      {item.status === 'published' || item.status === 'duplicate' ? (
        <section>
          <h3>Что изменилось</h3>
          {item.status === 'duplicate' ? <p>Ничего: повторная загрузка того же файла не добавляет строк и не меняет список.</p> : isReference(item) ? (
            <p>
              Справочник стал действующим{item.reference_version ? ` (версия ${item.reference_version})` : ''}. Карточки
              прогноза загрузка справочника не меняет.
            </p>
          ) : (
            <div className="upload-changes">
              <div>
                <strong>Сняты по событию: {releasedIds.length}</strong>
                {releasedIds.length ? <CardLinks ids={releasedIds} /> : null}
              </div>
              <div>
                <strong>Новые карточки: {newIds.length}</strong>
                {newIds.length ? <CardLinks ids={newIds} /> : null}
              </div>
              <div>
                <strong>Без изменений:</strong> остальные открытые карточки.
                {item.forecast_generation ? <span className="muted small"> Публикация № {item.forecast_generation}.</span> : null}
              </div>
            </div>
          )}
          {releasedIds.length ? (
            <p className="muted small">Строка файла, по которой карточка снята, видна в карточке и журнале; в отчёт загрузки она не входит.</p>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

export default function UploadPage() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [format, setFormat] = useState<ImportFormat>('journal_csv');
  const [current, setCurrent] = useState<ImportFile | null>(null);
  const [uploadError, setUploadError] = useState<unknown>(null);
  const [pollError, setPollError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [historyTick, setHistoryTick] = useState(0);
  const [pollTick, setPollTick] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const history = useAsync((signal) => listImports(undefined, signal), [historyTick]);
  const { refresh } = useForecastState();
  const tooLarge = file !== null && file.size > MAX_IMPORT_BYTES;

  // Опрос состояния загрузки до завершения: queued → … → published / duplicate / failed.
  useEffect(() => {
    if (!current || TERMINAL_IMPORT_STATUSES.includes(current.status)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      getImport(current.id, controller.signal)
        .then((next) => {
          setCurrent(next);
          setPollError(null);
          if (TERMINAL_IMPORT_STATUSES.includes(next.status)) {
            setHistoryTick((value) => value + 1);
            refresh();
          }
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setPollError(error);
          // 404 — сервер не знает загрузку (fixture не хранит файл): опрос прекращается.
          if (!(error instanceof ApiError && error.status === 404)) setPollTick((value) => value + 1);
        });
    }, POLL_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [current, pollTick, refresh]);

  const upload = async () => {
    if (!file || tooLarge) return;
    setBusy(true);
    setUploadError(null);
    setPollError(null);
    try {
      const created = await uploadImport(file, format);
      setCurrent(created);
      setSelectedId(null);
    } catch (error) {
      setUploadError(error);
    } finally {
      setBusy(false);
    }
  };

  const pollNotFound = pollError instanceof ApiError && pollError.status === 404;
  const selected = selectedId ? history.data?.items.find((item) => item.id === selectedId) ?? null : null;
  const shown = selected ?? current;

  return (
    <div className="upload-page">
      <section className="panel">
        <header className="panel__head">
          <h2>Файл</h2>
          <span className="panel__meta">до 50 МБ; журнал событий или справочник</span>
        </header>
        <div className="panel__body upload-form">
          <label className="upload-form__field">
            <span>Вид файла</span>
            <Select<ImportFormat> value={format} onChange={setFormat} style={{ width: 280 }} aria-label="Вид файла"
              options={Object.entries(UPLOAD_FORMAT_LABELS).map(([value, label]) => ({ value: value as ImportFormat, label }))} />
          </label>
          <label className="upload-form__field">
            <span>Файл CSV или XLSX</span>
            <input ref={inputRef} type="file" accept=".csv,text/csv,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
          </label>
          <Button type="primary" icon={<IconFileUpload size={18} />} onClick={() => { void upload(); }} disabled={!file || tooLarge} loading={busy}>
            Загрузить
          </Button>
          {file ? <span className="muted">{file.name} · {fmtBytes(file.size)}</span> : null}
        </div>
        {tooLarge ? <div className="panel__body"><StateMessage compact kind="error" title="Файл больше 50 МБ — сервер его не примет" /></div> : null}
        {uploadError ? (
          <div className="panel__body">
            <StateMessage compact kind="error" title="Файл не загружен" error={uploadError} onRetry={() => { void upload(); }} />
          </div>
        ) : null}
      </section>

      {current && !selected ? (
        <section className="panel" aria-live="polite">
          <header className="panel__head">
            <h2>Отчёт загрузки</h2>
            <span className="panel__meta">{IMPORT_STATUS_LABELS[current.status] ?? current.status}</span>
          </header>
          <div className="panel__body">
            {pollNotFound && current.simulated ? (
              <StateMessage compact kind="empty" title="Демонстрация: файл не сохранён и не обрабатывается">
                Пример полного отчёта — в истории загрузок ниже.
              </StateMessage>
            ) : pollError ? (
              <StateMessage compact kind="stale" title="Состояние загрузки не обновилось" error={pollError}>
                Показано последнее известное состояние; опрос повторится.
              </StateMessage>
            ) : null}
            <ImportReport item={current} />
          </div>
        </section>
      ) : null}

      {selected ? (
        <section className="panel">
          <header className="panel__head">
            <h2>Отчёт загрузки из истории</h2>
            <Button size="small" onClick={() => setSelectedId(null)}>Скрыть</Button>
          </header>
          <div className="panel__body"><ImportReport item={selected} /></div>
        </section>
      ) : null}

      <section className="panel">
        <header className="panel__head">
          <h2>История загрузок</h2>
          <span className="panel__meta">{history.data ? `${history.data.items.length} из ${history.data.total}` : ''}</span>
        </header>
        {history.loading ? <div className="panel__body"><StateMessage compact kind="loading" title="Загрузка истории…" /></div> : null}
        {history.error && !history.data ? (
          <div className="panel__body"><StateMessage compact kind="error" title="История загрузок недоступна" error={history.error} onRetry={history.reload} /></div>
        ) : null}
        {history.data && !history.data.items.length ? <div className="panel__body"><p className="muted">Загрузок ещё не было.</p></div> : null}
        {history.data?.items.length ? (
          <table className="plain-table upload-history">
            <thead>
              <tr><th scope="col">Загружен</th><th scope="col">Файл</th><th scope="col">Состояние</th><th scope="col">Принято / всего</th><th scope="col">Время</th><th scope="col">Изменения</th></tr>
            </thead>
            <tbody>
              {history.data.items.map((item) => (
                <tr key={item.id} className={shown?.id === item.id ? 'is-selected' : undefined}>
                  <td>{fmtShortDateTime(item.uploaded_at)}</td>
                  <td>
                    <button type="button" className="link-button" onClick={() => setSelectedId(item.id)}>{item.file_name}</button>
                    <div className="muted small">{importFormatLabel(item)}</div>
                  </td>
                  <td>{IMPORT_STATUS_LABELS[item.status] ?? item.status}{item.error_code ? `: ${IMPORT_ERROR_LABELS[item.error_code] ?? item.error_code}` : ''}</td>
                  <td>{item.rows_total !== null && item.rows_total !== undefined ? `${fmtCount(item.rows_accepted)} / ${fmtCount(item.rows_total)}` : '—'}</td>
                  <td>{fmtSeconds(elapsedSeconds(item))}</td>
                  <td>{changesText(item)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </section>
    </div>
  );
}
