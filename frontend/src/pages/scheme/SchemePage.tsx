import { useCallback, useEffect, useMemo } from 'react';
import { useLocation, useSearchParams } from 'react-router-dom';
import { listAllForecasts } from '../../api/forecast';
import { getScheme, listSchemes } from '../../api/scheme';
import { useForecastState } from '../../providers/ForecastStateProvider';
import { fmtShortDateTime } from '../../shared/lib/format';
import { useAsync } from '../../shared/lib/use-async';
import { useEscape } from '../../shared/lib/use-escape';
import { StateMessage } from '../../shared/ui/StateMessage';
import { ObjectMnemo } from '../../widgets/object-mnemo/ObjectMnemo';
import type { LineKind } from '../../widgets/object-mnemo/mnemo-layout';
import {
  groupsFromParam, schemeParams, selectionFromParam, selectionGroup, withSelectionGroup, type MnemoSelection,
} from '../../widgets/object-mnemo/selection';
import { ObjectPicker } from '../../widgets/object-picker/ObjectPicker';
import { attentionOrder, defaultObject } from '../../widgets/object-picker/attention-order';
import './SchemePage.css';

/**
 * Схема — мнемосхема одного объекта (F2). Слева выбор объекта и «Требуют внимания»,
 * справа схема объекта из одного `GET /schemes/{object}`. Объект, выбор и видимые группы
 * линий — в URL. По умолчанию — первый объект с тревогой СМВУ, иначе с ближайшим сроком прогноза.
 * Выбор линии или тревоги скрытой группы показывает эту группу (P1-9).
 */
export function SchemePage() {
  const { generation, objectName, state } = useForecastState();
  const [params, setParams] = useSearchParams();
  const location = useLocation();

  const list = useAsync((signal) => listSchemes(signal), [generation]);
  const open = useAsync((signal) => listAllForecasts('open', signal), [generation]);
  // F-04: снятые за 7 сут до данных прогноза — как счётчик «Снято по событию за 7 сут».
  const dataAt = state?.forecast_data_as_of ?? state?.data_as_of ?? null;
  const releasedSince = dataAt ? new Date(Date.parse(dataAt) - 7 * 24 * 3600 * 1000).toISOString() : null;
  const released = useAsync(
    (signal) => listAllForecasts('released', signal, releasedSince ?? undefined), [generation, releasedSince], Boolean(releasedSince),
  );

  const entries = useMemo(() => attentionOrder(
    list.data?.items ?? [],
    (open.data?.items ?? []).map((view) => ({
      id: view.card.id, object_id: view.card.object_id, window_end: view.card.window_end,
      day_index: view.day_index, days_total: view.days_total, risk_level: view.card.risk_level,
    })),
    (released.data?.items ?? []).map((view) => ({ id: view.card.id, object_id: view.card.object_id, released_at: view.released_at })),
  ), [list.data, open.data, released.data]);

  const urlObject = params.get('object');
  // Объект по умолчанию выбирается, когда известны и тревоги (схема), и сроки (открытые карточки).
  const ready = Boolean(list.data) && !open.loading;
  const fallback = ready ? defaultObject(entries) : null;
  const objectId = urlObject ?? fallback;
  const selection = urlObject ? selectionFromParam(params.get('sel')) : null;
  const groupsRaw = params.get('groups');

  useEffect(() => {
    if (!urlObject && fallback) setParams(schemeParams(fallback, null, groupsFromParam(groupsRaw)), { replace: true });
  }, [urlObject, fallback, setParams, groupsRaw]);

  const scheme = useAsync((signal) => getScheme(objectId as string, signal), [objectId, generation], Boolean(objectId));
  const shownScheme = scheme.data && scheme.data.object_id === objectId ? scheme.data : null;
  const feeders = useMemo(() => shownScheme?.feeders ?? [], [shownScheme]);

  const select = useCallback((next: MnemoSelection | null) => {
    if (!objectId) return;
    const groups = withSelectionGroup(groupsFromParam(groupsRaw), selectionGroup(next, feeders));
    setParams(schemeParams(objectId, next, groups), { replace: true });
  }, [objectId, setParams, groupsRaw, feeders]);
  const setGroups = useCallback((groups: LineKind[] | null) => {
    if (!objectId) return;
    // Скрыли группу выбранной линии — выбор снимается, иначе группа тут же вернулась бы.
    const group = selectionGroup(selection, feeders);
    const keep = groups === null || group === null || groups.includes(group);
    setParams(schemeParams(objectId, keep ? selection : null, groups), { replace: true });
  }, [objectId, selection, feeders, setParams]);
  const chooseObject = useCallback(
    (next: string) => setParams(schemeParams(next, null, groupsFromParam(groupsRaw)), { replace: true }),
    [setParams, groupsRaw],
  );
  // Выбор из URL (тревога, линия) показывает свою группу, даже если она скрыта фильтром.
  const visibleGroups = withSelectionGroup(groupsFromParam(groupsRaw), selectionGroup(selection, feeders));
  useEscape(useCallback(() => select(null), [select]), selection !== null);

  const openCard = useMemo(
    () => (open.data?.items ?? []).find((view) => view.card.object_id === objectId) ?? null,
    [open.data, objectId],
  );
  const inList = !objectId || !list.data || list.data.items.some((item) => item.object_id === objectId);
  const backTo = `${location.pathname}${location.search}`;

  return (
    <div className="scheme-page">
      {list.data ? (
        <ObjectPicker summaries={list.data.items} entries={entries} selectedId={objectId}
          onSelect={chooseObject} />
      ) : (
        <aside className="panel object-picker">
          {list.loading ? <StateMessage compact kind="loading" title="Загрузка списка объектов…" /> : null}
          {list.error ? (
            <StateMessage compact kind="error" title="Список объектов недоступен" error={list.error} onRetry={list.reload}>
              Демонстрационный список вместо ответа сервера не показывается.
            </StateMessage>
          ) : null}
        </aside>
      )}

      <div className="scheme-page__main">
        {list.error && list.data ? (
          <StateMessage compact kind="stale" title="Список объектов не обновился" error={list.error} onRetry={list.reload}>
            Показано состояние на {fmtShortDateTime(list.data.as_of)}.
          </StateMessage>
        ) : null}
        {open.error ? (
          <StateMessage compact kind="error" title="Открытые прогнозы не загружены — штриховка прогноза и сроки могут отсутствовать"
            error={open.error} onRetry={open.reload} />
        ) : null}
        {list.data && !list.data.items.length ? <StateMessage kind="empty" title="Объектов со схемой нет" /> : null}
        {!inList ? (
          <StateMessage compact kind="empty" title={`Объекта «${objectName(objectId)}» нет в текущем списке схем`} />
        ) : null}

        {objectId && !shownScheme && (scheme.loading || scheme.refreshing) ? <StateMessage kind="loading" title="Загрузка схемы объекта…" /> : null}
        {objectId && scheme.error && !shownScheme && !scheme.refreshing ? (
          <StateMessage kind="error" title="Схема объекта недоступна" error={scheme.error} onRetry={scheme.reload}>
            Демонстрационная схема вместо ответа сервера не показывается.
          </StateMessage>
        ) : null}
        {shownScheme && scheme.error ? (
          <StateMessage compact kind="stale" title="Схема объекта не обновилась" error={scheme.error} onRetry={scheme.reload}>
            Показано состояние на {fmtShortDateTime(shownScheme.as_of)}.
          </StateMessage>
        ) : null}
        {shownScheme ? (
          <ObjectMnemo
            scheme={shownScheme}
            objectName={shownScheme.object_name ?? objectName(shownScheme.object_id)}
            openCard={openCard}
            selection={selection}
            onSelect={select}
            visibleGroups={visibleGroups}
            onGroupsChange={setGroups}
            backTo={backTo}
          />
        ) : null}
      </div>
    </div>
  );
}
