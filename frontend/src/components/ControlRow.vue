<script setup lang="ts">
defineProps<{ label?: string }>();
</script>

<template>
  <div class="control-row" role="group" :aria-label="label ?? 'Controls'">
    <slot />
  </div>
</template>
