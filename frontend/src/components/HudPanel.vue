<script setup lang="ts">
// The shared HUD frame (plan D10): one chamfered .pnl > .in panel with the numbered
// head rule. `class` and `aria-label` reach the <section> by attribute fallthrough.
defineProps<{ index: string; title: string; testid?: string }>();
</script>

<template>
  <section class="pnl" :data-testid="testid">
    <div class="in">
      <div class="ph">
        <span class="idx">{{ index }}</span>
        <h2>{{ title }}</h2>
        <span class="rule"></span>
        <slot name="meta" />
      </div>
      <slot />
    </div>
  </section>
</template>
