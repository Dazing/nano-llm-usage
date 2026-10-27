<script setup lang="ts">
defineProps<{ label: string; testid?: string; tone?: "muted" | "strong" }>();
</script>

<template>
  <p class="label-value" :class="tone === 'muted' ? 'is-muted' : ''">
    <span class="lv-label">{{ label }}</span>
    <span v-if="testid" class="lv-value" :data-testid="testid"><slot /></span>
    <span v-else class="lv-value"><slot /></span>
  </p>
</template>
