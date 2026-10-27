<script setup lang="ts">
import { computed } from "vue";
import { ARC_END, ARC_START, arcPath, gaugeTicks, niceCeiling, polar } from "../lib/hudGeometry";
import { formatCompact } from "../lib/format";

// The mockup's dial, in the well's own 118x104 box (plan D6).
const props = defineProps<{ value: number | null; peak: number | null; unit: string; tone: string }>();

const CX = 59;
const CY = 60;
const R = 44;

const scale = computed(() => niceCeiling(props.peak));
const fraction = computed(() =>
  scale.value > 0 && props.value !== null ? Math.max(0, Math.min(1, props.value / scale.value)) : 0,
);
const ticks = gaugeTicks(CX, CY, R);
const track = arcPath(CX, CY, R, ARC_START, ARC_END);
const valueArc = computed(() =>
  fraction.value > 0
    ? arcPath(CX, CY, R, ARC_START, ARC_START + (ARC_END - ARC_START) * Math.max(fraction.value, 0.004))
    : null,
);
const needle = computed(() =>
  polar(CX, CY, R - 15, ARC_START + (ARC_END - ARC_START) * fraction.value),
);
const tone = computed(() => (props.value === null ? "tone-muted" : props.tone));
</script>

<template>
  <!-- Decorative: the numeric value is the text next to the dial (D6). -->
  <svg class="dial" :class="tone" viewBox="0 0 118 104" aria-hidden="true">
    <line
      v-for="(tick, index) in ticks"
      :key="index"
      class="tick"
      :class="{ major: tick.major }"
      :x1="tick.x1.toFixed(1)"
      :y1="tick.y1.toFixed(1)"
      :x2="tick.x2.toFixed(1)"
      :y2="tick.y2.toFixed(1)"
    />
    <path class="track" :d="track" />
    <template v-if="valueArc">
      <path class="glow" :d="valueArc" />
      <path class="value" :d="valueArc" />
    </template>
    <line class="needle" :x1="CX" :y1="CY" :x2="needle.x.toFixed(1)" :y2="needle.y.toFixed(1)" />
    <circle class="hub" :cx="CX" :cy="CY" r="3.4" />
    <text :x="CX" :y="CY + 27" text-anchor="middle" class="unit-label">{{ unit }}</text>
    <text :x="CX - R - 2" :y="CY + 40" class="end-label">0</text>
    <text v-if="scale > 0" :x="CX + R + 2" :y="CY + 40" text-anchor="end" class="end-label">{{
      formatCompact(scale)
    }}</text>
  </svg>
</template>
