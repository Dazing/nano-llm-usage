<script setup lang="ts">
import { computed } from "vue";
import { sparkRuns } from "../lib/hudGeometry";

// One polyline per unbroken run of samples (plan D6, AC7): a gap breaks the line.
const props = withDefaults(defineProps<{ values: (number | null)[]; height?: number }>(), {
  height: 38,
});

const runs = computed(() => sparkRuns(props.values, 300, props.height));
const lastRun = computed(() => runs.value[runs.value.length - 1] ?? null);
</script>

<template>
  <!-- Decorative: the numbers are printed beside it, and the series is not selectable. -->
  <svg
    class="spark-svg"
    :viewBox="`0 0 300 ${height}`"
    preserveAspectRatio="none"
    aria-hidden="true"
  >
    <template v-for="(run, index) in runs" :key="index">
      <polygon class="fill" :points="run.fill" />
      <polyline class="line" :points="run.points" />
      <circle
        v-if="run === lastRun && !run.single"
        class="end-dot"
        :cx="run.lastX.toFixed(1)"
        :cy="run.lastY.toFixed(1)"
        r="2.6"
      />
    </template>
  </svg>
</template>
