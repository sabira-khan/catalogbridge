<template>
  <img
    v-if="src && !failed"
    :src="src"
    :alt="alt"
    :class="imgClass"
    loading="lazy"
    @error="failed = true"
  />
  <div v-else :class="[imgClass, 'flex items-center justify-center bg-slate-800']">
    <span class="text-[10px] uppercase tracking-wide text-slate-600">no image</span>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  src: string | null
  alt: string
  imgClass: string
}>()

const failed = ref(false)
</script>
