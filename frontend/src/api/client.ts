import type { components } from "./schema";

export type StatusResponse = components["schemas"]["StatusResponse"];
export type SeriesListResponse = components["schemas"]["SeriesListResponse"];
export type SeriesItem = components["schemas"]["SeriesItem"];
export type SummaryResponse = components["schemas"]["SummaryResponse"];
export type HistoryResponse = components["schemas"]["HistoryResponse"];
export type EventsResponse = components["schemas"]["EventsResponse"];
export type LiveResponse = components["schemas"]["LiveResponse"];
export type LiveRequests = components["schemas"]["LiveRequests"];
export type LiveMtp = components["schemas"]["LiveMtp"];
export type LiveMoe = components["schemas"]["LiveMoe"];
export type Range = components["schemas"]["Range"];
export type Bucket = components["schemas"]["Bucket"];
export type Rate = components["schemas"]["Rate"];
export type CounterBlock = components["schemas"]["CounterBlock"];
export type GapScopes = components["schemas"]["GapScopes"];
export type Epoch = components["schemas"]["Epoch"];
export type EventItem = components["schemas"]["EventItem"];
export type Retention = components["schemas"]["Retention"];
export type ErrorResponse = components["schemas"]["ErrorResponse"];

const API = "/api/v1";

/** An API failure: ``code`` is the wire code, ``status`` the HTTP status (0 = never reached). */
export class ApiError extends Error {
  code: string;
  status: number;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

async function getJson<T>(
  path: string,
  query: Record<string, string | number | undefined>,
  signal: AbortSignal,
): Promise<T> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, String(value));
  }
  let response: Response;
  try {
    response = await fetch(`${API}${path}?${params}`, { signal, cache: "no-store" });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "network_error", "the dashboard API could not be reached");
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new ApiError(response.status, "schema_mismatch", "the API returned a body that is not JSON");
  }
  if (typeof body !== "object" || body === null || !("schema_version" in body)) {
    throw new ApiError(response.status, "schema_mismatch", "the API response is missing schema_version");
  }
  if (!response.ok) {
    const detail = (body as ErrorResponse).error;
    throw new ApiError(response.status, detail?.code ?? "unknown", detail?.message ?? "request failed");
  }
  return body as T;
}

export function fetchStatus(signal: AbortSignal): Promise<StatusResponse> {
  return getJson<StatusResponse>("/status", {}, signal);
}

export function fetchSeries(
  signal: AbortSignal,
  limit = 100,
  cursor?: string,
): Promise<SeriesListResponse> {
  return getJson<SeriesListResponse>("/series", { limit, cursor }, signal);
}

export function fetchSummary(
  seriesId: string,
  signal: AbortSignal,
  epochLimit = 20,
  epochCursor?: string,
): Promise<SummaryResponse> {
  return getJson<SummaryResponse>("/summary", { series_id: seriesId, epoch_limit: epochLimit, epoch_cursor: epochCursor }, signal);
}

export function fetchHistory(
  seriesId: string,
  from: string,
  to: string,
  bucket: "hour" | "day",
  signal: AbortSignal,
): Promise<HistoryResponse> {
  return getJson<HistoryResponse>("/history", { series_id: seriesId, from, to, bucket }, signal);
}

export function fetchEvents(
  seriesId: string,
  signal: AbortSignal,
  limit = 20,
  cursor?: string,
): Promise<EventsResponse> {
  return getJson<EventsResponse>("/events", { series_id: seriesId, limit, cursor }, signal);
}

/** The one-second live snapshot; it takes no series id and is never cached. */
export function fetchLive(signal: AbortSignal): Promise<LiveResponse> {
  return getJson<LiveResponse>("/live", {}, signal);
}
