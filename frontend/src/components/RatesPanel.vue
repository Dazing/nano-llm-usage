<script setup lang="ts">
import { computed } from "vue";
import type { CounterBlock } from "../api/client";
import { dashboard } from "../lib/dashboard";
import { describeGap, formatRate, formatTokens, formatUtc } from "../lib/format";
import LabelValue from "./LabelValue.vue";

const state = dashboard.state;
const REQUESTS_NOTE =
  "Request-accounted usage counts tokens recorded per request and can include cache-served tokens; it is not added to engine totals.";

const rates = computed(() => state.summary?.rates ?? {});
const isStale = computed(() => state.summary?.state === "stale");
const lifetime = computed(() => state.summary?.ranges.lifetime ?? null);

function requestBlock(key: "request_input" | "request_output"): CounterBlock | null {
  return lifetime.value ? lifetime.value.counters[key] : null;
}

function requestGap(key: "request_input" | "request_output"): string {
  const gaps = lifetime.value?.gap_seconds;
  return gaps ? describeGap(gaps[key]) : "";
}
</script>

<template>
  <section class="panel rates-panel" data-testid="rates-panel">
    <h2>Latest rate</h2>
    <p v-if="isStale" class="stale-notice" data-testid="stale-notice" role="status">
      Rates are unavailable while the series is stale
    </p>
    <div class="rate-grid">
      <div class="rate-cell">
        <LabelValue label="Computed input" :testid="`rate-input-value`">
          {{ formatRate(rates.computed_input) }}
        </LabelValue>
        <p class="rate-interval">
          over the last accepted interval ·
          {{ rates.computed_input ? formatUtc(rates.computed_input.observed_at) : "no data" }}
        </p>
      </div>
      <div class="rate-cell">
        <LabelValue label="Generated output" :testid="`rate-output-value`">
          {{ formatRate(rates.generated_output) }}
        </LabelValue>
        <p class="rate-interval">
          over the last accepted interval ·
          {{ rates.generated_output ? formatUtc(rates.generated_output.observed_at) : "no data" }}
        </p>
      </div>
    </div>

    <details class="requests-disclosure" data-testid="requests-disclosure">
      <summary>Request-accounted usage</summary>
      <div class="requests-summary" data-testid="requests-summary">
        <p class="note">{{ REQUESTS_NOTE }}</p>
        <div class="request-grid">
          <div v-for="key in ['request_input', 'request_output']" :key="key" class="request-cell">
            <p class="request-title">{{ key === "request_input" ? "Request input" : "Request output" }}</p>
            <p v-if="requestBlock(key)?.tokens == null" class="note" data-testid="unavailable-note">
              unavailable
            </p>
            <template v-else>
              <LabelValue label="Tokens">{{ formatTokens(requestBlock(key)!.tokens) }}</LabelValue>
              <p class="request-meta">
                {{ requestBlock(key)?.observed_intervals }} observed intervals · {{ requestGap(key) }}
              </p>
            </template>
          </div>
        </div>
      </div>
    </details>
  </section>
</template>
