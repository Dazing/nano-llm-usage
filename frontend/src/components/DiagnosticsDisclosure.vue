<script setup lang="ts">
import { computed } from "vue";
import { dashboard } from "../lib/dashboard";
import { describeGap, flagLabel, formatTokens, formatUtc, NO_DATA } from "../lib/format";
import HudPanel from "./HudPanel.vue";

const state = dashboard.state;

const EPOCH_REASON: Record<string, string> = {
  new_series: "New series",
  endpoint_change: "Endpoint change",
  counter_reset: "Counter reset",
  restart_evidence: "Restart evidence",
  clock_regression: "Clock regression",
};

const GAPS: {
  key: "primary" | "request_input" | "request_output" | "cached_input";
  label: string;
}[] = [
  { key: "primary", label: "Engine counters" },
  { key: "request_input", label: "Request input" },
  { key: "request_output", label: "Request output" },
  { key: "cached_input", label: "Cached input" },
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
const errorCode = computed(
  () => state.status?.last_error_code ?? state.status?.last_recorded_error_code ?? null,
);

const metaLine = computed(
  () =>
    `${events.value.length} events · ${epochs.value.length} epochs · ` +
    `retention ${state.status?.raw_retention_days ?? NO_DATA} d · ${flagLines.value.length} flags`,
);
</script>

<template>
  <HudPanel
    index="06"
    title="Diagnostics"
    testid="diagnostics-panel"
    class="pnl-diag"
    aria-label="Collection diagnostics"
  >
    <template #meta>
      <span class="meta">{{ metaLine }}</span>
    </template>

    <details class="diag-dtl" data-testid="diagnostics-disclosure">
      <summary>Collection diagnostics — gaps, epochs, quality flags, retention</summary>

      <div class="diag-body">
        <section data-testid="diagnostics-gaps">
          <h4>Unobserved intervals</h4>
          <ul v-if="lifetime">
            <li v-for="gap in GAPS" :key="gap.key">
              <span class="k">{{ gap.label }}:</span> {{ describeGap(lifetime.gap_seconds[gap.key]) }}
            </li>
          </ul>
          <ul v-if="events.length">
            <li v-for="event in events" :key="event.event_id">
              {{ event.kind }} · {{ formatUtc(event.occurred_at) }}
              <template v-if="event.details"> · {{ JSON.stringify(event.details) }}</template>
            </li>
          </ul>
          <p v-else-if="!events.length">No collection events recorded.</p>
        </section>

        <section data-testid="diagnostics-epochs">
          <h4>Epochs</h4>
          <p v-if="epochs.length === 0">No epoch boundaries recorded.</p>
          <ul v-else>
            <li v-for="epoch in epochs" :key="epoch.epoch_id">
              {{ EPOCH_REASON[epoch.reason] ?? epoch.reason }} · {{ formatUtc(epoch.started_at) }}
              <span class="k">
                · Opening balance — excluded from totals:
                {{ formatTokens(epoch.opening_computed_input) }} in,
                {{ formatTokens(epoch.opening_generated_output) }} out
              </span>
            </li>
          </ul>
        </section>

        <section data-testid="diagnostics-flags">
          <h4>Quality flags</h4>
          <p v-if="flagLines.length === 0">No quality flags on the current payload.</p>
          <ul v-else>
            <li v-for="line in flagLines" :key="line.flag">{{ line.text }}</li>
          </ul>
        </section>

        <section data-testid="diagnostics-retention">
          <h4>Raw retention</h4>
          <p v-if="state.status">Raw samples are kept for {{ state.status.raw_retention_days }} days.</p>
          <p v-if="retention">
            Raw samples available {{ formatUtc(retention.raw_samples_from) }} –
            {{ formatUtc(retention.raw_samples_until) }}
          </p>
          <p v-if="retention?.note">{{ retention.note }}</p>
        </section>

        <section data-testid="diagnostics-error">
          <h4>Last error</h4>
          <p v-if="errorCode">
            Last collection error: {{ errorCode }}. The collector retries on its own; the values shown
            are from the last successful sample.
          </p>
          <p v-else>No collection error recorded.</p>
        </section>
      </div>
    </details>
  </HudPanel>
</template>
