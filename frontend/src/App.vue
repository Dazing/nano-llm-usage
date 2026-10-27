<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from "vue";
import { dashboard } from "./lib/dashboard";
import { live as liveStore } from "./lib/live";
import { collectionLabel, formatUtc } from "./lib/format";
import ApiErrorBanner from "./components/ApiErrorBanner.vue";
import AppHeader from "./components/AppHeader.vue";
import DiagnosticsDisclosure from "./components/DiagnosticsDisclosure.vue";
import EmptyState from "./components/EmptyState.vue";
import LivePanel from "./components/LivePanel.vue";
import OfflineBanner from "./components/OfflineBanner.vue";
import RatesPanel from "./components/RatesPanel.vue";
import SkeletonBlock from "./components/SkeletonBlock.vue";
import SummaryCards from "./components/SummaryCards.vue";
import TrendPanel from "./components/TrendPanel.vue";

const state = dashboard.state;

onMounted(() => {
  dashboard.mount();
  liveStore.mount();
});
onBeforeUnmount(() => {
  dashboard.unmount();
  liveStore.unmount();
});

const collectionStatus = computed(
  () => state.summary?.collection_status ?? state.seriesCollectionStatus ?? "starting",
);
const offline = computed(() => collectionStatus.value === "offline");
const isEmpty = computed(() => !state.firstLoad && state.series.length === 0);
const live = computed(
  () =>
    `${collectionLabel(collectionStatus.value)}. ` +
    (state.status?.last_success_at
      ? `Last successful update ${formatUtc(state.status.last_success_at)}.`
      : "No successful update yet."),
);
</script>

<template>
  <div class="shell">
    <AppHeader />
    <main class="main">
      <p class="visually-hidden" role="status" aria-live="polite">{{ live }}</p>
      <OfflineBanner v-if="offline" />
      <ApiErrorBanner v-if="state.apiError" />
      <!-- The live row renders its five placeholders before any 15 s data exists. -->
      <LivePanel />
      <SkeletonBlock v-if="state.firstLoad" />
      <EmptyState v-else-if="isEmpty" />
      <template v-else>
        <SummaryCards />
        <TrendPanel />
        <RatesPanel />
        <DiagnosticsDisclosure />
      </template>
    </main>
  </div>
</template>
