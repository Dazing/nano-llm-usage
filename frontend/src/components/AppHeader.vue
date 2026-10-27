<script setup lang="ts">
import { computed, ref } from "vue";
import { dashboard } from "../lib/dashboard";
import { collectionLabel, formatUtc } from "../lib/format";

const state = dashboard.state;

type ThemeMode = "system" | "light" | "dark";
const stored = localStorage.getItem("jarvis-theme");
const theme = ref<ThemeMode>(stored === "light" || stored === "dark" ? stored : "system");

function cycleTheme(): void {
  theme.value = theme.value === "system" ? "light" : theme.value === "light" ? "dark" : "system";
  if (theme.value === "system") {
    localStorage.removeItem("jarvis-theme");
    delete document.documentElement.dataset.theme;
    return;
  }
  localStorage.setItem("jarvis-theme", theme.value);
  document.documentElement.dataset.theme = theme.value;
}

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

const seriesName = computed(() => {
  const selected = state.summary ?? state.series.find((item) => item.series_id === state.selectedSeriesId);
  if (!selected) return "No series yet";
  return `${selected.model_name} · engine ${selected.engine}`;
});

const lastUpdate = computed(() =>
  state.status?.last_success_at
    ? `Last successful update ${formatUtc(state.status.last_success_at)}`
    : "Last successful update no data",
);

function onPick(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  if (value) dashboard.selectSeries(value);
}
</script>

<template>
  <header class="app-header" data-testid="app-header">
    <div class="header-main">
      <h1 data-testid="product-name">Jarvis Token Dashboard</h1>
      <p class="series-name" data-testid="series-name">{{ seriesName }}</p>
    </div>
    <div class="header-side">
      <span class="badge" :class="badgeTone" data-testid="status-badge">{{ badgeText }}</span>
      <p class="last-update" data-testid="last-update">{{ lastUpdate }}</p>
      <button type="button" class="theme-toggle" data-testid="theme-toggle" @click="cycleTheme">
        Theme: {{ theme }}
      </button>
      <label v-if="state.series.length > 1" class="series-picker-label" for="series-picker">Series</label>
      <select
        v-if="state.series.length > 1"
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
  </header>
</template>
