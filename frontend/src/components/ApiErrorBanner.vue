<script setup lang="ts">
import { computed } from "vue";
import { dashboard } from "../lib/dashboard";
import { formatUtc, rangeErrorText } from "../lib/format";

const state = dashboard.state;
const text = computed(() => (state.apiError ? rangeErrorText(state.apiError.code) : ""));
</script>

<template>
  <div class="banner banner-error" data-testid="api-error" role="alert">
    <p>{{ text }}</p>
    <p v-if="state.staleSince" class="stale-notice" data-testid="stale-notice">
      Showing data from {{ formatUtc(state.staleSince) }}
    </p>
  </div>
</template>
