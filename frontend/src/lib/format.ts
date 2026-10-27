import type { Bucket, Rate } from "../api/client";

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
