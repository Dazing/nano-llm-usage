<script setup lang="ts">
import { computed } from "vue";
import { ARC_END, ARC_START, arcPath, ratioFraction, ratioTone, type Bands } from "../lib/hudGeometry";

// Concentric ratio rings, shortest window outermost (plan D6, item 6): 132x132 box,
// radii 20 + 13*i counted from the last window, so index 0 (the 5 s rolling value) is
// the largest ring and the lifetime/long windows sit inside it.
const props = defineProps<{ values: (number | null)[]; bands: Bands }>();

const CX = 66;
const CY = 66;

const rings = computed(() => {
  const outer = props.values.length - 1;
  return props.values.map((value, index) => {
    const radius = 20 + (outer - index) * 13;
    const fraction = ratioFraction(value);
    return {
      radius,
      tone: ratioTone(value, props.bands),
      track: arcPath(CX, CY, radius, ARC_START, ARC_END),
      value:
        fraction !== null && fraction > 0.002
          ? arcPath(CX, CY, radius, ARC_START, ARC_START + (ARC_END - ARC_START) * fraction)
          : null,
    };
  });
});
</script>

<template>
  <!-- Decorative: every ring's value is printed in the row list beside it (D6). -->
  <svg class="rings-svg" viewBox="0 0 132 132" aria-hidden="true">
    <circle class="dashed" :cx="CX" :cy="CY" r="62" />
    <template v-for="ring in rings" :key="ring.radius">
      <path class="track" :d="ring.track" />
      <template v-if="ring.value">
        <path class="glow" :class="ring.tone" :d="ring.value" />
        <path class="value" :class="ring.tone" :d="ring.value" />
      </template>
    </template>
  </svg>
</template>
