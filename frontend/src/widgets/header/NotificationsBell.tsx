import { useState } from 'react';
import { Button, Popover } from 'antd';
import { IconBell, IconChevronDown, IconChevronRight } from '@tabler/icons-react';
import { Link } from 'react-router-dom';
import { describeApiError } from '../../api/http';
import { useForecastState } from '../../providers/ForecastStateProvider';
import { useNotifications, type CriticalItem } from '../../providers/NotificationsProvider';
import { ROUTES } from '../../shared/config/routes';
import { BELL_MEMBERS_LIMIT, recordsText, repeatSpan, schemeObject } from '../../shared/lib/bell';
import {
  BELL_CAPTION, BELL_EMPTY, BELL_GROUP_HINT, BELL_HINT, BELL_NO_SCHEME, BELL_SERIES_HINT, BELL_TITLE,
} from '../../shared/config/wording';
import { fmtShortDateTime } from '../../shared/lib/format';

/** F-07: строка ведёт на схему объекта, а не в «Исходные сообщения» — если схема у объекта есть. */
function schemeHref(objectId: string): string {
  return `${ROUTES.scheme}?object=${encodeURIComponent(objectId)}`;
}

function seriesHint(rowKind: string | null | undefined): string | null {
  return rowKind === 'test_series' || rowKind === 'calibration_series' ? BELL_SERIES_HINT[rowKind] : null;
}

/** Объект без схемы: пометка вместо ссылки, почему — в подсказке (обкатка 29.09). */
function NoScheme() {
  return <> · <span title={BELL_NO_SCHEME.hint}>{BELL_NO_SCHEME.label}</span></>;
}

function Title({ row, target, onOpen }: { row: CriticalItem; target: string | null; onOpen: () => void }) {
  const { objectName } = useForecastState();
  const item = row.item;
  const object = item.object_name ?? objectName(item.object_id);
  const hint = seriesHint(item.row_kind);
  // Один span одной строкой: объект не уходит на отдельную строку grid-ячейки (P1-8).
  // Метка группы («Пожар», «Газ», «Затопление») — по `rule_id` на клиенте, `title` сервера не меняется.
  const title = (
    <span className="notifications__title" title={`${row.summary ?? item.title} · ${object}${hint ? `\n${hint}` : ''}`}>
      {row.group ? <span className="text-label notifications__group" title={BELL_GROUP_HINT}>{row.group}</span> : null}
      <strong>{row.summary ?? item.title}</strong> · {object}
    </span>
  );
  if (row.summary || !target) return title;
  return <Link to={schemeHref(target)} onClick={onOpen}>{title}</Link>;
}

/**
 * Строка колокольчика: одиночная запись или свёрнутая серия / повторы одного датчика за час
 * (`chatter`: «… ×N», под ним «с ЧЧ:ММ по ЧЧ:ММ») с раскрытием `members`. Ссылки на «Схему» —
 * только у объекта со схемой.
 */
function Row({ row, onOpen }: { row: CriticalItem; onOpen: () => void }) {
  const [open, setOpen] = useState(false);
  const { schemeObjects } = useForecastState();
  const item = row.item;
  const target = schemeObject(item, schemeObjects);
  const noScheme = item.object_id && !target ? <NoScheme /> : null;
  if (!row.summary) {
    return (
      <li title={item.rule_text ?? undefined}>
        <Title row={row} target={target} onOpen={onOpen} />
        <span className="muted small">{fmtShortDateTime(item.at)}{noScheme}</span>
      </li>
    );
  }
  return (
    <li>
      <button type="button" className="link-button notifications__series" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        {open ? <IconChevronDown size={14} aria-hidden /> : <IconChevronRight size={14} aria-hidden />}
        <Title row={row} target={target} onOpen={onOpen} />
      </button>
      <span className="muted small">
        {item.row_kind === 'chatter' ? repeatSpan(item) : (
          <>
            {recordsText(row.count)}{item.channels_count ? ` · каналов: ${item.channels_count}` : ''}
            {' · '}{item.first_at ? `${fmtShortDateTime(item.first_at)} — ` : ''}{fmtShortDateTime(item.at)}
          </>
        )}
        {noScheme}
      </span>
      {open ? (
        <>
          <ul className="notifications__nested">
            {row.members.map((member) => {
              // Название канала из справочника; ИД — в подсказке (QA 29.09, D3). Без названия — ИД.
              const channel = <span title={`ИД канала ${member.channel_id}`}>{member.channel_name ?? member.channel_id}</span>;
              const text = <>«{member.value_raw}» · {channel} · {fmtShortDateTime(member.event_at)}</>;
              return (
                <li key={member.row_uid}>
                  {target ? <Link to={schemeHref(target)} onClick={onOpen}>{text}</Link> : <span>{text}</span>}
                </li>
              );
            })}
          </ul>
          {row.count > BELL_MEMBERS_LIMIT ? <span className="muted small">показаны {Math.min(row.members.length, BELL_MEMBERS_LIMIT)} из {row.count}</span> : null}
        </>
      ) : null}
    </li>
  );
}

/**
 * Колокольчик ТЗ §10 — только опасные по тексту сообщения СМВУ с сервера (`/notifications`);
 * отметка «тревожное» не учитывается. Правило и подпись политики сервера — в подсказках.
 * Что отбирается и чего пока нет (охрана, температура), — в `BELL_HINT` и `BELL_CAPTION`.
 * Без пульсации и самопроизвольных всплывающих окон; новые прогнозы — счётчик у пункта меню.
 */
export function NotificationsBell() {
  const { critical, summary, error, markRead } = useNotifications();
  const [open, setOpen] = useState(false);
  // Серия — одна строка: число у колокольчика считает строки, не записи.
  const unread = critical.length;
  const policyHint = summary
    ? `${summary.policy_caption ?? `Политика ${summary.policy_version}${summary.policy_confirmed ? '' : ' (не подтверждена)'}`}`
      + ` · сообщения до ${fmtShortDateTime(summary.as_of)}`
    : undefined;
  // Все строки ответа (до 50) в прокручиваемом списке: серии не отрезаются после первых 12
  // (обкатка 29.09 — «И ещё строк: 21» прятал все три серии).
  const content = (
    <div className="notifications">
      {error ? <p className="muted small">Не удалось обновить: {describeApiError(error)}. Показаны прежние сведения.</p> : null}
      {critical.length ? (
        <ul className="notifications__list">
          {critical.map((row) => (
            <Row key={row.key} row={row} onOpen={() => { markRead([row]); setOpen(false); }} />
          ))}
        </ul>
      ) : <p className="muted">{summary ? BELL_EMPTY : 'Сведения ещё не получены.'}</p>}
      <div className="notifications__foot">
        <span className="muted small" title={policyHint}>{BELL_CAPTION}</span>
        {critical.length ? (
          <Button size="small" title="Прочитанное помнит этот браузер" onClick={() => markRead(critical)}>Прочитано</Button>
        ) : null}
      </div>
    </div>
  );
  return (
    <Popover content={content} title={<span title={BELL_HINT}>{BELL_TITLE}</span>} trigger="click" placement="bottomRight" open={open} onOpenChange={setOpen}>
      <button type="button" className="notifications-bell" aria-label={`${BELL_TITLE}: непрочитанных ${unread}`}
        title={BELL_HINT} aria-expanded={open}>
        <IconBell size={20} stroke={1.75} aria-hidden />
        {unread ? <span className="notifications-bell__count">{unread}</span> : null}
      </button>
    </Popover>
  );
}
