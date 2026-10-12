<template>
  <div v-if="product" class="space-y-6">
    <section class="flex items-start justify-between gap-6">
      <div class="min-w-0">
        <NuxtLink :to="`/jobs/${jobId}`" class="text-xs text-slate-500 hover:text-slate-300">
          back to review queue
        </NuxtLink>
        <h1 class="mt-1 text-2xl font-semibold tracking-tight">{{ product.title }}</h1>
        <p class="mt-1 text-sm text-slate-400">
          <span v-if="product.brand">{{ product.brand }}</span>
          <span v-if="product.source_category"> · {{ product.source_category }}</span>
          <span v-if="product.price"> · {{ product.price }} {{ product.currency }}</span>
        </p>
        <a
          v-if="product.source_url"
          :href="product.source_url"
          target="_blank"
          rel="noopener noreferrer"
          class="mt-1 inline-block text-xs text-sky-400 hover:text-sky-300"
        >
          view on the original store
        </a>
      </div>
      <div
        class="shrink-0 rounded-md px-3 py-1.5 text-xs"
        :class="
          product.reviewed
            ? 'bg-emerald-500/10 text-emerald-300'
            : product.needs_review
              ? 'bg-amber-500/10 text-amber-300'
              : 'bg-slate-800 text-slate-300'
        "
      >
        {{ product.reviewed ? 'reviewed' : product.needs_review ? 'needs review' : 'accepted' }}
      </div>
    </section>

    <div class="grid gap-6 lg:grid-cols-[1fr_380px]">
      <div class="space-y-6">
        <section class="space-y-3 rounded-lg border border-slate-800 bg-slate-900/40 p-5">
          <div class="flex items-center justify-between">
            <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">
              What Nemotron decided
            </h2>
            <ConfidenceBar
              :value="product.category_confidence"
              :threshold="target?.confidence_threshold"
            />
          </div>
          <p class="text-sm">
            <span v-if="product.target_category_path.length" class="font-medium">
              {{ product.target_category_path.join(' / ') }}
            </span>
            <span v-else class="text-rose-400">No category fitted</span>
          </p>
          <p class="text-sm italic text-slate-400">"{{ product.category_reason }}"</p>
          <div v-if="product.candidates.length" class="space-y-1.5 pt-1">
            <div class="text-xs uppercase tracking-wide text-slate-500">
              Shortlist it chose from, by {{ product.candidates[0].retriever }} similarity
            </div>
            <div
              v-for="candidate in product.candidates"
              :key="candidate.category_id"
              class="flex items-center justify-between text-xs"
            >
              <span
                :class="
                  candidate.category_id === product.target_category_id
                    ? 'text-sky-300'
                    : 'text-slate-400'
                "
              >
                {{ candidate.path.join(' / ') || candidate.category_id }}
              </span>
              <span class="font-mono text-slate-500">{{ candidate.similarity.toFixed(3) }}</span>
            </div>
          </div>
        </section>

        <section v-if="product.images.length" class="flex gap-2 overflow-x-auto">
          <SafeImage
            v-for="image in product.images"
            :key="image"
            :src="image"
            :alt="product.title"
            img-class="h-28 w-28 shrink-0 rounded-md object-cover"
          />
        </section>

        <section v-if="product.description" class="space-y-2">
          <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Description</h2>
          <p class="whitespace-pre-line text-sm leading-relaxed text-slate-300">
            {{ product.description }}
          </p>
        </section>

        <section v-if="product.variants.length" class="space-y-2">
          <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">
            {{ product.variants.length }} variants
          </h2>
          <div class="overflow-hidden rounded-lg border border-slate-800">
            <table class="w-full text-left text-xs">
              <thead class="bg-slate-900/60 text-slate-500">
                <tr>
                  <th class="px-3 py-2 font-medium">SKU</th>
                  <th class="px-3 py-2 font-medium">Options</th>
                  <th class="px-3 py-2 text-right font-medium">Price</th>
                  <th class="px-3 py-2 text-right font-medium">Stock</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800">
                <tr v-for="(variant, index) in product.variants" :key="index">
                  <td class="px-3 py-2 font-mono text-slate-400">{{ variant.sku || '-' }}</td>
                  <td class="px-3 py-2">
                    <span v-if="variant.options.length">
                      {{ variant.options.map((o) => `${o.name} ${o.value}`).join(', ') }}
                    </span>
                    <span v-else class="text-slate-600">-</span>
                  </td>
                  <td class="px-3 py-2 text-right font-mono">{{ variant.price || '-' }}</td>
                  <td
                    class="px-3 py-2 text-right"
                    :class="variant.available === false ? 'text-slate-600' : ''"
                  >
                    {{ variant.stock ?? (variant.available === false ? 'out' : 'in stock') }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section v-if="product.source_attributes.length" class="space-y-2">
          <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">
            What the source stated
          </h2>
          <dl class="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
            <div
              v-for="(attribute, index) in product.source_attributes"
              :key="index"
              class="flex justify-between gap-3 border-b border-slate-800/60 py-1"
            >
              <dt class="text-slate-500">{{ attribute.name }}</dt>
              <dd class="text-right text-slate-300">
                {{ attribute.value }}<span v-if="attribute.unit"> {{ attribute.unit }}</span>
                <span v-if="attribute.origin === 'llm'" class="ml-1 text-amber-400">inferred</span>
              </dd>
            </div>
          </dl>
        </section>
      </div>

      <aside class="space-y-4 lg:sticky lg:top-6 lg:self-start">
        <form
          class="space-y-4 rounded-lg border border-slate-800 bg-slate-900/40 p-5"
          @submit.prevent="save"
        >
          <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Correct it
          </h2>

          <label class="space-y-1.5">
            <span class="block text-xs text-slate-500">Category</span>
            <select
              v-model="draft.category"
              class="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-sky-500"
            >
              <option v-for="option in categories" :key="option.id" :value="option.id">
                {{ option.path.join(' / ') }}
              </option>
            </select>
          </label>

          <div v-if="requiredAttributes.length" class="space-y-2">
            <span class="block text-xs text-slate-500">
              Required by this category
            </span>
            <label v-for="key in requiredAttributes" :key="key" class="block space-y-1">
              <span class="flex items-center justify-between text-xs">
                <span class="text-slate-400">{{ key }}</span>
                <span v-if="product.missing_attributes.includes(key)" class="text-amber-400">
                  missing
                </span>
              </span>
              <input
                v-model="draft.attributes[key]"
                :placeholder="allowedHint(key)"
                class="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm outline-none focus:border-sky-500"
              />
            </label>
          </div>

          <label class="space-y-1.5">
            <span class="block text-xs text-slate-500">Note, optional</span>
            <textarea
              v-model="draft.note"
              rows="2"
              placeholder="Why this was wrong, so the next batch gets it right"
              class="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-sky-500"
            />
          </label>

          <button
            type="submit"
            :disabled="saving"
            class="w-full rounded-md bg-sky-500 px-4 py-2 text-sm font-medium text-slate-950 transition hover:bg-sky-400 disabled:bg-slate-700 disabled:text-slate-400"
          >
            {{ saving ? 'Saving' : 'Save and mark reviewed' }}
          </button>

          <p class="text-xs text-slate-500">
            Your corrections are reused as examples when Nemotron reruns the rest of this batch.
          </p>
          <p v-if="message" class="text-xs text-emerald-400">{{ message }}</p>
          <p v-if="error" class="text-xs text-rose-400">{{ error }}</p>
        </form>
      </aside>
    </div>
  </div>

  <p v-else-if="error" class="text-sm text-rose-400">{{ error }}</p>
  <p v-else class="text-sm text-slate-500">Loading.</p>
</template>

<script setup lang="ts">
import type { JobSummary, ProductDetail, TargetSummary } from '~/types/api'

const api = useApi()
const route = useRoute()
const productId = computed(() => String(route.params.id))

const product = ref<ProductDetail | null>(null)
const target = ref<TargetSummary | null>(null)
const job = ref<JobSummary | null>(null)
const saving = ref(false)
const message = ref('')
const error = ref('')

const draft = reactive<{ category: string; attributes: Record<string, string>; note: string }>({
  category: '',
  attributes: {},
  note: '',
})

const jobId = computed(() => job.value?.id ?? '')
const categories = computed(() => target.value?.categories ?? [])

const requiredAttributes = computed(() => {
  const option = categories.value.find((c) => c.id === draft.category)
  return option?.required_attributes ?? []
})

function allowedHint(key: string): string {
  const existing = product.value?.attributes.find((a) => a.target_key === key)
  return existing?.value ?? ''
}

function seedDraft(detail: ProductDetail) {
  draft.category = detail.target_category_id ?? ''
  draft.attributes = {}
  for (const attribute of detail.attributes) {
    draft.attributes[attribute.target_key] = attribute.value
  }
}

async function load() {
  try {
    const detail = await api.product(productId.value)
    product.value = detail
    seedDraft(detail)
    job.value = await api.job(detail.job_id)
    target.value = await api.target(job.value.target_id)
  } catch (err) {
    error.value = (err as { data?: { detail?: string } })?.data?.detail || 'could not load'
  }
}

async function save() {
  saving.value = true
  message.value = ''
  error.value = ''
  try {
    const attributes: Record<string, string> = {}
    for (const [key, value] of Object.entries(draft.attributes)) {
      if (value && value.trim()) attributes[key] = value.trim()
    }
    const updated = await api.correct(productId.value, {
      target_category_id: draft.category || null,
      attributes,
      note: draft.note.trim() || null,
    })
    product.value = updated
    seedDraft(updated)
    message.value = 'Saved. This will guide the rest of the batch.'
  } catch (err) {
    error.value = (err as { data?: { detail?: string } })?.data?.detail || 'could not save'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
