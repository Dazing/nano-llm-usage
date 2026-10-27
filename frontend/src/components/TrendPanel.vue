<script setup lang="ts">
import { computed, ref } from "vue";
import { dashboard } from "../lib/dashboard";
import { useTrendChart } from "../lib/chart";
import {
  bucketLabel,
  flagLabel,
  formatAmount,
  formatCacheShare,
  formatCoverage,
  formatCostFlags,
  formatDay,
  formatTokens,
  formatUtc,
  NO_DATA,
  rangeErrorText,
} from "../lib/format";
import HudPanel from "./HudPanel.vue";

const state = dashboard.state;
const history = computed(() => state.history);
const canvas = ref<HTMLCanvasElement | null>(null);
const chart = useTrendChart(canvas, history);

const PRESETS = [
  { id: "24h", label: "24h · hourly" },
  { id: "7d", label: "7d · daily" },
  { id: "30d", label: "30d · daily" },
] as const;

const customFrom = ref(state.customFrom);
const customTo = ref(state.customTo);

const metaLine = computed(() => {
  const historyValue = history.value;
  if (!historyValue) return "awaiting history";
  return (
    `${historyValue.bucket_count} ${historyValue.bucket} buckets · ` +
    `${formatUtc(historyValue.from)} → ${formatUtc(historyValue.to)} · ${historyValue.model_name}`
  );
});

const legendSpan = computed(() => {
  const historyValue = history.value;
  if (!historyValue) return "";
  const kind = historyValue.bucket === "hour" ? "hourly" : "daily";
  return `${kind} buckets · ${formatDay(historyValue.from)} → ${formatDay(historyValue.to)} UTC`;
});

const cost = computed(() => history.value?.range.api_equivalent ?? null);
const rates = computed(() => state.status?.cost_rates ?? null);

/** The amount is a span so the cost well can be compared against it exactly. */
const costParts = computed<string[]>(() => {
  const range = history.value?.range;
  if (!range) return [];
  const parts: string[] = [];
  if (rates.value) parts.push(`API-equivalent ${formatAmount(cost.value?.amount)}`);
  parts.push(`Cache hits ${formatCacheShare(cost.value?.cache_hit_ratio)}`);
  const coverage = formatCoverage(cost.value);
  if (coverage) parts.push(coverage);
  parts.push(...formatCostFlags(cost.value));
  return parts;
});

const costAmount = computed(() => formatAmount(cost.value?.amount));

const showCostAmount = computed(() => rates.value && costParts.value.length > 0);

const incomplete = computed(() => {
  const buckets = history.value?.buckets ?? [];
  return buckets.length > 0 && buckets[buckets.length - 1].incomplete;
});

const chartAria = computed(() => {
  const historyValue = history.value;
  const range = historyValue
    ? `${historyValue.from} to ${historyValue.to}`
    : "no history yet";
  const series = historyValue ? `${historyValue.model_name} · engine ${historyValue.engine}` : "no series";
  return (
    `Stacked bar chart of computed input and generated output tokens for ${series}, ` +
    `${state.rangePreset} range, ${range}`
  );
});

function quality(row: { quality_flags: string[]; partial: boolean; incomplete: boolean }): string {
  const lines = row.quality_flags.map(flagLabel);
  if (row.partial) lines.push("partial");
  if (row.incomplete) lines.push("incomplete");
  return lines.length > 0 ? lines.join(" · ") : "complete";
}

/** Table mode hides the canvas; on close the chart re-measures its container. */
const tableOpen = ref(false);

function onTableToggle(event: Event): void {
  tableOpen.value = (event.target as HTMLDetailsElement).open;
  if (!tableOpen.value) chart.resize();
}
</script>

<template>
  <HudPanel
    index="02"
    title="Trend"
    testid="trend-panel"
    class="pnl-trend"
    :class="{ 'tbl-on': tableOpen }"
    aria-label="Token trend"
  >
    <template #meta>
      <span class="meta">{{ metaLine }}</span>
    </template>

    <div class="trend-ctl">
      <div class="grp" role="group" aria-label="Range preset">
        <button
          v-for="preset in PRESETS"
          :key="preset.id"
          :data-testid="`range-${preset.id}`"
          :class="{ 'is-active': state.rangePreset === preset.id }"
          :aria-pressed="state.rangePreset === preset.id"
          @click="dashboard.setRangePreset(preset.id)"
        >
          {{ preset.label }}
        </button>
        <details class="custom-range" data-testid="range-custom">
          <summary>Custom · UTC</summary>
          <div class="custom-fields">
            <label for="custom-from">
              UTC start (inclusive)
              <input id="custom-from" v-model="customFrom" type="date" data-testid="custom-from" />
            </label>
            <label for="custom-to">
              UTC end (inclusive)
              <input id="custom-to" v-model="customTo" type="date" data-testid="custom-to" />
            </label>
            <button data-testid="custom-apply" @click="dashboard.applyCustomRange(customFrom, customTo)">
              Apply
            </button>
          </div>
        </details>
      </div>
      <span class="utc">UTC</span>
    </div>

    <div class="tr-notes">
      <p v-if="incomplete" class="note" data-testid="incomplete-note">
        Last bucket is incomplete (current UTC hour/day)
      </p>
      <p v-if="costParts.length > 0" class="note" data-testid="trend-cost">
        <template v-if="showCostAmount">
          API-equivalent <span class="tone-cost" data-testid="trend-cost-amount">{{ costAmount }}</span>
        </template>
        <template v-else>{{ costParts[0] }}</template>
        <template v-for="part in costParts.slice(1)" :key="part"> · {{ part }}</template>
      </p>
      <p v-if="state.apiError" class="range-error" data-testid="range-error" role="status">
        {{ rangeErrorText(state.apiError.code) }}
      </p>
    </div>

    <div class="chart-shell">
      <canvas ref="canvas" data-testid="chart-canvas" role="img" :aria-label="chartAria"></canvas>
    </div>

    <div class="legend">
      <span><i class="tone-input"></i>Computed input</span>
      <span><i class="tone-output"></i>Generated output</span>
      <span class="span">{{ legendSpan }}</span>
    </div>

    <details class="chart-disclosure" data-testid="chart-disclosure" @toggle="onTableToggle">
      <summary>Show as table</summary>
      <div class="trend-table">
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
            <tr v-for="row in history?.buckets ?? []" :key="row.started_at">
              <th scope="row">{{ bucketLabel(row) }}</th>
              <td>{{ formatTokens(row.counters.computed_input.tokens) }}</td>
              <td>{{ formatTokens(row.counters.generated_output.tokens) }}</td>
              <td>{{ formatTokens(row.processed_total) }}</td>
              <td class="q">{{ quality(row) }}</td>
            </tr>
            <tr v-if="!history || history.buckets.length === 0">
              <td colspan="5">{{ NO_DATA }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>
  </HudPanel>
</template>
