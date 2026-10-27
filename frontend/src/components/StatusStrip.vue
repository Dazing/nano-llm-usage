<script setup lang="ts">
import { computed } from "vue";
import { dashboard } from "../lib/dashboard";
import { formatUtc, rangeErrorText } from "../lib/format";

// One strip for every collection message that needs the operator's attention (plan D5):
// it replaces the old separate offline and API-error banners. A healthy collection runs
// no strip at all — the badge and the header's polite region carry that state.
const props = defineProps<{
  firstLoad: boolean;
  empty: boolean;
  status: string;
}>();

const state = dashboard.state;

const OFFLINE_COPY =
  "Collection is offline: no successful sample for more than two poll intervals. Values below are the last observed history.";
const STALE_COPY =
  "Series is stale: the collector is running, the last accepted sample is older than the stale window. Values are the last accepted history.";
const STARTING_COPY = "Collecting: the first successful sample has not been accepted yet.";
const EMPTY_COPY = "No series collected yet. Collection starts after the first successful sample.";

interface StripCase {
  code: string;
  tone: string;
  text: string;
  testid?: string;
  role?: string;
  notice?: string;
}

const message = computed<StripCase | null>(() => {
  if (state.apiError) {
    return {
      code: `API ${state.apiError.code}`,
      tone: "is-bad",
      text: rangeErrorText(state.apiError.code),
      testid: "api-error",
      role: "alert",
      notice: state.staleSince ? `Showing data from ${formatUtc(state.staleSince)}` : undefined,
    };
  }
  if (props.status === "offline") {
    return { code: "OFFLINE", tone: "is-warn", text: OFFLINE_COPY, testid: "offline-banner", role: "status" };
  }
  if (state.summary?.state === "stale") {
    return { code: "STALE", tone: "is-warn", text: STALE_COPY };
  }
  if (!props.firstLoad && props.status === "starting") {
    return { code: "STARTING", tone: "is-idle", text: STARTING_COPY };
  }
  if (props.firstLoad) {
    return { code: "SYS", tone: "is-idle", text: "Loading dashboard…" };
  }
  if (props.empty) {
    return { code: "SYS", tone: "is-idle", text: EMPTY_COPY };
  }
  // Healthy: no bar. The nominal read-out lived here; it is the header badge's job now.
  return null;
});
</script>

<template>
  <section
    v-if="message"
    class="pnl pnl-strip"
    data-testid="status-strip"
    aria-label="Collection status"
  >
    <div class="in">
      <div class="strip-msg" :class="message.tone" :data-testid="message.testid" :role="message.role">
        <span class="code">{{ message.code }}</span>
        <span class="txt">{{ message.text }}</span>
        <span v-if="message.notice" class="stale-notice" data-testid="stale-notice" role="status">{{
          message.notice
        }}</span>
      </div>
    </div>
  </section>
</template>
