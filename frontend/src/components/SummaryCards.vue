<script setup lang="ts">
import { computed } from "vue";
import type { ApiEquivalent, Range } from "../api/client";
import { dashboard } from "../lib/dashboard";
import {
  flagLabel,
  formatAmount,
  formatCacheShare,
  formatCoverage,
  formatCostFlags,
  formatDay,
  formatRatesFootnote,
  formatTokens,
  NO_DATA,
} from "../lib/format";
import Card from "./Card.vue";
import Donut from "./Donut.vue";
import HudPanel from "./HudPanel.vue";

const state = dashboard.state;

const CARDS = [
  { key: "today", title: "Observed today" },
  { key: "month", title: "Observed this month" },
  { key: "lifetime", title: "Observed since collection began" },
] as const;

type CardKey = (typeof CARDS)[number]["key"];

/**
 * A series with a baseline epoch and no accepted interval yet. The summary's series-level
 * `first_observation_at` is stamped by the baseline sample itself, so "nothing observed"
 * is read from the lifetime counters: no tokens and no observed intervals.
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

function range(key: CardKey): Range | null {
  return state.summary ? state.summary.ranges[key] : null;
}

function total(key: CardKey): string {
  const rangeValue = range(key);
  return baseline.value || !rangeValue ? NO_DATA : formatTokens(rangeValue.processed_total);
}

function counter(key: CardKey, counterKey: "computed_input" | "generated_output"): string {
  const rangeValue = range(key);
  const tokens = rangeValue?.counters[counterKey].tokens ?? null;
  return baseline.value ? NO_DATA : formatTokens(tokens);
}

/** Configured prices: the dollar line and the footnote are hidden without them. */
const rates = computed(() => state.status?.cost_rates ?? null);
const ratesFootnote = computed(() => formatRatesFootnote(rates.value));

function cost(key: CardKey): ApiEquivalent | null {
  const rangeValue = range(key);
  return baseline.value || !rangeValue ? null : rangeValue.api_equivalent;
}

function amount(key: CardKey): string {
  return formatAmount(cost(key)?.amount ?? null);
}

function cacheRatio(key: CardKey): number | null {
  return baseline.value ? null : range(key)?.api_equivalent.cache_hit_ratio ?? null;
}

function cacheShare(key: CardKey): string {
  return baseline.value ? NO_DATA : formatCacheShare(range(key)?.api_equivalent.cache_hit_ratio);
}

function coverage(key: CardKey): string {
  return formatCoverage(cost(key));
}

function costFlags(key: CardKey): string[] {
  return formatCostFlags(cost(key));
}

/** Plain-language quality lines; a flag is never communicated by colour alone. */
function flags(key: CardKey): string[] {
  const rangeValue = range(key);
  if (!rangeValue || baseline.value) return [];
  const lines: string[] = [];
  if (rangeValue.partial) lines.push("Partial — some intervals are unobserved");
  if (rangeValue.incomplete) lines.push("Current period is incomplete");
  if (!rangeValue.metadata_complete) lines.push(flagLabel("metadata_incomplete"));
  for (const flag of rangeValue.quality_flags) lines.push(flagLabel(flag));
  return lines;
}

/** Input/output split of the card's own totals, as bar widths. */
function split(key: CardKey): { input: string; output: string } | null {
  const rangeValue = range(key);
  if (baseline.value || !rangeValue) return null;
  const input = rangeValue.counters.computed_input.tokens;
  const output = rangeValue.counters.generated_output.tokens;
  if (input == null || output == null) return null;
  const inTokens = Number(BigInt(input));
  const outTokens = Number(BigInt(output));
  const sum = inTokens + outTokens;
  if (!Number.isFinite(sum) || sum <= 0) return null;
  return {
    input: `${((inTokens / sum) * 100).toFixed(2)}%`,
    output: `${((outTokens / sum) * 100).toFixed(2)}%`,
  };
}

const metaLine = computed(() => {
  const summary = state.summary;
  if (!summary) return "awaiting summary";
  return `${summary.model_name} · engine ${summary.engine}`;
});
</script>

<template>
  <HudPanel
    index="03"
    title="Period summary"
    testid="summary-panel"
    class="pnl-sum"
    aria-label="Period summary"
  >
    <template #meta>
      <span class="meta">{{ metaLine }}</span>
    </template>

    <div class="cards">
      <Card v-for="card in CARDS" :key="card.key" :title="card.title" :testid="`card-${card.key}`">
        <template v-if="card.key === 'lifetime'" #since>
          <span class="since" data-testid="lifetime-start">
            Collecting since {{ state.summary ? formatDay(state.summary.first_observation_at) : NO_DATA }}
          </span>
        </template>

        <div class="big num" :data-testid="`${card.key}-total`">{{ total(card.key) }}</div>

        <div class="split">
          <span v-if="split(card.key)" class="bar" aria-hidden="true">
            <i class="in-bar" :style="{ width: split(card.key)?.input }"></i>
            <i class="out-bar" :style="{ width: split(card.key)?.output }"></i>
          </span>
          <div class="keys">
            <span>Computed input</span>
            <b :data-testid="`${card.key}-input`">{{ counter(card.key, "computed_input") }}</b>
            <span>Generated output</span>
            <b :data-testid="`${card.key}-output`">{{ counter(card.key, "generated_output") }}</b>
          </div>
        </div>

        <div class="row">
          <Donut :value="cacheRatio(card.key)" />
          <div class="kv">
            <!-- Money only appears with configured prices (spec cost rule). -->
            <template v-if="rates">
              <span class="k">API-equivalent</span>
              <b class="v tone-cost" :data-testid="`${card.key}-cost`">{{ amount(card.key) }}</b>
            </template>
            <template v-else>
              <span class="k">API-equivalent</span>
              <span class="v is-muted">cost rates not configured</span>
            </template>
            <span class="k">Cache hits</span>
            <b class="v" :data-testid="`${card.key}-cache`">{{ cacheShare(card.key) }}</b>
          </div>
        </div>

        <p v-if="coverage(card.key)" class="cost-line" :data-testid="`${card.key}-cost-coverage`">
          {{ coverage(card.key) }}
        </p>
        <div v-if="costFlags(card.key).length || flags(card.key).length" class="flags">
          <p
            v-for="line in costFlags(card.key)"
            :key="`cost-${line}`"
            class="flag"
            :data-testid="`${card.key}-cost-flag`"
          >
            {{ line }}
          </p>
          <p
            v-for="line in flags(card.key)"
            :key="line"
            class="flag"
            :data-testid="`${card.key}-flag`"
          >
            {{ line }}
          </p>
        </div>
        <p v-if="card.key === 'lifetime' && baseline" class="note" data-testid="baseline-note">
          Collecting a first baseline — no observed work yet
        </p>
      </Card>
    </div>

    <p v-if="ratesFootnote" class="note sum-foot" data-testid="cost-rates-footnote">
      {{ ratesFootnote }}
    </p>
  </HudPanel>
</template>
