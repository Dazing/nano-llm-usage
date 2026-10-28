import type { Bucket, HistoryResponse } from "../api/client";

/**
 * The year activity heatmap model (docs/activity-heatmap-implementation-plan.md
 * D1-D3): the window, the grid geometry and the colour scale, as pure functions.
 *
 * No DOM and no Vue here — the panel renders whatever this returns, so the scale
 * and the window shape are testable (and reviewable) on their own.
 */

const DAY_MS = 86_400_000;

/** Fixed trailing-year grid: 52 columns of 7 days (plan D1: 53 columns needs 371 days,
 * above the API's 366-day daily cap). */
export const WEEK_COLUMNS = 52;
export const WEEKDAY_ROWS = 7;
const FULL_GRID_DAYS = WEEK_COLUMNS * WEEKDAY_ROWS;

/** Cell geometry of the SVG grid (plan D5): 14 px cells, 3 px gaps, weekday gutter, month row. */
export const CELL_PX = 14;
export const GAP_PX = 3;
export const GUTTER_PX = 28;
export const MONTH_ROW_PX = 14;
export const GRID_WIDTH_PX =
  GUTTER_PX + WEEK_COLUMNS * CELL_PX + (WEEK_COLUMNS - 1) * GAP_PX;
export const GRID_HEIGHT_PX =
  MONTH_ROW_PX + WEEKDAY_ROWS * CELL_PX + (WEEKDAY_ROWS - 1) * GAP_PX;

/** Row 0 is Sunday; the panel labels Mon, Wed and Fri (plan D5, matching GitHub). */
export const WEEKDAY_LABELS = ["", "Mon", "", "Wed", "", "Fri", ""];

const MONTH_NAMES = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/** Ratio precision: six decimals, so a 3-decimal display never rounds through `Number`. */
const RATIO_SCALE = 1_000_000n;
/** The floor of the fill range, so the smallest positive day is not a zero day (plan D3). */
export const FILL_FLOOR_PERCENT = 20;
const FILL_SPAN_PERCENT = 100 - FILL_FLOOR_PERCENT;

export interface ActivityWindow {
  /** Inclusive start of the first Sunday, `…T00:00:00.000Z`. */
  from: string;
  /** Exclusive end: midnight after today, `…T00:00:00.000Z`. */
  to: string;
  /** `YYYY-MM-DD` of the first column's first cell. */
  start: string;
  /** `YYYY-MM-DD` of today (UTC). */
  today: string;
  /** 358-364: the cells the grid holds, i.e. `358 + getUTCDay(today)`. */
  days: number;
}

export type HeatmapCellState = "none" | "zero" | "value";

export interface HeatmapCell {
  /** `YYYY-MM-DD`, UTC. */
  day: string;
  col: number;
  /** 0 = Sunday … 6 = Saturday. */
  row: number;
  /** `none` = unobserved (null), `zero` = a real 0, `value` = positive. */
  state: HeatmapCellState;
  /** A bucket exists for this day, so its quality flags are facts, not absence. */
  observed: boolean;
  /** The exact `processed_total` lexeme; null when the day is unobserved. */
  value: string | null;
  /** Linear share of the window maximum (0 for `none` and `zero`). */
  ratio: number;
  /** Fill strength in percent, `20 + 80·√ratio` (0 for `none` and `zero`). */
  pct: number;
  /** Today, `partial` or `incomplete`: drawn with the quality outline. */
  partial: boolean;
  flags: string[];
  /** Null when the day is unobserved; the bucket's `metadata_complete` otherwise. */
  metadataComplete: boolean | null;
  computedInput: string | null;
  generatedOutput: string | null;
}

export interface MonthLabel {
  col: number;
  label: string;
}

export interface HeatmapModel {
  cells: HeatmapCell[];
  months: MonthLabel[];
  /** The largest non-null day in the window, today included; null when none is positive. */
  max: { value: string; day: string } | null;
  /** Cells the API reports as unobserved — the hollow, dashed ones. */
  noDataDays: number;
}

const iso = (value: number): string => new Date(value).toISOString();
const dayLabel = (value: number): string => iso(value).slice(0, 10);

/** The trailing-year window: a whole number of weeks ending with the current UTC week. */
export function activityWindow(now: Date): ActivityWindow {
  const midnight = Math.floor(now.getTime() / DAY_MS) * DAY_MS;
  const dow = new Date(midnight).getUTCDay();
  // 357 + dow: back to the Sunday that opens the 52nd column before today's, so the
  // window is 358-364 days — inside MAX_BUCKETS["day"] = 366 — and today is the last cell.
  const startMs = midnight - (FULL_GRID_DAYS - WEEKDAY_ROWS + dow) * DAY_MS;
  return {
    from: iso(startMs),
    to: iso(midnight + DAY_MS),
    start: dayLabel(startMs),
    today: dayLabel(midnight),
    days: FULL_GRID_DAYS - WEEKDAY_ROWS + 1 + dow,
  };
}

/** The wire total is an exact decimal string; a lexeme this API never produces is unobserved. */
function parseTokens(value: string | null | undefined): bigint | null {
  if (value === null || value === undefined) return null;
  try {
    return BigInt(value);
  } catch {
    return null;
  }
}

/** The linear share of the maximum, fixed at six decimals, never rounded through `Number`. */
function shareOf(tokens: bigint, max: bigint): number {
  return Number((tokens * RATIO_SCALE) / max) / Number(RATIO_SCALE);
}

/**
 * One square-root step of the fill range: heavy-tailed token counts keep a visible
 * ramp next to a burst day, and the floor keeps a small day distinct from a zero (D3).
 */
function fillPercent(ratio: number): number {
  return FILL_FLOOR_PERCENT + FILL_SPAN_PERCENT * Math.sqrt(ratio);
}

/**
 * Folds the daily buckets into the 52x7 grid.
 *
 * A window day the payload does not carry is `none`, and buckets outside the window are
 * ignored, so the model is a function of the window and not of the response's shape. The
 * maximum is taken over the non-null days inside the window, so one pass finds the scale
 * and the second applies it.
 */
export function buildHeatmap(history: HistoryResponse | null, window: ActivityWindow): HeatmapModel {
  const byDay = new Map<string, Bucket>();
  for (const bucket of history?.buckets ?? []) {
    const day = bucket.bucket.slice(0, 10);
    if (!byDay.has(day)) byDay.set(day, bucket);
  }

  const startMs = Date.parse(window.from);
  const cells: HeatmapCell[] = [];
  const months: MonthLabel[] = [];
  const cellsPerMonthGap = 2; // a 3-letter label spans two columns plus their gaps
  let lastLabelCol = -Infinity;
  let max: { value: string; day: string } | null = null;
  let maxTokens = 0n;
  let noDataDays = 0;

  for (let index = 0; index < window.days; index += 1) {
    const day = dayLabel(startMs + index * DAY_MS);
    const bucket = byDay.get(day);
    const tokens = parseTokens(bucket?.processed_total);
    const state: HeatmapCellState =
      tokens === null ? "none" : tokens > 0n ? "value" : "zero";
    const col = Math.floor(index / WEEKDAY_ROWS);
    if (state === "none") noDataDays += 1;
    if (tokens !== null && maxTokens < tokens) {
      // A real 0 never becomes the maximum: maxTokens starts at 0n.
      maxTokens = tokens;
      max = { value: bucket?.processed_total ?? "0", day };
    }
    if (day.endsWith("-01") && col - lastLabelCol >= cellsPerMonthGap) {
      months.push({ col, label: MONTH_NAMES[Number(day.slice(5, 7)) - 1] });
      lastLabelCol = col;
    }
    cells.push({
      day,
      col,
      row: index % WEEKDAY_ROWS,
      state,
      observed: bucket !== undefined,
      value: state === "none" ? null : (bucket?.processed_total ?? null),
      ratio: 0,
      pct: 0,
      partial: bucket === undefined
        ? day === window.today
        : bucket.partial || bucket.incomplete || day === window.today,
      flags: bucket?.quality_flags ?? [],
      metadataComplete: bucket ? bucket.metadata_complete : null,
      computedInput: bucket?.counters.computed_input.tokens ?? null,
      generatedOutput: bucket?.counters.generated_output.tokens ?? null,
    });
  }

  if (max) {
    for (const cell of cells) {
      if (cell.state !== "value") continue;
      cell.ratio = shareOf(BigInt(cell.value ?? "0"), maxTokens);
      cell.pct = fillPercent(cell.ratio);
    }
  }

  return { cells, months, max, noDataDays };
}
