import { reactive } from "vue";
import { ApiError, fetchLive, type LiveResponse } from "../api/client";

const LIVE_POLL_MS = 1_000;

/** Sparkline history depth: 40 one-second samples, the mockup's 40-point wells. */
const SAMPLE_CAP = 40;

export interface LiveState {
  /** The newest snapshot; null before the first one and after a failed poll. */
  snapshot: LiveResponse | null;
  /** Set by a failed poll: every tile becomes a placeholder (plan D6). */
  unavailable: boolean;
  /**
   * Received values per series, newest last, capped at SAMPLE_CAP (plan D6).
   * A `null` entry is a gap — a failed tick, an unanswered snapshot, or the
   * pause of a hidden tab — and the sparkline breaks there instead of joining.
   */
  samples: {
    prefill: (number | null)[];
    decode: (number | null)[];
    mtp5s: (number | null)[];
  };
  /** Running maximum of the values received since mount: the gauge full scale. */
  peaks: { prefill: number | null; decode: number | null };
}

function pushSample(series: (number | null)[], value: number | null): void {
  series.push(value);
  if (series.length > SAMPLE_CAP) series.shift();
}

function raisePeak(peak: number | null, value: number | null): number | null {
  if (value === null) return peak;
  return peak === null ? value : Math.max(peak, value);
}

/**
 * The 1 Hz live row (plan D6).
 *
 * Deliberately its own store, separate from the 15 s `dashboard` store: a
 * one-second tick must never fan out to the SQLite-backed reads, and the live
 * row has nothing to coalesce — a tick that arrives while a request is in
 * flight is skipped, not stacked. Pauses on `visibilitychange` like `dashboard`
 * does, so a background tab stops polling too. It never fabricates a value: a
 * missing field is rendered as a placeholder by the panel, not carried forward.
 */
function createLive() {
  const state = reactive<LiveState>({
    snapshot: null,
    unavailable: false,
    samples: { prefill: [], decode: [], mtp5s: [] },
    peaks: { prefill: null, decode: null },
  });

  let timer: number | null = null;
  let controller: AbortController | null = null;

  /** One entry per series for a snapshot the engine answered with. */
  function record(snapshot: LiveResponse): void {
    const prefill = snapshot.prefill_tokens_per_second ?? null;
    const decode = snapshot.decode_tokens_per_second ?? null;
    pushSample(state.samples.prefill, prefill);
    pushSample(state.samples.decode, decode);
    pushSample(state.samples.mtp5s, snapshot.mtp_acceptance.window_5s ?? null);
    state.peaks.prefill = raisePeak(state.peaks.prefill, prefill);
    state.peaks.decode = raisePeak(state.peaks.decode, decode);
  }

  /** One gap entry per series: no value is invented for a tick that never landed. */
  function recordGap(): void {
    pushSample(state.samples.prefill, null);
    pushSample(state.samples.decode, null);
    pushSample(state.samples.mtp5s, null);
  }

  async function tick(): Promise<void> {
    if (controller !== null) return;
    controller = new AbortController();
    const signal = controller.signal;
    try {
      const snapshot = await fetchLive(signal);
      state.snapshot = snapshot;
      state.unavailable = snapshot.state === "unavailable";
      record(snapshot);
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError) {
        state.snapshot = null;
        state.unavailable = true;
        recordGap();
        return;
      }
      throw error;
    } finally {
      if (controller?.signal === signal) controller = null;
    }
  }

  function start(): void {
    if (timer !== null) return;
    timer = window.setInterval(() => void tick(), LIVE_POLL_MS);
  }

  function stop(): void {
    if (timer === null) return;
    window.clearInterval(timer);
    timer = null;
  }

  function onVisibility(): void {
    if (document.hidden) {
      stop();
      return;
    }
    // The ticks that were skipped while hidden are a gap, not a flat line (D6).
    recordGap();
    start();
    void tick();
  }

  function mount(): void {
    document.addEventListener("visibilitychange", onVisibility);
    start();
    void tick();
  }

  function unmount(): void {
    document.removeEventListener("visibilitychange", onVisibility);
    stop();
    controller?.abort();
    controller = null;
  }

  return { state, tick, start, stop, mount, unmount };
}

export type LiveStore = ReturnType<typeof createLive>;

export const live = createLive();
