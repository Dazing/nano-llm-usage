<script setup lang="ts">
import { computed } from "vue";
import type { ApiEquivalent } from "../api/client";
import { dashboard } from "../lib/dashboard";
import {
  formatAmount,
  formatCostFlags,
  rangePresetLabel,
} from "../lib/format";

// The API cost well (plan D3): the same money the summary and trend panels
// derive, read straight from the shared dashboard state. No new request.
const state = dashboard.state;

const rates = computed(() => state.status?.cost_rates ?? null);

/**
 * A series with a baseline epoch and no accepted interval yet — the same rule the
 * summary cards use, so both panels say `no data` and nothing else.
 */
const baseline = computed(() => {
  const lifetime = state.summary?.ranges.lifetime;
  if (!lifetime) return false;
  const input = lifetime.counters.computed_input;
  const output = lifetime.counters.generated_output;
  return (
    input.tokens === null &&
    output.tokens === null &&
    input.observed_intervals === 0 &&
    output.observed_intervals === 0
  );
});

const today = computed<ApiEquivalent | null>(() => {
  const range = state.summary?.ranges.today;
  return baseline.value || !range ? null : range.api_equivalent ?? null;
});

const period = computed<ApiEquivalent | null>(() =>
  baseline.value ? null : state.history?.range.api_equivalent ?? null,
);

/** The cache-skew note is a warning, so it is styled as a flag line, not a caption. */
function flagLines(cost: ApiEquivalent | null): string[] {
  return cost ? formatCostFlags(cost) : [];
}

const todayFlags = computed(() => flagLines(today.value));
const periodFlags = computed(() => flagLines(period.value));

/**
 * The period the figure covers is the selected range, so the label names the
 * selection itself (the range chip) instead of repeating the range's bounds.
 */
const periodLabel = computed(() => `Selected period - ${rangePresetLabel(state.rangePreset)}`);
</script>

<template>
  <!-- AC3: with no rates configured the note is the well's only content. -->
  <div class="well cost-well" data-testid="cost-panel">
    <p v-if="!rates" class="note" data-testid="cost-unpriced">Cost rates not configured</p>

    <template v-else>
      <span class="lab tone-cost">API cost</span>

      <div class="cost-rows">
        <div class="cost-row">
          <span class="lab">Today · UTC</span>
          <div class="cost-amount num" data-testid="cost-today">{{ formatAmount(today?.amount) }}</div>
          <div v-for="line in todayFlags" :key="line" class="cost-line is-flag">{{ line }}</div>
        </div>

        <div class="hr" aria-hidden="true"></div>

        <div class="cost-row">
          <span class="lab" data-testid="cost-period-label">{{ periodLabel }}</span>
          <div class="cost-amount num" data-testid="cost-period">{{ formatAmount(period?.amount) }}</div>
          <div v-for="line in periodFlags" :key="line" class="cost-line is-flag">{{ line }}</div>
        </div>

      </div>
    </template>
  </div>
</template>
