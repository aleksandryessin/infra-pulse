export interface ReceivedSnapshot {
  asOf: string;
  watermark: number;
}

/** Accept a linked observation boundary only as a complete, aware pair. */
export function linkedReceivedSnapshot(
  asOf: string | null,
  watermark: string | null,
): ReceivedSnapshot | null {
  if (!asOf || !/(?:[zZ]|[+-]\d{2}:\d{2})$/.test(asOf) || !Number.isFinite(Date.parse(asOf))) {
    return null;
  }
  if (watermark === null || !/^(0|[1-9]\d*)$/.test(watermark)) return null;
  const position = Number(watermark);
  if (!Number.isSafeInteger(position)) return null;
  return { asOf, watermark: position };
}
