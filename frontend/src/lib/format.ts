import type { ApiEquivalent, Bucket, CostRates, HistoryResponse, Rate } from "../api/client";

/**
 * Display helpers. Token totals are never the product of arithmetic on a JS
 * number: the wire value is an exact decimal string and stays one.
 */

export const NO_DATA = "no data";

export function formatTokens(value: string | null | undefined): string {
  if (value === null || value === undefined) return NO_DATA;
  try {
    return BigInt(value).toLocaleString("en-US");
  } catch {
    // A non-integer lexeme (never produced by this API) is shown verbatim.
    return value;
  }
}

export function formatRate(rate: Rate | undefined): string {
  if (!rate || !rate.eligible || rate.tokens_per_second === null || rate.tokens_per_second === undefined) {
    return "N/A";
  }
  return `${Math.round(rate.tokens_per_second).toLocaleString("en-US")} tok/s`;
}

/**
 * The live row renders a dash for a value the poller has not produced (plan D7).
 * It is deliberately not `N/A`: that is the wording of a rate the 15 s API
 * computed and found ineligible, which is a different fact.
 */
export const LIVE_PLACEHOLDER = "-";

/** `N tok/s` rounded to whole tokens; the live row has no sub-token precision. */
export function formatTps(value: number | null | undefined): string {
  if (value === null || value === undefined) return LIVE_PLACEHOLDER;
  return `${Math.round(value).toLocaleString("en-US")} tok/s`;
}

/** `N/req` — decode throughput attributed to one running request. */
export function formatPerRequest(value: number | null | undefined): string {
  if (value === null || value === undefined) return LIVE_PLACEHOLDER;
  return `${Math.round(value).toLocaleString("en-US")}/req`;
}

/** `70%` from a 0..1 ratio; never a delta, never a percentage of a delta. */
export function formatRatio(value: number | null | undefined): string {
  if (value === null || value === undefined) return LIVE_PLACEHOLDER;
  return `${Math.round(value * 100)}%`;
}

/** "2026-10-25 17:36 UTC" — never converted to the browser's local time. */
export function formatUtc(value: string | null | undefined): string {
  if (!value) return NO_DATA;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return NO_DATA;
  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function formatDay(value: string | null | undefined): string {
  if (!value) return NO_DATA;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return NO_DATA;
  return date.toISOString().slice(0, 10);
}

/** Hour buckets read "2026-10-25 17:00–18:00 UTC"; day buckets read "2026-10-25". */
export function bucketLabel(bucket: Bucket): string {
  const start = new Date(bucket.started_at);
  if (Number.isNaN(start.getTime())) return NO_DATA;
  const iso = start.toISOString();
  if (bucket.bucket === "day") return iso.slice(0, 10);
  const end = new Date(bucket.ended_at);
  const endLabel = Number.isNaN(end.getTime()) ? "" : `–${end.toISOString().slice(11, 16)}`;
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)}${endLabel} UTC`;
}

export const FLAG_LABELS: Record<string, string> = {
  collection_gap: "Some intervals were not collected; totals cover observed intervals only.",
  reset_boundary: "A counter reset boundary sits at the start of this range.",
  continuity_unverified: "Continuity across this interval could not be verified.",
  time_attribution_approximate: "Work is attributed to this bucket approximately, not exactly.",
  clock_discontinuity: "A clock discontinuity was observed in this range.",
  counter_unavailable: "This counter was unavailable for part of the range.",
  metadata_incomplete: "Quality metadata is incomplete for this total.",
};

export function flagLabel(flag: string): string {
  return FLAG_LABELS[flag] ?? `Unrecognised quality flag: ${flag}.`;
}

export function describeGap(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "no unobserved interval";
  return `unobserved for ${humanDuration(seconds)}`;
}

export function humanDuration(seconds: number): string {
  const total = Math.round(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  const parts: string[] = [];
  if (days) parts.push(`${days}d`);
  if (hours) parts.push(`${hours}h`);
  if (minutes) parts.push(`${minutes}m`);
  if (!days && (rest || !minutes)) parts.push(`${rest}s`);
  return parts.join(" ");
}

/** One sentence naming the action the operator can take, keyed by wire code. */
export const RANGE_ERROR_TEXT: Record<string, string> = {
  invalid_range: "Narrow the range to 366 days or fewer.",
  range_too_large: "Narrow the range to 366 days or fewer.",
  unaligned_range: "Choose range bounds aligned to the selected UTC hour or day.",
  invalid_bucket: "Choose a supported bucket: hourly or daily.",
  invalid_time: "Choose valid UTC dates for the range.",
  invalid_series_id: "Select a series from the list; the previous selection is no longer valid.",
  invalid_cursor: "The page cursor expired — reload the page.",
  series_not_found: "This series is no longer collected — pick another series.",
  storage_unavailable: "Retry — storage is unavailable.",
  read_capacity_exceeded: "Retry — too many reads are in flight.",
  schema_mismatch: "The dashboard and API versions differ — reload after redeploying.",
  network_error: "Retry — the dashboard API could not be reached.",
  unknown: "The request failed — retry.",
};

export function rangeErrorText(code: string): string {
  return RANGE_ERROR_TEXT[code] ?? "The request failed — retry.";
}

const COLLECTION_LABELS: Record<string, string> = {
  starting: "Collecting",
  healthy: "Healthy",
  degraded: "Degraded",
  offline: "Offline",
};

/** Plain-language collection status for the badge and the live region. */
export function collectionLabel(status: string | null | undefined): string {
  if (!status) return "Collecting";
  return COLLECTION_LABELS[status] ?? status;
}

// ---------------------------------------------------------------------------
// API-equivalent cost estimate (docs/cost-estimate-implementation-plan.md D6)
// ---------------------------------------------------------------------------

/** Amounts arrive as exact 6-decimal strings: display maths stays in BigInt. */
function scaledUnits(value: string, places: number): bigint | null {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) return null;
  const fraction = (match[2] ?? "").padEnd(places, "0").slice(0, places);
  return BigInt(match[1]) * 10n ** BigInt(places) + BigInt(fraction || "0");
}

function groupThousands(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** `$1,234.56`; a positive amount under a cent reads `< $0.01`, never `$0.00`. */
export function formatMoney(amount: string | null | undefined): string {
  if (amount === null || amount === undefined) return NO_DATA;
  const micros = scaledUnits(amount, 6);
  if (micros === null) return amount;
  if (micros > 0n && micros < 5_000n) return "< $0.01";
  // Six decimal places are micro-dollars: 10_000 of them make one cent.
  const cents = (micros + 5_000n) / 10_000n;
  const fraction = (cents % 100n).toString().padStart(2, "0");
  return `$${groupThousands((cents / 100n).toString())}.${fraction}`;
}

/** Operator price per M tokens with at least two decimals: `3` -> `$3.00`. */
export function formatUnitPrice(value: string): string {
  const [whole = "0", fraction = ""] = value.trim().split(".");
  const decimals = fraction.length < 2 ? fraction.padEnd(2, "0") : fraction;
  return `$${groupThousands(whole)}.${decimals}`;
}

/** `≈ $2.88`; a range with no priced hour reads `no data`, never `≈ $0.00`. */
export function formatAmount(amount: string | null | undefined): string {
  if (amount === null || amount === undefined) return NO_DATA;
  return `≈ ${formatMoney(amount)}`;
}

/** `62% of input` — the cache share over priced hours only. */
export function formatCacheShare(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined) return NO_DATA;
  return `${Math.round(ratio * 100)}% of input`;
}

/** Coverage of the priced window: `partial · covers 18.0 of 31.0 days`. */
export function formatCoverage(cost: ApiEquivalent | null | undefined): string {
  if (!cost || !cost.partial) return "";
  if (cost.expected_hours >= 48) {
    const days = (hours: number) => (hours / 24).toFixed(1);
    return `partial · covers ${days(cost.priced_hours)} of ${days(cost.expected_hours)} days`;
  }
  return `partial · covers ${cost.priced_hours} of ${cost.expected_hours} h`;
}

/** The skew caveat is text, not colour (spec D7). */
export const CACHE_SKEW_NOTE = "approximate — cache count ahead of request count";

export function formatCostFlags(cost: ApiEquivalent | null | undefined): string[] {
  if (!cost) return [];
  return cost.flags.includes("cache_exceeds_input") ? [CACHE_SKEW_NOTE] : [];
}

/** Footnote naming the prices behind every dollar figure on the page. */
export function formatRatesFootnote(rates: CostRates | null | undefined): string {
  if (!rates) return "";
  return (
    `Approximate API-equivalent at ${formatUnitPrice(rates.input_per_mtok)} input / ` +
    `${formatUnitPrice(rates.cached_input_per_mtok)} cached / ` +
    `${formatUnitPrice(rates.output_per_mtok)} output per M tokens (${rates.currency}). ` +
    "Priced only where cache tracking exists."
  );
}

const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;

/**
 * A compact scale label for an axis end: `18K`, `4.1M`, `2B`.
 *
 * Used where a full grouped number would not fit (the gauge end label), so the
 * exact value stays in the text next to the visual.
 */
export function formatCompact(value: number): string {
  if (value >= 1e9) return `${(value / 1e9).toFixed(value >= 1e10 ? 0 : 1)}B`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(value >= 1e7 ? 0 : 1)}M`;
  if (value >= 1e3) return `${Math.round(value / 1e3)}K`;
  return String(Math.round(value));
}

/**
 * The selected history period as one readable line (plan D3).
 *
 * Hour buckets show both bounds to the minute; day buckets show dates, where the
 * last day is `to - 1 day` because the wire bound is the following midnight, so
 * the inclusive reading matches what the chart contains.
 */
export function formatHistorySpan(history: HistoryResponse | null | undefined): string {
  if (!history) return NO_DATA;
  const from = Date.parse(history.from);
  const to = Date.parse(history.to);
  if (!Number.isFinite(from) || !Number.isFinite(to)) return NO_DATA;
  if (history.bucket === "hour") {
    const hours = Math.max(1, Math.round((to - from) / HOUR_MS));
    return `${hours} h · ${formatUtc(history.from)} → ${formatUtc(history.to)}`;
  }
  const days = Math.max(1, Math.round((to - from) / DAY_MS));
  const lastDay = new Date(to - DAY_MS).toISOString().slice(0, 10);
  return `${days} days · ${history.from.slice(0, 10)} → ${lastDay} UTC`;
}
