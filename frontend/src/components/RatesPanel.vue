<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import type { CounterBlock, Rate } from "../api/client";
import { dashboard } from "../lib/dashboard";
import { describeGap, formatRate, formatTokens, formatUtc } from "../lib/format";
import { filledCells } from "../lib/hudGeometry";
import HudPanel from "./HudPanel.vue";
import SegmentBar from "./SegmentBar.vue";

const state = dashboard.state;

const REQUESTS_NOTE =
  "Request-accounted usage counts tokens recorded per request and can include cache-served tokens; " +
  "it is not added to engine totals.";

const rates = computed(() => state.summary?.rates ?? null);
const lifetime = computed(() => state.summary?.ranges.lifetime ?? null);
const stale = computed(() => state.summary?.state === "stale");

type RateKey = "computed_input" | "generated_output";

/**
 * Meter scale: the session peak of the selected series. It restarts on reload and on
 * series change, so a meter is a relative reading, never an absolute promise.
 */
const peaks = reactive<Record<RateKey, number | null>>({
  computed_input: null,
  generated_output: null,
});

watch(
  rates,
  (value) => {
    if (!value) return;
    for (const key of ["computed_input", "generated_output"] as RateKey[]) {
      const rate: Rate | undefined = value[key];
      const perSecond = rate?.tokens_per_second;
      if (rate?.eligible && perSecond != null) {
        const peak = peaks[key];
        peaks[key] = peak === null ? perSecond : Math.max(peak, perSecond);
      }
    }
  },
  { immediate: true },
);

watch(
  () => state.selectedSeriesId,
  () => {
    peaks.computed_input = null;
    peaks.generated_output = null;
  },
);

function meter(key: RateKey): number {
  const rate = rates.value?.[key];
  const peak = peaks[key];
  const perSecond = rate?.tokens_per_second;
  if (!rate?.eligible || perSecond == null || !peak) return 0;
  return filledCells(perSecond / peak, 24);
}

const REQUEST_KEYS = ["request_input", "request_output"] as const;
type RequestKey = (typeof REQUEST_KEYS)[number];

function requestBlock(key: RequestKey): CounterBlock | null {
  return lifetime.value ? lifetime.value.counters[key] : null;
}

function requestGap(key: RequestKey): string {
  const gaps = lifetime.value?.gap_seconds;
  return gaps ? describeGap(gaps[key]) : "";
}
</script>

<template>
  <HudPanel
    index="05"
    title="Latest rate"
    testid="rates-panel"
    class="pnl-rate"
    aria-label="Latest accepted rate"
  >
    <template #meta>
      <span class="meta">over the last accepted interval</span>
      <p v-if="stale" class="meta stale-notice" data-testid="stale-notice" role="status">
        Rates are unavailable while the series is stale
      </p>
    </template>

    <div class="rate-grid">
      <div class="well rate">
        <span class="lab tone-input">Computed input</span>
        <div class="top">
          <span
            class="v num"
            data-testid="rate-input-value"
            :class="{ 'is-null': !rates?.computed_input?.eligible }"
          >
            {{ formatRate(rates?.computed_input) }}
          </span>
          <span class="u">tok/s</span>
        </div>
        <SegmentBar variant="meter" :cells="24" :filled="meter('computed_input')" />
        <div class="sub">
          over the last accepted interval ·
          <b>{{ formatUtc(rates?.computed_input?.observed_at) }}</b>
        </div>
      </div>

      <div class="well rate">
        <span class="lab tone-output">Generated output</span>
        <div class="top">
          <span
            class="v num"
            data-testid="rate-output-value"
            :class="{ 'is-null': !rates?.generated_output?.eligible }"
          >
            {{ formatRate(rates?.generated_output) }}
          </span>
          <span class="u">tok/s</span>
        </div>
        <SegmentBar variant="meter" :cells="24" :filled="meter('generated_output')" />
        <div class="sub">
          over the last accepted interval ·
          <b>{{ formatUtc(rates?.generated_output?.observed_at) }}</b>
        </div>
      </div>
    </div>

    <details class="rate-req" data-testid="requests-disclosure">
      <summary>Request-accounted usage</summary>
      <p class="note" data-testid="requests-summary">{{ REQUESTS_NOTE }}</p>
      <div class="cells">
        <div v-for="key in REQUEST_KEYS" :key="key">
          <span class="lab">{{ key === "request_input" ? "Request input" : "Request output" }}</span>
          <p v-if="requestBlock(key)?.tokens == null" class="note" data-testid="unavailable-note">
            unavailable
          </p>
          <template v-else>
            <div class="num">{{ formatTokens(requestBlock(key)?.tokens ?? null) }}</div>
            <div class="sub">
              {{ requestBlock(key)?.observed_intervals }} observed intervals · {{ requestGap(key) }}
            </div>
          </template>
        </div>
      </div>
    </details>
  </HudPanel>
</template>
