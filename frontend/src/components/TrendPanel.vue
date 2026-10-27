<script setup lang="ts">
import { computed, ref, toRef } from "vue";
import type { Bucket } from "../api/client";
import { dashboard } from "../lib/dashboard";
import { useTrendChart } from "../lib/chart";
import {
  bucketLabel,
  flagLabel,
  formatAmount,
  formatCacheShare,
  formatCoverage,
  formatCostFlags,
  formatTokens,
  rangeErrorText,
} from "../lib/format";
import ControlRow from "./ControlRow.vue";

const state = dashboard.state;
const canvas = ref<HTMLCanvasElement | null>(null);
const historyRef = toRef(state, "history");
useTrendChart(canvas, historyRef);

const PRESETS = [
  { preset: "24h", testid: "range-24h", label: "24h" },
  { preset: "7d", testid: "range-7d", label: "7d" },
  { preset: "30d", testid: "range-30d", label: "30d" },
] as const;

const customFrom = ref(state.customFrom);
const customTo = ref(state.customTo);

const newestIncomplete = computed(() => historyRef.value?.buckets.at(-1)?.incomplete === true);
const errorText = computed(() => (state.apiError ? rangeErrorText(state.apiError.code) : ""));

/** One line for the selected range: the API prices the range, never a bucket. */
const costLine = computed(() => {
  const range = historyRef.value?.range;
  if (!range) return "";
  const cost = range.api_equivalent;
  const parts: string[] = [];
  if (state.status?.cost_rates) parts.push(`API-equivalent ${formatAmount(cost.amount)}`);
  parts.push(`Cache hits ${formatCacheShare(cost.cache_hit_ratio)}`);
  const coverage = formatCoverage(cost);
  if (coverage) parts.push(coverage);
  parts.push(...formatCostFlags(cost));
  return parts.join(" · ");
});

const chartAria = computed(() => {
  const series = state.summary?.model_name ?? "the selected series";
  const history = historyRef.value;
  const window = history ? `${history.from} to ${history.to}` : state.rangePreset;
  return `Stacked bar chart of computed input and generated output tokens for ${series}, ${state.rangePreset} range, ${window}`;
});

function quality(row: Bucket): string {
  const lines = row.quality_flags.map(flagLabel);
  if (row.partial) lines.push("partial");
  if (row.incomplete) lines.push("incomplete");
  return lines.length ? lines.join(" · ") : "complete";
}
</script>

<template>
  <section class="panel trend-panel" data-testid="trend-panel">
    <h2>Trend</h2>
    <ControlRow label="Trend range">
      <button
        v-for="item in PRESETS"
        :key="item.preset"
        type="button"
        class="chip"
        :class="{ 'is-active': state.rangePreset === item.preset }"
        :data-testid="item.testid"
        :aria-pressed="state.rangePreset === item.preset"
        @click="dashboard.setRangePreset(item.preset)"
      >
        {{ item.label }}
      </button>
      <details class="custom-range" data-testid="range-custom">
        <summary>Custom range</summary>
        <div class="custom-fields">
          <label for="custom-from">UTC start (inclusive)</label>
          <input id="custom-from" v-model="customFrom" type="date" data-testid="custom-from" />
          <label for="custom-to">UTC end (exclusive)</label>
          <input id="custom-to" v-model="customTo" type="date" data-testid="custom-to" />
          <button
            type="button"
            class="chip"
            data-testid="custom-apply"
            @click="dashboard.applyCustomRange(customFrom, customTo)"
          >
            Apply
          </button>
        </div>
      </details>
    </ControlRow>

    <p v-if="newestIncomplete" class="note" data-testid="incomplete-note">
      Last bucket is incomplete (current UTC hour/day)
    </p>

    <p v-if="costLine" class="note" data-testid="trend-cost">{{ costLine }}</p>

    <div class="chart-shell">
      <canvas ref="canvas" data-testid="chart-canvas" role="img" :aria-label="chartAria"></canvas>
    </div>

    <p v-if="errorText" class="range-error" data-testid="range-error" role="status">{{ errorText }}</p>

    <details class="chart-disclosure" data-testid="chart-disclosure">
      <summary>Show as table</summary>
      <table class="chart-table" data-testid="chart-table">
        <caption>Token totals per UTC bucket</caption>
        <thead>
          <tr>
            <th scope="col">UTC bucket</th>
            <th scope="col">Computed input</th>
            <th scope="col">Generated output</th>
            <th scope="col">Processed total</th>
            <th scope="col">Quality</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in historyRef?.buckets ?? []" :key="row.started_at">
            <th scope="row">{{ bucketLabel(row) }}</th>
            <td>{{ formatTokens(row.counters.computed_input.tokens) }}</td>
            <td>{{ formatTokens(row.counters.generated_output.tokens) }}</td>
            <td>{{ formatTokens(row.processed_total) }}</td>
            <td>{{ quality(row) }}</td>
          </tr>
          <tr v-if="!historyRef || historyRef.buckets.length === 0">
            <td colspan="5">no data</td>
          </tr>
        </tbody>
      </table>
    </details>
  </section>
</template>
