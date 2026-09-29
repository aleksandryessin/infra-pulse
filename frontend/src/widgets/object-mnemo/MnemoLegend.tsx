import { useId, type ReactNode } from 'react';
import { FEEDER_KIND_LABELS, LANDMARK_KIND_HINTS, LINE_GROUP_HINTS, LINE_GROUP_LEGEND } from '../../shared/config/labels';
import { SCHEME_CONVENTION } from '../../shared/config/wording';
import { AlarmMark } from '../../shared/ui/Marks';
import { LINE_KINDS } from './mnemo-layout';
import { LINE_ICONS } from './line-icons';

function Swatch({ children, width = 30 }: { children: ReactNode; width?: number }) {
  return (
    <svg className="mnemo-legend__swatch" width={width} height={18} viewBox={`0 0 ${width} 18`} aria-hidden>{children}</svg>
  );
}

/** Условные обозначения в три колонки, по умолчанию свёрнуты: типы линий, положение и связи, состояния. */
export function MnemoLegend() {
  const hatch = `legend-hatch-${useId().replace(/:/g, '')}`;
  return (
    <details className="mnemo-legend">
      <summary>Условные обозначения</summary>
      <div className="mnemo-legend__grid">
        <ul>
          {LINE_KINDS.map((kind) => {
            const IconKind = LINE_ICONS[kind];
            return (
              <li key={kind} title={LINE_GROUP_HINTS[kind]}>
                <IconKind size={16} stroke={1.6} aria-hidden className="mnemo-legend__icon" />
                <span>{FEEDER_KIND_LABELS[kind]} <span className="muted">· {LINE_GROUP_LEGEND[kind]}</span></span>
              </li>
            );
          })}
        </ul>
        <ul>
          <li title={[LANDMARK_KIND_HINTS.ats, LANDMARK_KIND_HINTS.panel, LANDMARK_KIND_HINTS.other].join('. ')}>
            <Swatch width={22}>
              <path className="mnemo-cube__top" d="M4 5 L8 2 L20 2 L16 5 Z" />
              <path className="mnemo-cube__side" d="M16 5 L20 2 L20 14 L16 17 Z" />
              <rect className="mnemo-cube__front" x="4" y="5" width="12" height="12" />
            </Swatch>
            ввод, АВР (автоматический ввод резерва), щит — узел на своём ПК, без линий к группам
          </li>
          <li>
            <Swatch><rect className="mnemo-bar" x="1" y="6" width="28" height="6" rx="2" /></Swatch>
            отрезок — линия вдоль участка «ПК a–b», указанного в названии канала
          </li>
          <li>
            <Swatch width={34}>
              <g className="mnemo-link">
                <line x1="1" x2="14" y1="9" y2="9" />
                <rect x="14.5" y="2.5" width="18" height="13" rx="2" />
              </g>
            </Swatch>
            в скобках — что питает линия (например, вентилятор В23; подтверждено заказчиком)
          </li>
          <li>
            <Swatch width={30}>
              <circle className="mnemo-node mnemo-node--stack" cx="13" cy="7" r="6" />
              <circle className="mnemo-node" cx="9" cy="11" r="6" />
            </Swatch>
            «N линий · ПК a–b» — линии рядом, число — сумма потерь связи линий
          </li>
          <li>
            <Swatch><rect className="mnemo-tunnel__tail" x="2" y="3" width="26" height="12" /></Swatch>
            длина объекта неизвестна; «Без пикета» — в названии канала нет ПК
          </li>
          <li>
            <Swatch width={30}><text className="mnemo-legend__pk" x="1" y="13">ПК</text></Swatch>
            Условная схема: {SCHEME_CONVENTION.lead}
          </li>
          <li>
            <Swatch width={24}>
              <rect className="mnemo-badge" x="1" y="1" width="22" height="16" rx="4" />
              <text className="mnemo-badge__text" x="12" y="13" textAnchor="middle">13</text>
            </Swatch>
            потерь связи линии за 365 сут; нет числа — потерь связи не было
          </li>
        </ul>
        <ul>
          <li>
            <Swatch>
              <defs>
                <pattern id={hatch} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                  <rect className="mnemo-hatch__bg" width="6" height="6" />
                  <line className="mnemo-hatch__line" x1="0" y1="0" x2="0" y2="6" />
                </pattern>
              </defs>
              <rect className="mnemo-tunnel__front--forecast" x="1.5" y="2.5" width="27" height="13" fill={`url(#${hatch})`} />
            </Swatch>
            прогноз обесточивания объекта (штриховка трассы)
          </li>
          <li><span className="mnemo-legend__mark"><AlarmMark size={14} /></span> тревожные сообщения СМВУ за сутки, «!N» — число сообщений (отказ не подтверждён)</li>
          <li>
            <Swatch width={22}><rect className="mnemo-legend__past" x="3" y="1.5" width="15" height="15" rx="1" /></Swatch>
            серый «!» — после сообщения линия вернулась в «Норма»
          </li>
          <li>
            <Swatch><rect className="mnemo-tunnel__front--released" x="1.5" y="2.5" width="27" height="13" /></Swatch>
            карточка снята по событию за 7 сут (пунктирный контур)
          </li>
          <li>
            <Swatch width={22}><circle className="mnemo-ring" cx="11" cy="9" r="6.5" /></Swatch>
            выбранный элемент
          </li>
        </ul>
      </div>
    </details>
  );
}
