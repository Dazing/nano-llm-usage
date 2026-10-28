// Disposable probe for the heatmap read cadence in lib/dashboard.ts (D6).
// Run: cd frontend && node probe_cadence.ts
import { ACTIVITY_REFRESH_MS, activityDue, type ActivityReadMark } from "./src/lib/dashboard.ts";

let failures = 0;
const check = (name: string, actual: unknown, expected: unknown) => {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    failures += 1;
    console.log(`FAIL ${name}\n  expected ${e}\n  actual   ${a}`);
  } else {
    console.log(`ok   ${name} = ${a}`);
  }
};

const FROM = "2025-08-17T00:00:00.000Z";
const applied = (over: Partial<ActivityReadMark> = {}): ActivityReadMark => ({
  seriesId: "s1",
  windowFrom: FROM,
  at: 1_000_000,
  ...over,
});
const wanted = (over: Record<string, unknown> = {}) => ({
  seriesId: "s1",
  windowFrom: FROM,
  nowMs: 1_000_000,
  ...over,
});

check("cadence constant", ACTIVITY_REFRESH_MS, 300_000);
check("never read is due", activityDue(wanted(), { seriesId: null, windowFrom: null, at: null }), true);
check("fresh payload is not due", activityDue(wanted(), applied()), false);
check("4m59s is not due",
  activityDue(wanted({ nowMs: 1_000_000 + ACTIVITY_REFRESH_MS - 1 }), applied()), false);
check("5m exactly is due", activityDue(wanted({ nowMs: 1_000_000 + ACTIVITY_REFRESH_MS }), applied()), true);
check("20m is due", activityDue(wanted({ nowMs: 1_000_000 + 1_200_000 }), applied()), true);
check("other series is due", activityDue(wanted({ seriesId: "s2" }), applied()), true);
check("slid window is due",
  activityDue(wanted({ windowFrom: "2025-08-24T00:00:00.000Z" }), applied()), true);
check("window slid with a fresh payload is due",
  activityDue(
    { seriesId: "s1", windowFrom: "2025-08-24T00:00:00.000Z", nowMs: 1_000_001 },
    applied(),
  ),
  true);
check("cleared mark is due",
  activityDue(wanted({ nowMs: 1_000_001 }), { seriesId: null, windowFrom: FROM, at: 1_000_000 }), true);
check("payload without a time is due",
  activityDue(wanted({ nowMs: 1_000_001 }), applied({ at: null })), true);

console.log(failures === 0 ? "PROBE PASSED" : `PROBE FAILED (${failures})`);
process.exit(failures === 0 ? 0 : 1);
