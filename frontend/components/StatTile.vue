<template>
  <div class="rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3">
    <div class="text-xs uppercase tracking-wide text-slate-500">{{ label }}</div>
    <div class="mt-1 font-mono text-2xl" :class="tone">{{ value }}</div>
    <div v-if="hint" class="mt-0.5 text-xs text-slate-500">{{ hint }}</div>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  label: string
  value: string | number
  hint?: string
  tone?: string
}>()
</script>
