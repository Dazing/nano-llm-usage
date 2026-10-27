<script setup lang="ts">
import { computed } from "vue";
import { dashboard } from "../lib/dashboard";
import { collectionLabel, formatUtc } from "../lib/format";

const state = dashboard.state;

// The polite status sentence moved here with the nominal strip (item 2): the header is
// the one live region, so a screen reader still hears the collection state change.
defineProps<{ liveLine: string }>();

const collectionStatus = computed(
  () => state.summary?.collection_status ?? state.seriesCollectionStatus ?? "starting",
);
const storageStatus = computed(() => state.summary?.storage_status ?? state.status?.storage_status ?? "ok");

const badgeText = computed(() =>
  storageStatus.value === "unavailable" ? "Storage unavailable" : collectionLabel(collectionStatus.value),
);
const badgeTone = computed(() => {
  if (storageStatus.value === "unavailable" || collectionStatus.value === "offline") return "is-bad";
  return collectionStatus.value === "healthy" ? "is-ok" : "is-warn";
});

function onPick(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  if (value) dashboard.selectSeries(value);
}
</script>

<template>
  <!-- The brand mark is decoration; the product name stays the accessible h1 text (D4). -->
  <section class="pnl pnl-head" data-testid="app-header" aria-label="Dashboard header">
    <div class="in">
      <div class="brand">
        <h1 class="t">
          <span class="visually-hidden" data-testid="product-name">Nano LLM Usage - JARVIS</span>
          <span class="mark" aria-hidden="true">Nano LLM Usage<i> - </i>JARVIS</span>
        </h1>
      </div>
      <div v-if="state.series.length > 1" class="sel">
        <label class="lab" for="series-picker">Series</label>
        <select
          id="series-picker"
          class="series-picker"
          data-testid="series-picker"
          :value="state.selectedSeriesId ?? ''"
          @change="onPick"
        >
          <option
            v-for="item in state.series"
            :key="item.series_id"
            :value="item.series_id"
            data-testid="series-option"
          >
            {{ item.model_name }} · engine {{ item.engine }}
          </option>
        </select>
      </div>
      <div class="hstat">
        <div class="hbox">
          <span class="lab">Last successful update · UTC</span>
          <span class="v" data-testid="last-update">{{ formatUtc(state.status?.last_success_at) }}</span>
        </div>
        <span class="badge" :class="badgeTone" data-testid="status-badge">
          <span class="dot" aria-hidden="true"></span>{{ badgeText }}
        </span>
        <span
          class="visually-hidden status-live"
          role="status"
          aria-live="polite"
          data-testid="status-live"
          >{{ liveLine }}</span
        >
      </div>
    </div>
  </section>
</template>
