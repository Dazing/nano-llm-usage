// Disposable probe for lib/heatmap.ts (this repo has no JS test runner; the permanent
// behavioural checks for the panel live in scripts/smoke_frontend.py).
// Run: cd frontend && node probe_heatmap.ts   (Node >=23.6 strips the types)
import { activityWindow, buildHeatmap } from "./src/lib/heatmap.ts";

let failures = 0;
function check(name, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    failures += 1;
    console.log(`FAIL ${name}\n  expected ${e}\n  actual   ${a}`);
  } else {
    console.log(`ok   ${name} = ${a}`);
  }
}

const DAY = 86_400_000;
const dayMs = (iso) => Date.parse(`${iso}T00:00:00.000Z`);
const isoDay = (ms) => new Date(ms).toISOString().slice(0, 10);
const r3 = (n) => n.toFixed(3);
const r1 = (n) => n.toFixed(1);

// --- window shape (D1): Sunday start, 358+dow cells, today last, no future cell --------
for (const [today, dow, days] of [
  ["2026-08-09", 0, 358],
  ["2026-08-12", 3, 361],
  ["2026-08-15", 6, 364],
  ["2024-02-29", 4, 362], // leap day
  ["2026-01-01", 4, 362], // year boundary
]) {
  const now = new Date(`${today}T13:37:00.000Z`);
  const w = activityWindow(now);
  const model = buildHeatmap(null, w);
  check(`${today} dow`, new Date(dayMs(today)).getUTCDay(), dow);
  check(`${today} from`, w.from, `${isoDay(dayMs(today) - (357 + dow) * DAY)}T00:00:00.000Z`);
  check(`${today} to`, w.to, `${isoDay(dayMs(today) + DAY)}T00:00:00.000Z`);
  check(`${today} days`, w.days, days);
  check(`${today} from is Sunday`, new Date(Date.parse(w.from)).getUTCDay(), 0);
  check(`${today} days <= API cap 366`, w.days <= 366, true);
  check(`${today} cells`, model.cells.length, days);
  check(`${today} cols`, Math.max(...model.cells.map((c) => c.col)), 51);
  const last = model.cells[model.cells.length - 1];
  check(`${today} today is last cell`, [last.day, last.col, last.row], [today, 51, dow]);
  check(`${today} no future cell`, model.cells.every((c) => c.day <= today), true);
  check(`${today} rows 0..6`, Math.max(...model.cells.map((c) => c.row)), 6);
  check(`${today} all null -> no data`, [model.noDataDays, model.max], [days, null]);
}

// --- states, ratios, scale (D2/D3) ------------------------------------------------------
const w = activityWindow(new Date("2026-08-12T09:00:00.000Z")); // Wednesday -> 361 cells
const start = dayMs(w.start);
const day = (offset) => isoDay(start + offset * DAY);
const counter = (tokens) => ({
  tokens,
  observed_intervals: tokens === null ? 0 : 288,
  gap_seconds: 0,
  quality_flags: [],
  metadata_complete: true,
});
const bucket = (offset, total, extra = {}) => ({
  bucket: `${day(offset)}T00:00:00+00:00`,
  started_at: `${day(offset)}T00:00:00+00:00`,
  ended_at: `${isoDay(dayMs(day(offset)) + DAY)}T00:00:00+00:00`,
  counters: {
    computed_input: counter(total === null ? null : "100"),
    generated_output: counter(total === null ? null : "7"),
    request_input: counter(total === null ? null : "100"),
    request_output: counter(total === null ? null : "7"),
    cached_input: counter("0"),
  },
  gap_seconds: { range: 0, since_last_event: 0 },
  metadata_complete: true,
  partial: false,
  incomplete: false,
  quality_flags: [],
  processed_total: total,
  ...extra,
});
const MAX = "20000000000000000"; // 2e16: above 2^53, so Number() cannot tell neighbours apart
const history = {
  schema_version: 1,
  series_id: "s1",
  bucket: "day",
  from: w.from,
  to: w.to,
  bucket_count: 12,
  generated_at: "2026-08-12T09:00:00Z",
  buckets: [
    bucket(0, MAX),                                     // first column, beaten by 205 below
    bucket(7, "5000000000000000"),                      // a quarter of the max
    bucket(14, "0"),                                    // a real zero
    bucket(21, null),                                   // null total -> unobserved
    bucket(28, "19800000000000000"),                    // 0.99 of the max
    bucket(35, "1"),                                    // smallest positive
    bucket(200, "9007199254740993"),                    // 2^53+1: exact string maths
    bucket(205, "20000000000000001"),                   // max + 1: identical as a Number
    bucket(360, "5", { partial: true }),                // today, partial
    bucket(100, "123", { incomplete: true, quality_flags: ["collector_gap"] }),
    bucket(400, "77"),                                  // after the window: ignored
    bucket(-1, "88"),                                   // before the window: ignored
    bucket(210, "9", { partial: true, quality_flags: ["partial_bucket"] }),
    bucket(240, null, { incomplete: true, quality_flags: ["collection_gap"] }),
  ],
};
const model = buildHeatmap(history, w);
const cell = (offset) => model.cells[offset];
check("cell count", model.cells.length, 361);
check("max uses exact string compare", model.max, { value: "20000000000000001", day: day(205) });
check("max day ratio/pct", [r3(cell(205).ratio), r1(cell(205).pct)], ["1.000", "100.0"]);
check("max day state", cell(205).state, "value");
// ratio is fixed at 6 decimals, so max-1 at this scale is a 1.000 share when shown at 3
check("runner-up ratio", [cell(0).ratio.toFixed(6), r3(cell(0).ratio), r1(cell(0).pct)],
  ["0.999999", "1.000", "100.0"]);
check("quarter max ratio/pct", [r3(cell(7).ratio), r1(cell(7).pct)], ["0.250", "60.0"]);
check("zero day", [cell(14).state, cell(14).value, r3(cell(14).ratio), r1(cell(14).pct)],
  ["zero", "0", "0.000", "0.0"]);
check("null total is none not zero", [cell(21).state, cell(21).value], ["none", null]);
check("absent day is unobserved", [cell(1).observed, cell(1).flags], [false, []]);
// A day the API reported with no total still carries its flags: quality is not absence.
check("hollow day keeps its flags",
  [cell(240).state, cell(240).observed, cell(240).flags, cell(240).partial],
  ["none", true, ["collection_gap"], true]);
check("missing day is none", [cell(1).state, cell(1).value], ["none", null]);
check("0.99 of max ratio/pct", [r3(cell(28).ratio), r1(cell(28).pct)], ["0.990", "99.6"]);
check("smallest positive keeps floor", [r3(cell(35).ratio), r1(cell(35).pct), cell(35).state],
  ["0.000", "20.0", "value"]);
check("above 2^53 exact lexeme kept", [cell(200).value, r3(cell(200).ratio)],
  ["9007199254740993", "0.450"]);
check("today partial", [cell(360).state, cell(360).partial, cell(360).day], ["value", true, w.today]);
check("incomplete day partial", [cell(100).partial, cell(100).flags], [true, ["collector_gap"]]);
check("partial day", [cell(210).partial, cell(210).flags], [true, ["partial_bucket"]]);
check("out of window buckets ignored",
  model.cells.some((c) => c.value === "77" || c.value === "88"), false);
check("counters carried", [cell(0).computedInput, cell(0).generatedOutput], ["100", "7"]);
check("metadata carried", [cell(0).metadataComplete, cell(1).metadataComplete], [true, null]);
check("no data days counted", model.noDataDays, 361 - 10);
check("empty payload all none", buildHeatmap(null, w).noDataDays, 361);

// out-of-order buckets map by label, not by index
const shuffled = { ...history, buckets: [...history.buckets].reverse() };
check("bucket order irrelevant", buildHeatmap(shuffled, w).max, { value: "20000000000000001", day: day(205) });

// duplicate day labels: one cell per day, first wins
const dup = buildHeatmap({ ...history, buckets: [bucket(0, "1"), bucket(0, MAX)] }, w);
check("duplicate label first wins", [dup.max?.value, dup.cells.length], ["1", 361]);

// no positive day at all -> the legend has no max
check("no positive day",
  buildHeatmap({ ...history, buckets: [bucket(3, "0"), bucket(4, null)] }, w).max, null);

// --- month labels: on the first column holding day 1, monotone, never overlapping ------
const months = model.months;
check("window start", w.start, "2025-08-17");
check("month label count", months.length, 12);
check("first month label", months[0], { col: 2, label: "Sep" });
check("last month column", months[months.length - 1].col, 49);
check("labels spaced", months.every((m, i) => i === 0 || m.col > months[i - 1].col + 1), true);
check("labels sit on a column holding day 1",
  months.every((m) => model.cells.some((c) => c.col === m.col && c.day.endsWith("-01"))), true);
// independently derived from the window: the first column of each month, 2-column gap rule
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const expected = [];
let lastCol = -Infinity;
for (let i = 0; i < w.days; i += 1) {
  const d = day(i);
  if (!d.endsWith("-01")) continue;
  const col = Math.floor(i / 7);
  if (col - lastCol < 2) continue;
  expected.push({ col, label: MONTHS[Number(d.slice(5, 7)) - 1] });
  lastCol = col;
}
check("months match independent derivation", months, expected);
check("labels in range", months.every((m) => m.col >= 0 && m.col < 52), true);
check("last label", months[months.length - 1].label, "Aug");

console.log(failures === 0 ? "PROBE PASSED" : `PROBE FAILED (${failures})`);
process.exit(failures === 0 ? 0 : 1);
