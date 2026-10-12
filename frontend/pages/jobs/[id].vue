<template>
  <div v-if="job" class="space-y-6">
    <section class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <NuxtLink to="/" class="text-xs text-slate-500 hover:text-slate-300">back</NuxtLink>
        <h1 class="mt-1 text-2xl font-semibold tracking-tight">
          {{ job.store_name || job.source }}
        </h1>
        <p class="mt-1 text-sm text-slate-400">
          {{ job.platform }} to {{ target?.name || job.target_id }}
        </p>
      </div>
      <div class="flex items-center gap-2">
        <button
          :disabled="!stats?.corrections || relearning"
          class="rounded-md border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-xs text-sky-200 transition hover:bg-sky-500/20 disabled:cursor-not-allowed disabled:border-slate-700 disabled:bg-transparent disabled:text-slate-500"
          @click="relearn"
        >
          {{ relearning ? 'Relearning' : `Apply my ${stats?.corrections || 0} corrections` }}
        </button>
        <button
          class="rounded-md border border-slate-700 px-3 py-2 text-xs text-slate-300 transition hover:bg-slate-900"
          @click="download('csv')"
        >
          Export CSV
        </button>
        <button
          class="rounded-md border border-slate-700 px-3 py-2 text-xs text-slate-300 transition hover:bg-slate-900"
          @click="download('json')"
        >
          Export JSON
        </button>
      </div>
    </section>

    <section v-if="stats" class="grid gap-3 sm:grid-cols-5">
      <StatTile label="Products" :value="stats.products" />
      <StatTile
        label="Need review"
        :value="stats.needs_review"
        :tone="stats.needs_review ? 'text-amber-300' : 'text-emerald-400'"
      />
      <StatTile label="Reviewed" :value="stats.reviewed" tone="text-emerald-400" />
      <StatTile
        label="Unmapped"
        :value="stats.unmapped"
        :tone="stats.unmapped ? 'text-rose-400' : 'text-slate-100'"
      />
      <StatTile
        label="Mean confidence"
        :value="`${Math.round(stats.mean_confidence * 100)}%`"
        :hint="target ? `threshold ${Math.round(target.confidence_threshold * 100)}%` : undefined"
      />
    </section>

    <p v-if="relearnMessage" class="rounded-md border border-sky-500/30 bg-sky-500/5 px-4 py-3 text-sm text-sky-200">
      {{ relearnMessage }}
    </p>

    <section class="space-y-3">
      <div class="flex items-center gap-2 text-xs">
        <button
          v-for="option in filters"
          :key="option.value"
          class="rounded-full px-3 py-1 transition"
          :class="
            filter === option.value
              ? 'bg-slate-100 text-slate-950'
              : 'border border-slate-700 text-slate-400 hover:text-slate-200'
          "
          @click="setFilter(option.value)"
        >
          {{ option.label }}
        </button>
      </div>

      <p v-if="!products.length" class="rounded-lg border border-slate-800 px-4 py-8 text-center text-sm text-slate-500">
        Nothing here.
      </p>

      <div v-else class="divide-y divide-slate-800 overflow-hidden rounded-lg border border-slate-800">
        <NuxtLink
          v-for="product in products"
          :key="product.id"
          :to="`/products/${product.id}`"
          class="flex items-center gap-4 px-4 py-3 transition hover:bg-slate-900/60"
        >
          <SafeImage
            :src="product.image_url"
            :alt="product.title"
            img-class="h-10 w-10 shrink-0 rounded object-cover"
          />

          <div class="min-w-0 flex-1">
            <div class="truncate text-sm">{{ product.title }}</div>
            <div class="mt-0.5 truncate text-xs text-slate-500">
              <span v-if="product.target_category_path.length">
                {{ product.target_category_path.join(' / ') }}
              </span>
              <span v-else class="text-rose-400">unmapped</span>
              <span v-if="product.missing_attributes.length" class="text-amber-400">
                · missing {{ product.missing_attributes.join(', ') }}
              </span>
            </div>
          </div>

          <ConfidenceBar
            :value="product.category_confidence"
            :threshold="target?.confidence_threshold"
          />

          <span
            class="w-16 shrink-0 text-right text-xs"
            :class="product.reviewed ? 'text-emerald-400' : product.needs_review ? 'text-amber-300' : 'text-slate-500'"
          >
            {{ product.reviewed ? 'reviewed' : product.needs_review ? 'review' : 'ok' }}
          </span>
        </NuxtLink>
      </div>
    </section>
  </div>

  <p v-else-if="error" class="text-sm text-rose-400">{{ error }}</p>
  <p v-else class="text-sm text-slate-500">Loading.</p>
</template>

<script setup lang="ts">
import type { JobSummary, ProductSummary, ReviewStats, TargetSummary } from '~/types/api'

const api = useApi()
const route = useRoute()
const jobId = computed(() => String(route.params.id))

const job = ref<JobSummary | null>(null)
const target = ref<TargetSummary | null>(null)
const stats = ref<ReviewStats | null>(null)
const products = ref<ProductSummary[]>([])
const filter = ref<'review' | 'reviewed' | 'all'>('review')
const relearning = ref(false)
const relearnMessage = ref('')
const error = ref('')

const filters = [
  { value: 'review' as const, label: 'Needs review' },
  { value: 'reviewed' as const, label: 'Reviewed' },
  { value: 'all' as const, label: 'All' },
]

async function loadProducts() {
  const query: Record<string, boolean> = {}
  if (filter.value === 'review') query.needs_review = true
  if (filter.value === 'reviewed') query.reviewed = true
  products.value = await api.products(jobId.value, query)
}

async function setFilter(next: 'review' | 'reviewed' | 'all') {
  filter.value = next
  await loadProducts()
}

async function load() {
  try {
    job.value = await api.job(jobId.value)
    target.value = await api.target(job.value.target_id)
    stats.value = await api.stats(jobId.value)
    await loadProducts()
  } catch (err) {
    error.value = (err as { data?: { detail?: string } })?.data?.detail || 'could not load this job'
  }
}

async function relearn() {
  relearning.value = true
  relearnMessage.value = ''
  try {
    const before = stats.value?.needs_review ?? 0
    stats.value = await api.relearn(jobId.value)
    const after = stats.value.needs_review
    relearnMessage.value =
      before === after
        ? 'Nemotron reran the unreviewed products. The same ones still need a look.'
        : `Nemotron reran the unreviewed products using your corrections. ${before - after} fewer need review.`
    await loadProducts()
  } catch (err) {
    relearnMessage.value =
      (err as { data?: { detail?: string } })?.data?.detail || 'relearning failed'
  } finally {
    relearning.value = false
  }
}

async function download(fmt: string) {
  const result = await api.exportJob(jobId.value, fmt)
  const blob = new Blob([result.body], {
    type: fmt === 'csv' ? 'text/csv' : 'application/json',
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = result.filename
  link.click()
  URL.revokeObjectURL(url)
}

onMounted(load)
</script>
