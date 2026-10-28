<script setup lang="ts">
import { computed } from "vue";
import { live } from "../lib/live";
import { filledCells, filledRequests, MOE_BANDS, MTP_BANDS, ratioTone } from "../lib/hudGeometry";
import {
  formatPerRequest,
  formatRatio,
  formatTpsNumber,
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
 * The number alone: the unit is named in the label above it (`PREFILL · TPS`), so the
 * value stays a bare count and the row reads like the mockup.
 */
const prefillText = computed(() => formatTpsNumber(prefillValue.value));
const decodeText = computed(() => formatTpsNumber(decodeValue.value));
const decodePerRequest = computed(() =>
  formatPerRequest(snapshot.value?.decode_tokens_per_second_per_request),
);
const decodePerText = computed(() => `${decodePerRequest.value} · per running request`);

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
          <div class="req-bars">
            <div class="rrow bar-active">
              <span>Active</span>
              <SegmentBar :cells="7" :filled="filledRequests(active, 7)" />
              <b class="num">{{ activeText }}</b>
            </div>
            <div class="rrow bar-waiting">
              <span>Waiting</span>
              <SegmentBar :cells="7" :filled="filledRequests(queued, 7)" />
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
          </div>
        </div>
      </div>

      <div class="live-mid">
        <div class="well gauge live-cell">
          <Gauge :value="prefillValue" :peak="peaks.prefill" unit="tok/s" tone="tone-prefill" />
          <div class="g-body">
            <span class="lab">Prefill · TPS</span>
            <div
              class="g-val num tone-prefill"
              data-testid="live-prefill"
              :class="{ 'is-null': prefillText === '-' }"
            >{{ prefillText }}</div>
            <div class="spark">
              <Sparkline class="tone-prefill" :values="samples.prefill" :height="34" />
            </div>
          </div>
        </div>

        <div class="well gauge live-cell">
          <Gauge :value="decodeValue" :peak="peaks.decode" unit="tok/s" tone="tone-decode" />
          <div class="g-body">
            <span class="lab">Decode · TPS</span>
            <div
              class="g-val num tone-decode"
              data-testid="live-decode"
              :class="{ 'is-null': decodeText === '-' }"
            >{{ decodeText }}</div>
            <div class="g-unit" data-testid="live-decode-per">{{ decodePerText }}</div>
            <div class="spark">
              <Sparkline class="tone-decode" :values="samples.decode" :height="34" />
            </div>
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
      </div>
    </div>
  </HudPanel>
</template>
