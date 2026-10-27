<script setup lang="ts">
import { computed } from "vue";
import { live } from "../lib/live";
import { formatPerRequest, formatRatio, formatTps, formatUtc, joinSlots, LIVE_PLACEHOLDER } from "../lib/format";
import LabelValue from "./LabelValue.vue";

const snapshot = computed(() => live.state.snapshot);

const chip = computed<{ label: string; tone: string }>(() => {
  const state = snapshot.value?.state;
  if (live.state.unavailable || state === "stale" || state === "unavailable") {
    return { label: "Stale", tone: "is-warn" };
  }
  if (state === "live") return { label: "Live", tone: "is-ok" };
  return { label: "Starting", tone: "" };
});

/** A count slot keeps its own value; only a missing one becomes a placeholder. */
function countSlot(value: number | null | undefined): string {
  return value === null || value === undefined ? LIVE_PLACEHOLDER : String(value);
}

const requests = computed(() =>
  joinSlots([countSlot(snapshot.value?.requests.active), countSlot(snapshot.value?.requests.queued)]),
);
const prefill = computed(() => formatTps(snapshot.value?.prefill_tokens_per_second));
const decode = computed(
  () =>
    `${formatTps(snapshot.value?.decode_tokens_per_second)} · ` +
    formatPerRequest(snapshot.value?.decode_tokens_per_second_per_request),
);
const mtp = computed(() => {
  const window = snapshot.value?.mtp_acceptance;
  return joinSlots([
    formatRatio(window?.window_5s),
    formatRatio(window?.window_1m),
    formatRatio(window?.window_30m),
    formatRatio(window?.lifetime),
  ]);
});
const moe = computed(() => {
  const window = snapshot.value?.moe_gpu_hit;
  return joinSlots([
    formatRatio(window?.window_5s),
    formatRatio(window?.window_1m),
    formatRatio(window?.window_30m),
  ]);
});
const observedAt = computed(() => formatUtc(snapshot.value?.observed_at));
</script>

<template>
  <section class="panel live-panel" data-testid="live-panel" aria-label="Live engine activity">
    <h2>
      Live
      <span class="badge" :class="chip.tone" data-testid="live-state">{{ chip.label }}</span>
      <span class="live-observed">{{ observedAt }}</span>
    </h2>
    <div class="live-grid">
      <div class="live-cell">
        <LabelValue label="Requests" testid="live-requests">{{ requests }}</LabelValue>
        <p class="live-slots">running / waiting</p>
      </div>
      <div class="live-cell">
        <LabelValue label="Prefill" testid="live-prefill">{{ prefill }}</LabelValue>
        <p class="live-slots">instantaneous</p>
      </div>
      <div class="live-cell">
        <LabelValue label="Decode" testid="live-decode">{{ decode }}</LabelValue>
        <p class="live-slots">instantaneous · per running request</p>
      </div>
      <div class="live-cell">
        <LabelValue label="MTP accept" testid="live-mtp">{{ mtp }}</LabelValue>
        <p class="live-slots">5s · 1m · 30m · lifetime</p>
      </div>
      <div class="live-cell">
        <LabelValue label="MoE GPU hit" testid="live-moe">{{ moe }}</LabelValue>
        <p class="live-slots">5s · 1m · 30m</p>
      </div>
    </div>
  </section>
</template>
