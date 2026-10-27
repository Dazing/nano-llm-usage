<script setup lang="ts">
// A row of cells: filled for the count/fraction received, empty for the rest (plan D6, D10).
withDefaults(
  defineProps<{ filled: number; cells: number; variant?: "segbar" | "meter" }>(),
  { variant: "segbar" },
);
</script>

<template>
  <span class="cells" :class="variant" aria-hidden="true">
    <i v-for="index in cells" :key="index" :class="{ on: index <= filled }"></i>
  </span>
</template>
