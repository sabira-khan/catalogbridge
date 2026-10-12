<template>
  <div class="flex items-center gap-2">
    <div class="h-1.5 w-20 overflow-hidden rounded-full bg-slate-800">
      <div class="h-full rounded-full transition-all" :class="tone" :style="{ width: `${pct}%` }" />
    </div>
    <span class="w-9 text-right font-mono text-xs" :class="textTone">{{ pct }}%</span>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{ value: number; threshold?: number }>()

const pct = computed(() => Math.round(props.value * 100))
const limit = computed(() => (props.threshold ?? 0.7) * 100)

const tone = computed(() => {
  if (pct.value >= 80) return 'bg-emerald-400'
  if (pct.value >= limit.value) return 'bg-sky-400'
  return 'bg-amber-400'
})

const textTone = computed(() => (pct.value >= limit.value ? 'text-slate-400' : 'text-amber-300'))
</script>
