<script setup lang="ts">
defineProps<{ title: string; testid: string }>();
</script>

<template>
  <section class="card" :data-testid="testid" :aria-label="title">
    <h2 class="card-title">
      {{ title }}
      <span class="utc-tag">UTC</span>
    </h2>
    <slot />
  </section>
</template>
