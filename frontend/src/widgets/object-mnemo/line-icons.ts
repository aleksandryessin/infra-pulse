import {
  IconArrowsExchange, IconBolt, IconBulb, IconDroplet, IconFlameOff, IconLayoutBoardSplit, IconPlug, IconPropeller,
  type Icon,
} from '@tabler/icons-react';
import type { LineKind } from './mnemo-layout';

/** Иконки групп линий электропитания (Tabler, один набор контурных иконок). */
export const LINE_ICONS: Record<LineKind, Icon> = {
  lighting: IconBulb,
  ventilation: IconPropeller,
  pumps: IconDroplet,
  ozk: IconFlameOff,
  other: IconPlug,
};

/** Иконки узлов на тоннеле: ввод, АВР, щит. Неизвестный вид — IconBox в месте отрисовки. */
export const LANDMARK_ICONS: Record<string, Icon> = {
  input: IconBolt,
  ats: IconArrowsExchange,
  panel: IconLayoutBoardSplit,
};
