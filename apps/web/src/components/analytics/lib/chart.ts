/** Geometry for the single-series line chart: clean y ticks, scales and nearest-point lookup. */

/** 0 and 3–5 evenly spaced "nice" ticks (1, 2, 2.5, 5 × 10^n) covering `max`. */
export function niceTicks(max: number, target = 4): number[] {
  if (!Number.isFinite(max) || max <= 0) return [0, 1];
  const rough = max / target;
  const pow = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= rough) ?? 10 * pow;
  const ticks: number[] = [];
  for (let v = 0; v < max + step * 0.999; v += step) ticks.push(Number(v.toPrecision(12)));
  if (ticks.length < 2) ticks.push(step);
  return ticks;
}

export type Geometry = { width: number; height: number; padLeft: number; padRight: number; padTop: number; padBottom: number };

export function xAt(i: number, n: number, g: Geometry): number {
  const inner = g.width - g.padLeft - g.padRight;
  if (n <= 1) return g.padLeft + inner / 2;
  return g.padLeft + (i / (n - 1)) * inner;
}

export function yAt(v: number, top: number, g: Geometry): number {
  const inner = g.height - g.padTop - g.padBottom;
  const ratio = top > 0 ? Math.min(1, Math.max(0, v / top)) : 0;
  return g.padTop + inner * (1 - ratio);
}

export function linePath(values: number[], top: number, g: Geometry): string {
  return values.map((v, i) => `${i === 0 ? "M" : "L"}${xAt(i, values.length, g).toFixed(1)},${yAt(v, top, g).toFixed(1)}`).join(" ");
}

export function areaPath(values: number[], top: number, g: Geometry): string {
  if (values.length === 0) return "";
  const base = (g.height - g.padBottom).toFixed(1);
  const first = xAt(0, values.length, g).toFixed(1);
  const last = xAt(values.length - 1, values.length, g).toFixed(1);
  return `${linePath(values, top, g)} L${last},${base} L${first},${base} Z`;
}

/** Index of the data position nearest to pointer x (the crosshair snaps to it). */
export function nearestIndex(x: number, n: number, g: Geometry): number {
  if (n <= 1) return 0;
  const inner = g.width - g.padLeft - g.padRight;
  const ratio = (x - g.padLeft) / inner;
  return Math.min(n - 1, Math.max(0, Math.round(ratio * (n - 1))));
}

/** About `count` evenly spaced label indexes, always including the first and last. */
export function labelIndexes(n: number, count = 6): number[] {
  if (n <= 0) return [];
  if (n <= count) return Array.from({ length: n }, (_, i) => i);
  const step = (n - 1) / (count - 1);
  const out = new Set<number>();
  for (let i = 0; i < count; i++) out.add(Math.round(i * step));
  return [...out].sort((a, b) => a - b);
}
