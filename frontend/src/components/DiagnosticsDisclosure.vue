<script setup lang="ts">
import { computed } from "vue";
import { dashboard } from "../lib/dashboard";
import { describeGap, flagLabel, formatTokens, formatUtc } from "../lib/format";

const state = dashboard.state;

const EPOCH_REASON: Record<string, string> = {
  new_series: "New series",
  endpoint_change: "Endpoint change",
  counter_reset: "Counter reset",
  restart_evidence: "Restart evidence",
  clock_regression: "Clock regression",
};

const GAPS: { key: "primary" | "request_input" | "request_output"; label: string }[] = [
  { key: "primary", label: "Engine counters" },
  { key: "request_input", label: "Request input" },
  { key: "request_output", label: "Request output" },
];

const lifetime = computed(() => state.summary?.ranges.lifetime ?? null);

const flagLines = computed(() => {
  const flags = new Set<string>();
  const summary = state.summary;
  if (summary) {
    for (const range of [summary.ranges.today, summary.ranges.month, summary.ranges.lifetime]) {
      range.quality_flags.forEach((flag) => flags.add(flag));
      if (!range.metadata_complete) flags.add("metadata_incomplete");
    }
    for (const rate of Object.values(summary.rates)) rate.quality_flags.forEach((flag) => flags.add(flag));
  }
  for (const bucket of state.history?.buckets ?? []) bucket.quality_flags.forEach((flag) => flags.add(flag));
  return [...flags].map((flag) => ({ flag, text: flagLabel(flag) }));
});

const epochs = computed(() => state.summary?.epochs.items ?? []);
const events = computed(() => state.events?.items ?? []);
const retention = computed(() => state.events?.retention ?? null);
const errorCode = computed(() => state.status?.last_error_code ?? state.status?.last_recorded_error_code ?? null);
</script>

<template>
  <details class="diagnostics" data-testid="diagnostics-disclosure">
    <summary>Diagnostics</summary>

    <section class="diagnostics-block" data-testid="diagnostics-gaps">
      <h3>Unobserved intervals</h3>
      <ul v-if="lifetime">
        <li v-for="gap in GAPS" :key="gap.key">
          {{ gap.label }}: {{ describeGap(lifetime.gap_seconds[gap.key]) }}
        </li>
      </ul>
      <ul v-if="events.length">
        <li v-for="event in events" :key="event.event_id">
          {{ event.kind }} · {{ formatUtc(event.occurred_at) }}
          <template v-if="event.details"> · {{ JSON.stringify(event.details) }}</template>
        </li>
      </ul>
      <p v-else-if="!events.length" class="note">No collection events recorded.</p>
    </section>

    <section class="diagnostics-block" data-testid="diagnostics-epochs">
      <h3>Epochs</h3>
      <p v-if="epochs.length === 0" class="note">No epoch boundaries recorded.</p>
      <ul v-else>
        <li v-for="epoch in epochs" :key="epoch.epoch_id">
          {{ EPOCH_REASON[epoch.reason] ?? epoch.reason }} · {{ formatUtc(epoch.started_at) }}
          <span class="note">
            · Opening balance — excluded from totals:
            {{ formatTokens(epoch.opening_computed_input) }} in,
            {{ formatTokens(epoch.opening_generated_output) }} out
          </span>
        </li>
      </ul>
    </section>

    <section class="diagnostics-block" data-testid="diagnostics-flags">
      <h3>Quality flags</h3>
      <p v-if="flagLines.length === 0" class="note">No quality flags on the current payload.</p>
      <ul v-else>
        <li v-for="line in flagLines" :key="line.flag">{{ line.text }}</li>
      </ul>
    </section>

    <section class="diagnostics-block" data-testid="diagnostics-retention">
      <h3>Raw retention</h3>
      <p v-if="state.status" class="note">Raw samples are kept for {{ state.status.raw_retention_days }} days.</p>
      <p v-if="retention" class="note">
        Raw samples available {{ formatUtc(retention.raw_samples_from) }} –
        {{ formatUtc(retention.raw_samples_until) }}
      </p>
      <p v-if="retention?.note" class="note">{{ retention.note }}</p>
    </section>

    <section class="diagnostics-block" data-testid="diagnostics-error">
      <h3>Last error</h3>
      <p v-if="errorCode" class="note">
        Last collection error: {{ errorCode }}. The collector retries on its own; the values shown are
        from the last successful sample.
      </p>
      <p v-else class="note">No collection error recorded.</p>
    </section>
  </details>
</template>
