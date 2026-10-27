<script setup lang="ts">
import { computed } from "vue";
import type { Range } from "../api/client";
import { dashboard } from "../lib/dashboard";
import { flagLabel, formatDay, formatTokens } from "../lib/format";
import Card from "./Card.vue";
import LabelValue from "./LabelValue.vue";

const state = dashboard.state;
const NO_DATA = "no data";

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
  return input.tokens === null && output.tokens === null
    && input.observed_intervals === 0 && output.observed_intervals === 0;
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
</script>

<template>
  <div class="summary-grid">
    <Card v-for="card in CARDS" :key="card.key" :title="card.title" :testid="`card-${card.key}`">
      <LabelValue label="Processed total" :testid="`${card.key}-total`">{{ total(card.key) }}</LabelValue>
      <LabelValue label="Computed input" :testid="`${card.key}-input`" tone="muted">
        {{ counter(card.key, "computed_input") }}
      </LabelValue>
      <LabelValue label="Generated output" :testid="`${card.key}-output`" tone="muted">
        {{ counter(card.key, "generated_output") }}
      </LabelValue>
      <p
        v-if="card.key === 'lifetime'"
        class="lifetime-start"
        data-testid="lifetime-start"
      >
        Collecting since {{ state.summary ? formatDay(state.summary.first_observation_at) : NO_DATA }}
      </p>
      <p v-if="card.key === 'lifetime' && baseline" class="note" data-testid="baseline-note">
        Collecting a first baseline — no observed work yet
      </p>
      <p v-for="line in flags(card.key)" :key="line" class="flag" :data-testid="`${card.key}-flag`">
        {{ line }}
      </p>
    </Card>
  </div>
</template>
