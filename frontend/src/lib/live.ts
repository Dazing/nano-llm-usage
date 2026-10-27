import { reactive } from "vue";
import { ApiError, fetchLive, type LiveResponse } from "../api/client";

const LIVE_POLL_MS = 1_000;

export interface LiveState {
  /** The newest snapshot; null before the first one and after a failed poll. */
  snapshot: LiveResponse | null;
  /** Set by a failed poll: every tile becomes a placeholder (plan D6). */
  unavailable: boolean;
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
  const state = reactive<LiveState>({ snapshot: null, unavailable: false });

  let timer: number | null = null;
  let controller: AbortController | null = null;

  async function tick(): Promise<void> {
    if (controller !== null) return;
    controller = new AbortController();
    const signal = controller.signal;
    try {
      const snapshot = await fetchLive(signal);
      state.snapshot = snapshot;
      state.unavailable = snapshot.state === "unavailable";
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError) {
        state.snapshot = null;
        state.unavailable = true;
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
