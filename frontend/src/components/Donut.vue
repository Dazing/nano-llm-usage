<script setup lang="ts">
import { computed } from "vue";

// The mockup's cache-share ring: 66 unit box, r 26, drawn from the top.
const props = defineProps<{ value: number | null }>();

const RADIUS = 26;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

const fraction = computed(() =>
  props.value === null ? 0 : Math.max(0, Math.min(1, props.value)),
);
const arc = computed(() => CIRCUMFERENCE * fraction.value);
const percent = computed(() => (props.value === null ? "-" : `${Math.round(props.value * 100)}%`));
</script>

<template>
  <!-- Decorative: the same share is printed as text beside it. -->
  <svg class="donut" viewBox="0 0 66 66" aria-hidden="true">
    <circle class="track" cx="33" cy="33" :r="RADIUS" />
    <circle
      v-if="fraction > 0"
      class="value"
      cx="33"
      cy="33"
      :r="RADIUS"
      :stroke-dasharray="`${arc.toFixed(2)} ${CIRCUMFERENCE.toFixed(2)}`"
      transform="rotate(-90 33 33)"
    />
    <text class="pct" x="33" y="37" text-anchor="middle">{{ percent }}</text>
  </svg>
</template>
