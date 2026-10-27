<script setup lang="ts">
defineProps<{ title: string; testid: string }>();
</script>

<template>
  <section class="card" :data-testid="testid" :aria-label="title">
    <div class="ch">
      <h3>{{ title }}</h3>
      <span class="utc">UTC</span>
      <slot name="since" />
    </div>
    <slot />
  </section>
</template>
