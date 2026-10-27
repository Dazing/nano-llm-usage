import { reactive } from "vue";
import {
  ApiError,
  fetchEvents,
  fetchHistory,
  fetchSeries,
  fetchStatus,
  fetchSummary,
  type EventsResponse,
  type HistoryResponse,
  type SeriesItem,
  type StatusResponse,
  type SummaryResponse,
} from "../api/client";

export type RangePreset = "24h" | "7d" | "30d" | "custom";
export type BucketKind = "hour" | "day";
export interface RangeParams {
  from: string;
  to: string;
  bucket: BucketKind;
}

const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;
const POLL_MS = 15_000;
/** The API rejects a daily range wider than 366 days and an hourly one over 31 days. */
const MAX_DAY_SPAN_DAYS = 366;
const MAX_HOUR_SPAN_DAYS = 31;

export interface DashboardState {
  series: SeriesItem[];
  selectedSeriesId: string | null;
  status: StatusResponse | null;
  summary: SummaryResponse | null;
  history: HistoryResponse | null;
  events: EventsResponse | null;
  rangePreset: RangePreset;
  customFrom: string;
  customTo: string;
  loading: boolean;
  firstLoad: boolean;
  apiError: { code: string; message: string } | null;
  staleSince: string | null;
  lastSuccessAt: string | null;
  /** Collection status reported by the series page, used before any summary exists. */
  seriesCollectionStatus: string | null;
}

const iso = (value: number): string => new Date(value).toISOString();

/** The range the API must be asked for: UTC-aligned, half-open, with the API's own span limits. */
export function paramsForRange(
  preset: RangePreset,
  now: Date,
  customFrom: string,
  customTo: string,
): RangeParams | { code: string } {
  const nowMs = now.getTime();
  if (preset === "24h") {
    const hour = Math.floor(nowMs / HOUR_MS) * HOUR_MS;
    return { from: iso(hour - 23 * HOUR_MS), to: iso(hour + HOUR_MS), bucket: "hour" };
  }
  if (preset === "7d" || preset === "30d") {
    const days = preset === "7d" ? 6 : 29;
    const midnight = Math.floor(nowMs / DAY_MS) * DAY_MS;
    return { from: iso(midnight - days * DAY_MS), to: iso(midnight + DAY_MS), bucket: "day" };
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(customFrom) || !/^\d{4}-\d{2}-\d{2}$/.test(customTo)) {
    return { code: "invalid_time" };
  }
  const startMs = Date.parse(`${customFrom}T00:00:00Z`);
  const endMs = Date.parse(`${customTo}T00:00:00Z`);
  if (Number.isNaN(startMs) || Number.isNaN(endMs)) return { code: "invalid_time" };
  // The picked end date is inclusive for the operator, exclusive on the wire.
  const to = endMs + DAY_MS;
  const spanDays = (to - startMs) / DAY_MS;
  if (startMs >= to || spanDays > MAX_DAY_SPAN_DAYS) return { code: "invalid_range" };
  return { from: iso(startMs), to: iso(to), bucket: "day" };
}

/** Guards every range against the bucket's own API span limit before it is sent. */
function withinSpanLimit(params: RangeParams): boolean {
  const spanMs = Date.parse(params.to) - Date.parse(params.from);
  if (!Number.isFinite(spanMs) || spanMs <= 0) return false;
  const limit = params.bucket === "hour" ? MAX_HOUR_SPAN_DAYS : MAX_DAY_SPAN_DAYS;
  return spanMs <= limit * DAY_MS;
}

function createDashboard() {
  const state = reactive<DashboardState>({
    series: [],
    selectedSeriesId: null,
    status: null,
    summary: null,
    history: null,
    events: null,
    rangePreset: "24h",
    customFrom: "",
    customTo: "",
    loading: false,
    firstLoad: true,
    apiError: null,
    staleSince: null,
    lastSuccessAt: null,
    seriesCollectionStatus: null,
  });

  let requestSeq = 0;
  const applied: Record<"status" | "summary" | "history" | "events", number> = {
    status: 0,
    summary: 0,
    history: 0,
    events: 0,
  };
  let controller: AbortController | null = null;
  let running = false;
  let queued = false;
  let pollTimer: number | null = null;
  const cleanups = new Set<() => void>();

  function fail(error: unknown): void {
    if (error instanceof ApiError) {
      state.apiError = { code: error.code, message: error.message };
      state.staleSince = state.staleSince ?? iso(Date.now());
      return;
    }
    throw error;
  }

  function currentRange(): RangeParams | { code: string } {
    const preset = state.rangePreset;
    const params = paramsForRange(preset, new Date(), state.customFrom, state.customTo);
    if ("code" in params || !withinSpanLimit(params)) {
      const code = "code" in params ? params.code : "invalid_range";
      state.apiError = { code, message: code };
      return { code };
    }
    return params;
  }

  /** Fetches everything the screen shows; a response is applied only if it is still current. */
  async function refresh(): Promise<void> {
    if (running) {
      // One in-flight read set (D8): the server caps concurrent readers, so a burst that
      // arrives mid-flight is coalesced and re-run right after, never stacked on top.
      queued = true;
      return;
    }
    running = true;
    requestSeq += 1;
    const seq = requestSeq;
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    state.loading = true;
    try {
      const [status, series] = await Promise.all([fetchStatus(signal), fetchSeries(signal)]);
      if (signal.aborted || seq < requestSeq) return;
      if (seq >= applied.status) {
        state.status = status;
        applied.status = seq;
        state.series = series.items;
        state.seriesCollectionStatus = series.collection_status;
      }
      if (state.selectedSeriesId === null && series.items.length > 0) {
        selectSeries(series.items[0].series_id);
        return;
      }
      const seriesId = state.selectedSeriesId;
      const range = seriesId ? currentRange() : null;
      if (seriesId && range && !("code" in range)) {
        // The collector shares the reader budget with the page, so the panels are read
        // one at a time: a page load that fans out to five readers starves the collector.
        const summary = await fetchSummary(seriesId, signal);
        if (seq >= applied.summary && seriesId === state.selectedSeriesId) {
          state.summary = summary;
          applied.summary = seq;
        }
        const history = await fetchHistory(seriesId, range.from, range.to, range.bucket, signal);
        if (seq >= applied.history && seriesId === state.selectedSeriesId) {
          state.history = history;
          applied.history = seq;
        }
        const events = await fetchEvents(seriesId, signal);
        if (seq >= applied.events && seriesId === state.selectedSeriesId) {
          state.events = events;
          applied.events = seq;
        }
      }
      if (signal.aborted || seq < requestSeq) return;
      if (range && "code" in range) {
        // The range was rejected locally: currentRange() already put the code on state,
        // and a successful status/series read must not wipe the message the operator needs.
        return;
      }
      state.apiError = null;
      state.staleSince = null;
      state.lastSuccessAt = iso(Date.now());
    } catch (error) {
      if (signal.aborted) return;
      fail(error);
    } finally {
      if (!signal.aborted) {
        state.loading = false;
        state.firstLoad = false;
      }
      if (controller === null || controller.signal === signal) controller = null;
      running = false;
      if (queued) {
        queued = false;
        await refresh();
      }
    }
  }

  function selectSeries(seriesId: string): void {
    requestSeq += 1;
    controller?.abort();
    controller = null;
    state.selectedSeriesId = seriesId;
    state.summary = null;
    state.history = null;
    state.events = null;
    state.apiError = null;
    state.staleSince = null;
    void refresh();
  }

  function setRangePreset(preset: RangePreset): void {
    state.rangePreset = preset;
    void refresh();
  }

  function applyCustomRange(from: string, to: string): void {
    state.rangePreset = "custom";
    state.customFrom = from;
    state.customTo = to;
    void refresh();
  }

  function start(): void {
    if (pollTimer !== null) return;
    pollTimer = window.setInterval(() => void refresh(), POLL_MS);
  }

  function stop(): void {
    if (pollTimer === null) return;
    window.clearInterval(pollTimer);
    pollTimer = null;
  }

  function onVisibility(): void {
    if (document.hidden) {
      stop();
      return;
    }
    start();
    void refresh();
  }

  function onFocus(): void {
    if (controller === null) void refresh();
  }

  function mount(): void {
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", onFocus);
    start();
    void refresh();
  }

  function unmount(): void {
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("focus", onFocus);
    stop();
    controller?.abort();
    controller = null;
    for (const cleanup of cleanups) cleanup();
    cleanups.clear();
  }

  return { state, refresh, selectSeries, setRangePreset, applyCustomRange, start, stop, mount, unmount, cleanups };
}

export type Dashboard = ReturnType<typeof createDashboard>;

export const dashboard = createDashboard();
