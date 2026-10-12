<template>
  <div class="space-y-8">
    <section class="space-y-3">
      <h1 class="text-2xl font-semibold tracking-tight">Migrate a catalog</h1>
      <p class="max-w-2xl text-sm text-slate-400">
        Point CatalogBridge at a store or a spreadsheet. It reads the catalog, converts every
        product to the target marketplace's schema, and flags anything a human should check.
      </p>
    </section>

    <form class="space-y-4 rounded-lg border border-slate-800 bg-slate-900/40 p-6" @submit.prevent="start">
      <div class="grid gap-4 sm:grid-cols-2">
        <label class="space-y-1.5">
          <span class="block text-xs uppercase tracking-wide text-slate-500">Source</span>
          <input
            v-model="form.source"
            required
            placeholder="examplestore.com or ./catalog.csv"
            class="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-sky-500"
          />
          <span class="block text-xs text-slate-500">
            Shopify, WooCommerce, a website or a spreadsheet. The platform is detected for you.
          </span>
        </label>

        <label class="space-y-1.5">
          <span class="block text-xs uppercase tracking-wide text-slate-500">Target marketplace</span>
          <select
            v-model="form.target_id"
            required
            class="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-sky-500"
          >
            <option v-for="target in targets" :key="target.id" :value="target.id">
              {{ target.name }} ({{ target.categories.length }} categories)
            </option>
          </select>
          <span v-if="selected" class="block text-xs text-slate-500">
            Flags anything below {{ Math.round(selected.confidence_threshold * 100) }}% confidence.
          </span>
        </label>
      </div>

      <div class="grid gap-4 sm:grid-cols-2">
        <label class="space-y-1.5">
          <span class="block text-xs uppercase tracking-wide text-slate-500">Limit</span>
          <input
            v-model.number="form.limit"
            type="number"
            min="1"
            max="5000"
            class="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-sky-500"
          />
        </label>
        <label class="space-y-1.5">
          <span class="block text-xs uppercase tracking-wide text-slate-500">Retrieval</span>
          <select
            v-model="form.retriever"
            class="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-sky-500"
          >
            <option value="embedding">Embeddings (better)</option>
            <option value="keyword">Keyword (baseline)</option>
          </select>
        </label>
      </div>

      <label class="flex items-start gap-2.5 rounded-md border border-amber-500/30 bg-amber-500/5 p-3">
        <input v-model="form.confirmed_ownership" type="checkbox" class="mt-0.5" />
        <span class="text-xs text-amber-200/90">
          I own this catalog or am authorized to migrate it. Nothing is published automatically:
          results go to review and then to an export file.
        </span>
      </label>

      <div class="flex items-center gap-3">
        <button
          type="submit"
          :disabled="busy || !form.confirmed_ownership"
          class="rounded-md bg-sky-500 px-4 py-2 text-sm font-medium text-slate-950 transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
        >
          {{ busy ? 'Migrating, this takes a while' : 'Start migration' }}
        </button>
        <p v-if="error" class="text-sm text-rose-400">{{ error }}</p>
      </div>
    </form>

    <section v-if="jobs.length" class="space-y-3">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Recent migrations</h2>
      <div class="divide-y divide-slate-800 overflow-hidden rounded-lg border border-slate-800">
        <NuxtLink
          v-for="job in jobs"
          :key="job.id"
          :to="`/jobs/${job.id}`"
          class="flex items-center justify-between px-4 py-3 transition hover:bg-slate-900/60"
        >
          <div class="min-w-0">
            <div class="truncate text-sm">{{ job.store_name || job.source }}</div>
            <div class="mt-0.5 text-xs text-slate-500">
              {{ job.platform }} to {{ job.target_id }}
            </div>
          </div>
          <div class="flex shrink-0 items-center gap-6 text-xs">
            <span v-if="job.status !== 'ready'" class="text-amber-300">{{ job.status }}</span>
            <span class="text-slate-400">{{ job.product_count }} products</span>
            <span :class="job.needs_review_count ? 'text-amber-300' : 'text-emerald-400'">
              {{ job.needs_review_count }} to review
            </span>
          </div>
        </NuxtLink>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import type { JobSummary, TargetSummary } from '~/types/api'

const api = useApi()
const router = useRouter()

const targets = ref<TargetSummary[]>([])
const jobs = ref<JobSummary[]>([])
const busy = ref(false)
const error = ref('')

const form = reactive({
  source: '',
  target_id: '',
  limit: 50 as number | null,
  retriever: 'embedding',
  confirmed_ownership: false,
})

const selected = computed(() => targets.value.find((t) => t.id === form.target_id))

async function load() {
  try {
    targets.value = await api.targets()
    if (targets.value.length && !form.target_id) form.target_id = targets.value[0].id
    jobs.value = await api.jobs()
  } catch (err) {
    error.value = describe(err)
  }
}

async function start() {
  busy.value = true
  error.value = ''
  try {
    const job = await api.createJob({
      source: form.source,
      target_id: form.target_id,
      limit: form.limit,
      retriever: form.retriever,
      confirmed_ownership: form.confirmed_ownership,
    })
    if (job.status === 'failed') {
      error.value = job.error || 'the migration failed'
      jobs.value = await api.jobs()
      return
    }
    await router.push(`/jobs/${job.id}`)
  } catch (err) {
    error.value = describe(err)
  } finally {
    busy.value = false
  }
}

function describe(err: unknown): string {
  const detail = (err as { data?: { detail?: string } })?.data?.detail
  if (detail) return detail
  const message = (err as { message?: string })?.message
  return message || 'something went wrong'
}

onMounted(load)
</script>
