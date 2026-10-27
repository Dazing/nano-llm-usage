/**
 * HUD geometry primitives (plan D6): the arc, ring, sparkline and segment maths
 * behind the live panel's inline SVGs.
 *
 * Pure functions only — no DOM, no colour strings. Callers pass token-based CSS
 * classes so no hex colour ever appears in TypeScript, and every helper returns
 * "nothing" (an empty list, `null`) when a value was never received: the visuals
 * draw received data and never a plausible guess.
 */

/** The mockup's shared sweep: 220° of arc, open at the bottom right. */
export const ARC_START = -220;
export const ARC_END = 40;

export interface Point {
  x: number;
  y: number;
}

export interface GaugeTick {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  major: boolean;
}

export interface SparkRun {
  /** Polyline points for one unbroken run of samples. */
  points: string;
  /** Area polygon under the same run (baseline closed at `height`). */
  fill: string;
  single: boolean;
  lastX: number;
  lastY: number;
}

export interface Bands {
  ok: number;
  warn: number;
  caution: number;
}

/** MTP acceptance bands (plan D6): 70 / 50 / 35 %. */
export const MTP_BANDS: Bands = { ok: 0.7, warn: 0.5, caution: 0.35 };
/** MoE GPU hit bands (plan D6): 85 / 60 / 40 %. */
export const MOE_BANDS: Bands = { ok: 0.85, warn: 0.6, caution: 0.4 };

export function polar(centerX: number, centerY: number, radius: number, degrees: number): Point {
  const radians = (degrees * Math.PI) / 180;
  return { x: centerX + radius * Math.cos(radians), y: centerY + radius * Math.sin(radians) };
}

/** A clockwise arc between two angles, in the mockup's sweep direction. */
export function arcPath(
  centerX: number,
  centerY: number,
  radius: number,
  fromDegrees: number,
  toDegrees: number,
): string {
  const from = polar(centerX, centerY, radius, fromDegrees);
  const to = polar(centerX, centerY, radius, toDegrees);
  const large = toDegrees - fromDegrees > 180 ? 1 : 0;
  const f = (value: number) => value.toFixed(2);
  return `M${f(from.x)} ${f(from.y)}A${radius} ${radius} 0 ${large} 1 ${f(to.x)} ${f(to.y)}`;
}

function clampedFraction(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

/** Eleven dial ticks, every fifth one long and bright. */
export function gaugeTicks(centerX: number, centerY: number, radius: number): GaugeTick[] {
  const ticks: GaugeTick[] = [];
  for (let index = 0; index <= 10; index += 1) {
    const angle = ARC_START + ((ARC_END - ARC_START) * index) / 10;
    const major = index % 5 === 0;
    const inner = polar(centerX, centerY, radius + 6, angle);
    const outer = polar(centerX, centerY, radius + (major ? 12 : 9), angle);
    ticks.push({ x1: inner.x, y1: inner.y, x2: outer.x, y2: outer.y, major });
  }
  return ticks;
}

/** A 1/2/2.5/5 x 10^n full scale at or above `value`; 0 for nothing received. */
export function niceCeiling(value: number | null): number {
  if (value === null || !Number.isFinite(value) || value <= 0) return 0;
  const base = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (value <= step * base) return step * base;
  }
  return 10 * base;
}

/**
 * One run per unbroken stretch of received samples. `null` entries are gaps and
 * close a run, so a failed poll or a hidden tab breaks the line (D6, AC7)
 * instead of joining the values on either side of it.
 */
export function sparkRuns(values: (number | null)[], width: number, height: number, pad = 3): SparkRun[] {
  const received = values.filter((value): value is number => value !== null);
  if (received.length === 0) return [];
  const min = Math.min(...received);
  const span = Math.max(...received) - min || 1;
  const last = values.length - 1;
  const x = (index: number) => (last === 0 ? width / 2 : (index / last) * width);
  const y = (value: number) => height - pad - ((value - min) / span) * (height - pad * 2);

  const runs: SparkRun[] = [];
  let points: Point[] = [];
  const flush = () => {
    if (points.length === 0) return;
    const first = points[0];
    const end = points[points.length - 1];
    runs.push({
      points: points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" "),
      fill: `${first.x.toFixed(1)},${height} ${points
        .map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`)
        .join(" ")} ${end.x.toFixed(1)},${height}`,
      single: points.length === 1,
      lastX: end.x,
      lastY: end.y,
    });
    points = [];
  };
  values.forEach((value, index) => {
    if (value === null) {
      flush();
      return;
    }
    points.push({ x: x(index), y: y(value) });
  });
  flush();
  return runs;
}

/** Filled cell count of a segment meter for a fraction of its scale. */
export function filledCells(fraction: number | null, cells: number): number {
  if (fraction === null || !Number.isFinite(fraction)) return 0;
  return Math.round(clampedFraction(fraction) * cells);
}

/** Filled cell count of a request bar: one cell per request, never more than `cells`. */
export function filledRequests(value: number | null, cells: number): number {
  if (value === null || !Number.isFinite(value) || value <= 0) return 0;
  return Math.min(cells, Math.floor(value));
}

/** Tone class of a ratio against its bands; `null` is muted, never a colour. */
export function ratioTone(value: number | null, bands: Bands): string {
  if (value === null || !Number.isFinite(value)) return "tone-muted";
  if (value >= bands.ok) return "tone-ok";
  if (value >= bands.warn) return "tone-warn";
  if (value >= bands.caution) return "tone-caution";
  return "tone-bad";
}

/** Fraction of a ratio's arc, clamped; `null` draws no arc at all. */
export function ratioFraction(value: number | null): number | null {
  if (value === null || !Number.isFinite(value)) return null;
  return clampedFraction(value);
}
