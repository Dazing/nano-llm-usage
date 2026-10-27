<script setup lang="ts">
import { computed } from "vue";
import { live } from "../lib/live";
import { filledCells, filledRequests, MOE_BANDS, MTP_BANDS, ratioTone } from "../lib/hudGeometry";
import {
  formatPerRequest,
  formatRatio,
  formatTps,
  formatUtc,
  LIVE_PLACEHOLDER,
} from "../lib/format";
import ApiCostWell from "./ApiCostWell.vue";
import Gauge from "./Gauge.vue";
import HudPanel from "./HudPanel.vue";
import RatioRings from "./RatioRings.vue";
import SegmentBar from "./SegmentBar.vue";
import Sparkline from "./Sparkline.vue";

const snapshot = computed(() => live.state.snapshot);
const samples = live.state.samples;
const peaks = live.state.peaks;

const isLive = computed(() => snapshot.value?.state === "live" && !live.state.unavailable);
const isStale = computed(
  () =>
    live.state.unavailable ||
    snapshot.value?.state === "stale" ||
    snapshot.value?.state === "unavailable",
);

const chip = computed<{ label: string; tone: string }>(() => {
  if (isStale.value) return { label: "Stale", tone: "is-warn" };
  if (snapshot.value?.state === "live") return { label: "Live", tone: "is-ok" };
  return { label: "Starting", tone: "is-idle" };
});

/** A count slot keeps its own value; only a missing one becomes a placeholder. */
function countSlot(value: number | null | undefined): string {
  return value === null || value === undefined ? LIVE_PLACEHOLDER : String(value);
}

const active = computed(() => snapshot.value?.requests.active ?? null);
const queued = computed(() => snapshot.value?.requests.queued ?? null);
const activeText = computed(() => countSlot(active.value));
const queuedText = computed(() => countSlot(queued.value));

const prefillValue = computed(() => snapshot.value?.prefill_tokens_per_second ?? null);
const decodeValue = computed(() => snapshot.value?.decode_tokens_per_second ?? null);

/**
 * `1,240 tok/s` split so the unit renders smaller: the two parts concatenated are
 * exactly the text `formatTps` produces, placeholders included.
 */
const prefill = computed(() => splitUnit(formatTps(prefillValue.value)));
const decode = computed(() => {
  const parts = splitUnit(formatTps(decodeValue.value));
  return { ...parts, tail: ` · ${formatPerRequest(snapshot.value?.decode_tokens_per_second_per_request)}` };
});

function splitUnit(text: string): { main: string; unit: string } {
  return text.endsWith(" tok/s")
    ? { main: text.slice(0, -" tok/s".length), unit: " tok/s" }
    : { main: text, unit: "" };
}

interface RatioRow {
  key: string;
  label: string;
  value: number | null;
}

const mtpRows = computed<RatioRow[]>(() => {
  const window = snapshot.value?.mtp_acceptance;
  return [
    { key: "5s", label: "5s", value: window?.window_5s ?? null },
    { key: "1m", label: "1m", value: window?.window_1m ?? null },
    { key: "30m", label: "30m", value: window?.window_30m ?? null },
    { key: "life", label: "life", value: window?.lifetime ?? null },
  ];
});
const mtpWindows = computed(() => mtpRows.value.slice(0, 3));

const moeRows = computed<RatioRow[]>(() => {
  const window = snapshot.value?.moe_gpu_hit;
  return [
    { key: "5s", label: "5s", value: window?.window_5s ?? null },
    { key: "1m", label: "1m", value: window?.window_1m ?? null },
    { key: "30m", label: "30m", value: window?.window_30m ?? null },
  ];
});

const observedAt = computed(() => formatUtc(snapshot.value?.observed_at));

const note = computed(() => {
  if (snapshot.value === null || snapshot.value.state === "starting") {
    return "No engine sample accepted yet; every slot shows -.";
  }
  if (isStale.value) {
    return "Live state Stale — the engine has not answered within the stale window; values are frozen at the last accepted sample, unknown values show -.";
  }
  return "Prefill and decode are instantaneous; MTP acceptance is proposed tokens accepted per window, MoE GPU hit is expert residency on GPU.";
});
</script>

<template>
  <HudPanel
    index="01"
    title="Live"
    testid="live-panel"
    class="pnl-live acc-cyan"
    :class="{ 'is-paused': !isLive }"
    aria-label="Live engine activity"
  >
    <template #meta>
      <span class="meta">1 Hz · instantaneous</span>
      <span class="meta live-observed">observed {{ observedAt }}</span>
      <span class="chip live-chip" :class="chip.tone" data-testid="live-state">
        <span class="dot" aria-hidden="true"></span>{{ chip.label }}
      </span>
    </template>

    <div class="live-sweep" aria-hidden="true"></div>

    <div class="live-grid">
      <div class="live-left">
        <div class="well live-cell">
          <span class="lab">Requests</span>
          <!-- The two slots joined is the text (D4); only the waiting part is styled. -->
          <div class="req-num num" data-testid="live-requests">{{ activeText }}<span class="small"> / {{ queuedText }}</span></div>
          <div class="sub">running / waiting</div>
          <div class="req-bars">
            <div class="rrow bar-active">
              <span>Active</span>
              <SegmentBar :cells="16" :filled="filledRequests(active, 16)" />
              <b class="num">{{ activeText }}</b>
            </div>
            <div class="rrow bar-waiting">
              <span>Waiting</span>
              <SegmentBar :cells="16" :filled="filledRequests(queued, 16)" />
              <b class="num">{{ queuedText }}</b>
            </div>
          </div>
        </div>

        <div class="well live-cell">
          <span class="lab tone-mtp">MTP acceptance ratio</span>
          <div class="rings">
            <RatioRings :values="mtpWindows.map((row) => row.value)" :bands="MTP_BANDS" />
            <div class="rlist">
              <div
                v-for="row in mtpRows"
                :key="row.key"
                class="rrow"
                :class="ratioTone(row.value, MTP_BANDS)"
              >
                <span>{{ row.label }}</span>
                <SegmentBar :cells="10" :filled="filledCells(row.value, 10)" />
                <b class="num" :data-testid="`live-mtp-${row.key}`">{{ formatRatio(row.value) }}</b>
              </div>
            </div>
          </div>
          <div class="spark">
            <Sparkline class="tone-mtp" :values="samples.mtp5s" :height="34" />
            <div class="sub">MTP 5s window · last 40 samples</div>
          </div>
        </div>
      </div>

      <div class="live-mid">
        <div class="well gauge live-cell">
          <Gauge :value="prefillValue" :peak="peaks.prefill" unit="tok/s" tone="tone-prefill" />
          <div class="g-body">
            <span class="lab">Prefill</span>
            <div class="g-val num tone-prefill" data-testid="live-prefill" :class="{ 'is-null': prefill.main === '-' }">{{ prefill.main }}<span v-if="prefill.unit" class="g-unit">{{ prefill.unit }}</span></div>
            <div class="g-unit">instantaneous</div>
            <Sparkline class="tone-prefill" :values="samples.prefill" />
          </div>
        </div>

        <div class="well gauge live-cell">
          <Gauge :value="decodeValue" :peak="peaks.decode" unit="tok/s" tone="tone-decode" />
          <div class="g-body">
            <span class="lab">Decode</span>
            <div class="g-val num tone-decode" data-testid="live-decode" :class="{ 'is-null': decode.main === '-' }">{{ decode.main }}<span v-if="decode.unit" class="g-unit">{{ decode.unit }}</span><span class="g-unit">{{ decode.tail }}</span></div>
            <div class="g-unit">instantaneous · per running request</div>
            <Sparkline class="tone-decode" :values="samples.decode" />
          </div>
        </div>
      </div>

      <div class="live-right">
        <ApiCostWell />

        <div class="well live-cell">
          <span class="lab tone-moe">MoE GPU hit ratio</span>
          <div class="rings sm">
            <RatioRings :values="moeRows.map((row) => row.value)" :bands="MOE_BANDS" />
            <div class="rlist">
              <div
                v-for="row in moeRows"
                :key="row.key"
                class="rrow"
                :class="ratioTone(row.value, MOE_BANDS)"
              >
                <span>{{ row.label }}</span>
                <SegmentBar :cells="10" :filled="filledCells(row.value, 10)" />
                <b class="num" :data-testid="`live-moe-${row.key}`">{{ formatRatio(row.value) }}</b>
              </div>
            </div>
          </div>
        </div>

        <div class="well live-hint">
          <span class="note">{{ note }}</span>
        </div>
      </div>
    </div>
  </HudPanel>
</template>
