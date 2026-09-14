<script setup lang="ts">
withDefaults(defineProps<{ size?: number }>(), { size: 40 })
</script>
<template>
  <img
    src="/coreman-mark.svg"
    alt="CoreMan"
    :width="size"
    :height="size"
    class="coreman-logo"
  >
</template>
<style scoped>.coreman-logo { display: block; flex-shrink: 0; }</style>
