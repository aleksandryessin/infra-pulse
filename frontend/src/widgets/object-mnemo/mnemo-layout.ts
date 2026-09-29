/**
 * Геометрия мнемосхемы одного объекта (F2, плотные объекты — F3). Чистые функции без React
 * и без import.meta: проверяются `npm test` (tests/mnemo-layout.test.mjs).
 *
 * Условность схемы: по горизонтали — пикет из названия канала (1 ПК = 10 м), по вертикали —
 * только группа линий. Длина объекта неизвестна: трасса сплошная до последнего известного
 * ПК, дальше пунктир. Линий «ввод / АВР → линия» нет — подключение в данных не записано.
 * Связь рисуется только по `named_link` из API; названия каналов здесь не разбираются.
 *
 * Три области с общими координатами по вертикали: слева подписи групп (не прокручиваются),
 * в центре трасса по ПК (прокручивается, не меньше MIN_PX_PER_PK на пикет; «Вписать» —
 * вся трасса в ширину), справа колонка «Без пикета» (не прокручивается). В группе не больше
 * MAX_LANES поддорожек: при плотности подписи скрываются (видны при наведении и фокусе),
 * линии ближе CLUSTER_PX сливаются в знак «N линий · ПК a–b» с суммой потерь связи.
 * Фильтр групп (P1-9): скрытая группа — строка HIDDEN_ROW_H с числом линий и тревог; её
 * тревоги не теряются, шкала трассы от фильтра не зависит.
 */

export type LineKind = 'lighting' | 'ventilation' | 'pumps' | 'ozk' | 'other';
export const LINE_KINDS: readonly LineKind[] = ['lighting', 'ventilation', 'pumps', 'ozk', 'other'];

export interface PicketInput {
  form: 'point' | 'range' | 'unknown';
  picket_from?: number | null;
  picket_to?: number | null;
}

export interface MnemoLineInput {
  id: string;
  name: string;
  /** `feeder_kind` из API; неизвестный код попадает в «прочее». */
  kind: string;
  picket: PicketInput;
  episodes: number;
  /** `named_link.label`; null — связи в названии канала нет, отрезок не рисуется. */
  link: string | null;
  /**
   * Тревожные сообщения СМВУ за сутки: id записи, готовая подпись для подсказки («„Обесточен“
   * 24.09, 08:10») и `active` — после записи не было «Нормы» (B-3). Новые — первыми.
   */
  alarms: { id: string; text: string; active: boolean }[];
  /** Тревожных сообщений линии за сутки без обрезки списка (B-3); нет — длина `alarms`. */
  alarmRecords?: number;
}

export interface MnemoLandmarkInput {
  index: number;
  kind: string;
  /** Короткая подпись над узлом: вид («Ввод», «АВР»), не разобранное название. */
  label: string;
  picket: PicketInput;
}

export interface MnemoInput {
  /** Ширина всей области схемы в px (измеряется у контейнера). */
  width: number;
  lines: MnemoLineInput[];
  landmarks: MnemoLandmarkInput[];
  /** «Вписать»: вся трасса в ширину экрана, плотность ниже MIN_PX_PER_PK допускается. */
  fit?: boolean;
  /** Скрытые фильтром группы линий; тревоги скрытой группы остаются в её строке. */
  hidden?: readonly LineKind[];
}

/** Размеры знаков — общие для раскладки и отрисовки. */
export const M = {
  r: 12,
  badgeH: 18,
  linkSeg: 14,
  linkBoxH: 17,
  barH: 8,
  tunnelH: 40,
  tunnelDx: 16,
  tunnelDy: 12,
  cubeW: 24,
  cubeH: 26,
  cubeDx: 8,
  cubeDy: 6,
  alarm: 16,
  tailW: 56,
  laneGap: 12,
} as const;

/** Минимум пикселей на пикет без «Вписать»; дальше — горизонтальная прокрутка. */
export const MIN_PX_PER_PK = 7;
/** Линии, чьи значки ближе этого расстояния, сливаются в один знак. */
export const CLUSTER_PX = 24;
/** Поддорожек в группе не больше этого числа. */
export const MAX_LANES = 2;
/** Высота строки скрытой группы. */
export const HIDDEN_ROW_H = 24;

export interface AxisTick { x: number; pk: number; major: boolean; label: string | null }

export interface MnemoTunnel {
  x1: number;
  x2: number;
  /** Верх передней грани. */
  y: number;
  h: number;
  /** Пунктирное продолжение за последним известным ПК. */
  tail: { x1: number; x2: number };
}

export interface MnemoLandmark {
  index: number;
  kind: string;
  label: string;
  /** Истинное положение ПК; `cx` может быть сдвинут, чтобы узлы не слипались. */
  x: number;
  cx: number;
  /** Низ передней грани узла (стоит на верхней грани трассы). */
  bottom: number;
}

/**
 * Знак тревожных сообщений на трассе (F6, правило 3): только квадрат «!», при нескольких
 * записях — «!N». Текст — в подсказке, фокусе и полосе сведений. `active` — хотя бы одна
 * запись без последующей «Нормы»; иначе знак серый (линия уже вернулась в норму).
 */
export interface MnemoAlarm { id: string; x: number; y: number; w: number; count: number; active: boolean; text: string }

export interface MnemoLabel { x: number; y: number; text: string; muted: boolean }

export interface MnemoItem {
  /** `line` — одна линия; `cluster` — несколько линий группы рядом (или без ПК), один знак. */
  type: 'line' | 'cluster';
  /** channel_id линии; у знака — id первой линии. */
  id: string;
  ids: string[];
  name: string;
  kind: LineKind;
  /** В колонке «ПК ?»: пикета в названии канала нет, место не угадывается. */
  unknown: boolean;
  /** Диапазон ПК знака (для «N линий · ПК a–b»); null — в колонке «ПК ?». */
  range: { from: number; to: number } | null;
  cx: number;
  cy: number;
  bar: { x1: number; x2: number } | null;
  link: { x1: number; x2: number; boxX: number; boxW: number; label: string } | null;
  badge: { x: number; w: number; text: string };
  /** Подпись под значком; `hidden` — при плотности, видна при наведении и фокусе. */
  label: MnemoLabel;
  labelHidden: boolean;
  alarm: MnemoAlarm | null;
  /** Занятый по горизонтали отрезок (для раскладки по дорожкам). */
  extent: { left: number; right: number };
  lane: number;
}

export interface MnemoRow {
  kind: LineKind;
  y: number;
  h: number;
  /** Базовая линия подписи группы. */
  labelY: number;
  /** Знаки на трассе (координаты трассы). */
  items: MnemoItem[];
  /** Знаки колонки «ПК ?» (координаты колонки). */
  columnItems: MnemoItem[];
  /** Подпись пустой группы на трассе. */
  empty: { x: number; y: number } | null;
  /** Подписи скрыты из-за плотности. */
  dense: boolean;
  lanes: number;
  /** Сводка тревожных сообщений группы за сутки (F6, правило 2); null — записей нет. */
  alarmSummary: { records: number; lines: number; active: number } | null;
  /** Группа скрыта фильтром: линий в группе, тревог и первая тревога (для выбора). */
  hidden: { lines: number; alarms: number; alarmId: string | null } | null;
}

export interface MnemoLayout {
  height: number;
  compact: boolean;
  fit: boolean;
  /** Левая неподвижная область: значки и названия групп. */
  left: { width: number };
  /** Трасса: ширина содержимого, видимая ширина и пикселей на ПК. */
  track: { width: number; viewport: number; pxPerPk: number; scrolls: boolean };
  /** Колонка «ПК ?» справа, неподвижная. */
  column: { width: number; y: number; h: number; count: number };
  /** Диапазон шкалы, ПК; null — ни одного известного пикета. */
  domain: { min: number; max: number } | null;
  /** Первый и последний известный ПК объекта. */
  known: { min: number; max: number } | null;
  tunnel: MnemoTunnel | null;
  landmarks: MnemoLandmark[];
  axis: { y: number; x1: number; x2: number; ticks: AxisTick[] };
  rowsHeaderY: number;
  rows: MnemoRow[];
  rowsBottom: number;
}

const MINOR_STEPS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
const MAJOR_STEPS = [5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000];

/** Оценка ширины строки: средний символ Inter ≈ 0,57 кегля (кириллица шире латиницы). */
export function textWidth(text: string, size = 11.5, factor = 0.57): number {
  return Math.ceil(text.length * size * factor);
}

/** Обрезает подпись до ширины с «…»; полное название — в aria-label и полосе сведений. */
export function fitText(text: string, maxWidth: number, size = 11.5): string {
  if (textWidth(text, size) <= maxWidth) return text;
  const chars = Math.max(1, Math.floor(maxWidth / (size * 0.57)) - 1);
  return `${text.slice(0, chars).trimEnd()}…`;
}

export function fmtPk(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toLocaleString('ru-RU', { maximumFractionDigits: 1 });
}

export function pkRangeText(from: number, to: number): string {
  return from === to ? `ПК ${fmtPk(from)}` : `ПК ${fmtPk(from)}–${fmtPk(to)}`;
}

export function linesWord(count: number): string {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return 'линия';
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'линии';
  return 'линий';
}

/** Известный пикет: точка или диапазон; `unknown` и пустое значение — null. */
export function picketSpan(picket: PicketInput): { from: number; to: number } | null {
  if (picket.form === 'unknown' || picket.picket_from === null || picket.picket_from === undefined) return null;
  const to = picket.form === 'range' && picket.picket_to !== null && picket.picket_to !== undefined
    ? picket.picket_to : picket.picket_from;
  return { from: picket.picket_from, to: Math.max(to, picket.picket_from) };
}

export function lineKind(kind: string): LineKind {
  return (LINE_KINDS as readonly string[]).includes(kind) ? kind as LineKind : 'other';
}

/** Засечки: мелкий шаг ≥ 12 px, подписи с шагом ≥ 80 px, кратным мелкому. */
export function axisTicks(min: number, max: number, pxPerPk: number): { pk: number; major: boolean }[] {
  const minor = MINOR_STEPS.find((step) => step * pxPerPk >= 12) ?? MINOR_STEPS[MINOR_STEPS.length - 1];
  const major = MAJOR_STEPS.find((step) => step >= minor && step % minor === 0 && step * pxPerPk >= 80)
    ?? MAJOR_STEPS[MAJOR_STEPS.length - 1];
  const ticks: { pk: number; major: boolean }[] = [];
  const start = Math.ceil(min / minor - 1e-9) * minor;
  for (let value = start; value <= max + 1e-9 && ticks.length < 4000; value += minor) {
    const pk = Math.round(value * 1000) / 1000;
    ticks.push({ pk, major: Math.abs(pk / major - Math.round(pk / major)) < 1e-9 });
  }
  return ticks;
}

/**
 * Жадная раскладка по дорожкам (сортировка по левому краю). Возвращает номера дорожек;
 * при `maxLanes` — индекс первого элемента, которому места нет (иначе -1).
 */
export function assignLanes(
  extents: { left: number; right: number }[],
  gap: number = M.laneGap,
  maxLanes = Infinity,
): { lanes: number[]; overflow: number } {
  const order = extents.map((extent, index) => ({ extent, index })).sort((a, b) => a.extent.left - b.extent.left);
  const laneRight: number[] = [];
  const lanes = new Array<number>(extents.length).fill(0);
  for (const { extent, index } of order) {
    let lane = laneRight.findIndex((right) => right + gap <= extent.left);
    if (lane < 0) {
      if (laneRight.length >= maxLanes) return { lanes, overflow: index };
      lane = laneRight.length;
      laneRight.push(extent.right);
    } else {
      laneRight[lane] = extent.right;
    }
    lanes[index] = lane;
  }
  return { lanes, overflow: -1 };
}

/** Соседние линии группы: значки ближе `px` — один узел. Вход отсортирован по ПК. */
export function clusterByDistance<T>(entries: { x: number; value: T }[], px: number = CLUSTER_PX): T[][] {
  const groups: T[][] = [];
  let lastX = -Infinity;
  for (const entry of entries) {
    if (groups.length && entry.x - lastX < px) groups[groups.length - 1].push(entry.value);
    else groups.push([entry.value]);
    lastX = entry.x;
  }
  return groups;
}

const ALARM_PRE = 14;
/** Место под сводку тревог группы в левой колонке (две строки под названием группы). */
const SUMMARY_H = 30;
const LANE_LABELED = 55;
const LANE_BARE = 42;
const CY_OFFSET = 24;

interface Node { lines: MnemoLineInput[]; from: number; to: number }

/** Тревожных сообщений линии за сутки: полное число с сервера или длина списка. */
export function lineAlarmRecords(line: { alarms: readonly unknown[]; alarmRecords?: number }): number {
  return Math.max(line.alarmRecords ?? 0, line.alarms.length);
}

function alarmOf(lines: MnemoLineInput[], cx: number): MnemoAlarm | null {
  const all = lines.flatMap((line) => line.alarms);
  if (!all.length) return null;
  const count = lines.reduce((sum, line) => sum + lineAlarmRecords(line), 0);
  const active = all.some((alarm) => alarm.active);
  // Число сообщений — в начале подсказки, чтобы обрезка его не съедала.
  const text = count > 1 ? `сообщений: ${count} · последнее ${all[0].text}` : all[0].text;
  const w = count > 1 ? M.alarm + String(count).length * 7 : M.alarm;
  return { id: all[0].id, x: cx - M.alarm / 2, y: 0, w, count, active, text };
}

/** Знак линии или узла линий относительно центра значка (cy — после раскладки). */
function buildItem(
  node: Node,
  kind: LineKind,
  cx: number,
  barTo: number | null,
  unknown: boolean,
  labels: boolean,
  maxLabel: number,
): MnemoItem {
  const single = node.lines.length === 1;
  const line = node.lines[0];
  let anchor = barTo !== null ? Math.max(cx + M.r, barTo) : cx + M.r;
  let link: MnemoItem['link'] = null;
  if (single && line.link) {
    const label = fitText(line.link, unknown ? 50 : 110, 11);
    const boxW = textWidth(label, 11, 0.6) + 12;
    link = { x1: anchor, x2: anchor + M.linkSeg, boxX: anchor + M.linkSeg, boxW, label };
    anchor += M.linkSeg + boxW;
  }
  const episodes = node.lines.reduce((sum, item) => sum + item.episodes, 0);
  // F6, правило 4: нули не рисуются — нет числа значит «потерь связи за 365 сут не было».
  const badgeText = episodes > 0 ? String(episodes) : '';
  const badge = { x: anchor + 6, w: badgeText ? 10 + badgeText.length * 7 : 0, text: badgeText };
  const range = unknown ? null : { from: node.from, to: node.to };
  const text = single ? line.name
    : `${node.lines.length} ${linesWord(node.lines.length)}${range ? ` · ${pkRangeText(range.from, range.to)}` : ''}`;
  const label: MnemoLabel = { x: cx - 14, y: 0, text: fitText(text, maxLabel), muted: false };
  const alarm = alarmOf(node.lines, cx);
  const right = Math.max(
    badge.w ? badge.x + badge.w : anchor,
    labels ? label.x + textWidth(label.text) : 0,
    alarm ? alarm.x + alarm.w : 0,
  );
  const left = Math.min(cx - M.r, labels ? label.x : Infinity, alarm ? alarm.x : Infinity);
  return {
    type: single ? 'line' : 'cluster',
    id: line.id,
    ids: node.lines.map((item) => item.id),
    name: single ? line.name : text,
    kind,
    unknown,
    range,
    cx,
    cy: 0,
    bar: single && barTo !== null && barTo > cx ? { x1: cx, x2: barTo } : null,
    link,
    badge,
    label,
    labelHidden: !labels,
    alarm,
    extent: { left, right },
    lane: 0,
  };
}

function placeVertically(item: MnemoItem, laneTop: number, laneHasAlarm: boolean): void {
  const cy = laneTop + (laneHasAlarm ? ALARM_PRE : 0) + CY_OFFSET;
  item.cy = cy;
  item.label.y = cy + 25;
  if (item.alarm) item.alarm.y = cy - M.r - 22;
}

/** Раскладка группы на трассе: подписи → без подписей → слияние, пока хватает MAX_LANES. */
function layoutGroup(
  lines: MnemoLineInput[],
  kind: LineKind,
  x: (pk: number) => number,
  maxLabel: number,
): { items: MnemoItem[]; lanes: number; dense: boolean } {
  const sorted = lines
    .map((line) => ({ line, span: picketSpan(line.picket) as { from: number; to: number } }))
    .sort((a, b) => a.span.from - b.span.from || a.span.to - b.span.to);
  let nodes: Node[] = clusterByDistance(sorted.map((entry) => ({ x: x(entry.span.from), value: entry })))
    .map((group) => ({
      lines: group.map((entry) => entry.line),
      from: Math.min(...group.map((entry) => entry.span.from)),
      to: Math.max(...group.map((entry) => entry.span.to)),
    }));
  const build = (labels: boolean) => nodes.map((node) => buildItem(
    node, kind, x(node.from), node.lines.length === 1 && node.to > node.from ? x(node.to) : null, false, labels, maxLabel,
  ));
  for (const labels of [true, false]) {
    const items = build(labels);
    const placed = assignLanes(items.map((item) => item.extent), M.laneGap, MAX_LANES);
    if (placed.overflow < 0) {
      items.forEach((item, index) => { item.lane = placed.lanes[index]; });
      return { items, lanes: items.length ? Math.max(...placed.lanes) + 1 : 0, dense: !labels };
    }
  }
  // Даже без подписей не помещается: сливаем мешающий знак с предыдущим, пока не поместится.
  for (let guard = 0; guard < 10_000; guard += 1) {
    const items = build(false);
    const placed = assignLanes(items.map((item) => item.extent), M.laneGap, MAX_LANES);
    if (placed.overflow < 0) {
      items.forEach((item, index) => { item.lane = placed.lanes[index]; });
      return { items, lanes: items.length ? Math.max(...placed.lanes) + 1 : 0, dense: true };
    }
    const at = Math.max(1, placed.overflow);
    const merged: Node = {
      lines: [...nodes[at - 1].lines, ...nodes[at].lines],
      from: Math.min(nodes[at - 1].from, nodes[at].from),
      to: Math.max(nodes[at - 1].to, nodes[at].to),
    };
    nodes = [...nodes.slice(0, at - 1), merged, ...nodes.slice(at + 1)];
  }
  return { items: [], lanes: 0, dense: true };
}

/**
 * Раскладка мнемосхемы: трасса по ПК с узлами вводов и АВР, шкала, пять полос групп
 * линий и колонка «ПК ?». Каждый знак — на своём пикете; пересечения разводятся не
 * больше чем по MAX_LANES дорожкам, положение по горизонтали не меняется.
 */
export function layoutMnemo(input: MnemoInput): MnemoLayout {
  const width = Math.max(560, Math.floor(input.width));
  const compact = width < 900;
  const fit = input.fit === true;
  // Колонка подписей: «электропитания» полужирным 13 px ≈ 110 px (Inter), при 104 px обрезалось
  // на 1366 (M1 аудита 29.09); «Трасса по пикетам» в узком виде — в две строки.
  const leftW = compact ? 120 : 124;
  const columnW = compact ? 132 : 156;
  const gap = 12;
  const viewport = width - leftW - columnW - gap * 2;
  const padLeft = 18;
  const padRight = M.tunnelDx + M.tailW + 24;

  const known: number[] = [];
  for (const line of input.lines) {
    const span = picketSpan(line.picket);
    if (span) known.push(span.from, span.to);
  }
  for (const landmark of input.landmarks) {
    const span = picketSpan(landmark.picket);
    if (span) known.push(span.from, span.to);
  }

  let domain: MnemoLayout['domain'] = null;
  let knownRange: MnemoLayout['known'] = null;
  let pxPerPk = 0;
  let trackWidth = viewport;
  if (known.length) {
    const lo = Math.min(...known);
    const hi = Math.max(...known);
    knownRange = { min: lo, max: hi };
    const spread = Math.max(hi - lo, 6);
    const pad = Math.max(1, spread * 0.03);
    const min = Math.max(0, lo - pad - (hi - lo < 6 ? (6 - (hi - lo)) / 2 : 0));
    domain = { min, max: Math.max(hi + pad, min + spread + pad) };
    const span = domain.max - domain.min;
    const fitted = (viewport - padLeft - padRight) / span;
    pxPerPk = fit ? fitted : Math.max(MIN_PX_PER_PK, fitted);
    trackWidth = Math.max(viewport, Math.ceil(padLeft + span * pxPerPk + padRight));
  }
  const x = (pk: number): number => (domain ? padLeft + (pk - domain.min) * pxPerPk : padLeft);

  // --- трасса и узлы вводов / АВР (без линий к группам) ---
  const landmarkInputs = input.landmarks
    .map((landmark) => ({ landmark, span: picketSpan(landmark.picket) }))
    .filter((entry): entry is { landmark: MnemoLandmarkInput; span: { from: number; to: number } } => entry.span !== null)
    .sort((a, b) => a.span.from - b.span.from);
  const tunnelY = landmarkInputs.length ? 64 : 40;
  const landmarks: MnemoLandmark[] = [];
  let prevRight = -Infinity;
  for (const { landmark, span } of landmarkInputs) {
    const at = x(span.from);
    const half = Math.max(M.cubeW + M.cubeDx, textWidth(landmark.label, 12, 0.62)) / 2;
    const cx = Math.max(at, prevRight + half + 6);
    prevRight = cx + half;
    landmarks.push({ index: landmark.index, kind: landmark.kind, label: landmark.label, x: at, cx, bottom: tunnelY - M.tunnelDy });
  }

  let tunnel: MnemoTunnel | null = null;
  if (knownRange) {
    const x1 = x(knownRange.min);
    const x2 = Math.max(x(knownRange.max), x1 + 6);
    tunnel = {
      x1,
      x2,
      y: tunnelY,
      h: M.tunnelH,
      tail: { x1: x2 + M.tunnelDx + 6, x2: x2 + M.tunnelDx + 6 + M.tailW - 12 },
    };
  }

  const axisY = tunnelY + M.tunnelH + 16;
  const ticks: AxisTick[] = domain
    ? axisTicks(domain.min, domain.max, pxPerPk)
      .map((tick) => ({ x: x(tick.pk), pk: tick.pk, major: tick.major, label: tick.major ? `ПК ${fmtPk(tick.pk)}` : null }))
    : [];

  // --- полосы групп линий ---
  const rowsHeaderY = axisY + 28;
  let y = axisY + 52;
  const rows: MnemoRow[] = [];
  let unknownCount = 0;
  const hiddenKinds = new Set(input.hidden ?? []);
  for (const kind of LINE_KINDS) {
    const lines = input.lines.filter((line) => lineKind(line.kind) === kind);
    const withAlarms = lines.filter((line) => line.alarms.length);
    const alarmSummary = withAlarms.length ? {
      records: withAlarms.reduce((sum, line) => sum + lineAlarmRecords(line), 0),
      lines: withAlarms.length,
      active: withAlarms.filter((line) => line.alarms.some((alarm) => alarm.active)).length,
    } : null;
    if (hiddenKinds.has(kind)) {
      const alarms = withAlarms.flatMap((line) => line.alarms);
      rows.push({
        kind,
        y,
        h: HIDDEN_ROW_H,
        labelY: y + 16,
        items: [],
        columnItems: [],
        empty: null,
        dense: false,
        lanes: 0,
        alarmSummary,
        hidden: { lines: lines.length, alarms: alarmSummary?.records ?? 0, alarmId: alarms[0]?.id ?? null },
      });
      y += HIDDEN_ROW_H;
      continue;
    }
    const onTrack = domain ? lines.filter((line) => picketSpan(line.picket) !== null) : [];
    const noPicket = lines.filter((line) => !onTrack.includes(line));
    unknownCount += noPicket.length;
    const group = layoutGroup(onTrack, kind, x, compact ? 160 : 220);
    const columnCx = 12 + M.r;
    const columnNodes: Node[] = noPicket.length > MAX_LANES
      ? [{ lines: noPicket, from: 0, to: 0 }]
      : noPicket.map((line) => ({ lines: [line], from: 0, to: 0 }));
    const columnItems = columnNodes.map((node, index) => {
      const item = buildItem(node, kind, columnCx, null, true, true, columnW - 16);
      item.lane = index;
      return item;
    });
    const laneCount = Math.max(1, group.lanes, columnItems.length);
    const all = [...group.items, ...columnItems];
    let laneTop = y;
    let firstCy = y + CY_OFFSET;
    for (let lane = 0; lane < laneCount; lane += 1) {
      const inLane = all.filter((item) => item.lane === lane);
      const alarm = inLane.some((item) => item.alarm);
      const labeled = inLane.some((item) => !item.labelHidden) || !inLane.length;
      for (const item of inLane) placeVertically(item, laneTop, alarm);
      if (lane === 0) firstCy = laneTop + (alarm ? ALARM_PRE : 0) + CY_OFFSET;
      laneTop += (alarm ? ALARM_PRE : 0) + (labeled ? LANE_LABELED : LANE_BARE);
    }
    // Под названием группы — две строки сводки тревог (правило 2): полоса не ниже их.
    const h = Math.max(laneTop - y, alarmSummary ? firstCy + 5 - y + SUMMARY_H : 0);
    rows.push({
      kind,
      y,
      h,
      labelY: firstCy + 5,
      items: group.items,
      columnItems,
      empty: group.items.length ? null : { x: padLeft, y: firstCy + 4 },
      dense: group.dense,
      lanes: laneCount,
      alarmSummary,
      hidden: null,
    });
    y += h;
  }
  const rowsBottom = y;

  return {
    height: rowsBottom + 8,
    compact,
    fit,
    left: { width: leftW },
    track: { width: trackWidth, viewport, pxPerPk, scrolls: trackWidth > viewport + 1 },
    column: { width: columnW, y: axisY - 12, h: rowsBottom - (axisY - 12), count: unknownCount },
    domain,
    known: knownRange,
    tunnel,
    landmarks,
    axis: { y: axisY, x1: domain ? x(domain.min) : padLeft, x2: domain ? x(domain.max) : padLeft, ticks },
    rowsHeaderY,
    rows,
    rowsBottom,
  };
}
