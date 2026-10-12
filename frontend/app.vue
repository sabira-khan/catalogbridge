<template>
  <div class="min-h-screen bg-slate-950 text-slate-100 antialiased">
    <header class="border-b border-slate-800">
      <div class="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
        <NuxtLink to="/" class="flex items-baseline gap-3">
          <span class="text-lg font-semibold tracking-tight">CatalogBridge</span>
          <span class="text-xs text-slate-500">catalog migration review</span>
        </NuxtLink>
        <div class="flex items-center gap-2 text-xs">
          <span
            class="inline-block h-2 w-2 rounded-full"
            :class="llmReady ? 'bg-emerald-400' : 'bg-amber-400'"
          />
          <span class="text-slate-400">
            {{ llmReady ? 'Nemotron connected' : 'no Nebius key' }}
          </span>
        </div>
      </div>
    </header>
    <main class="mx-auto max-w-6xl px-6 py-8">
      <NuxtPage />
    </main>
  </div>
</template>

<script setup lang="ts">
const api = useApi()
const llmReady = ref(false)

onMounted(async () => {
  try {
    llmReady.value = (await api.health()).llm_configured
  } catch {
    llmReady.value = false
  }
})
</script>
