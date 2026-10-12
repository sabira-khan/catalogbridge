import type {
  CorrectionSummary,
  JobSummary,
  ProductDetail,
  ProductSummary,
  ReviewStats,
  TargetSummary,
} from '~/types/api'

export interface CorrectionPayload {
  target_category_id?: string | null
  attributes?: Record<string, string>
  note?: string | null
}

export interface JobPayload {
  source: string
  target_id: string
  platform?: string | null
  limit?: number | null
  retriever?: string
  confirmed_ownership: boolean
}

export function useApi() {
  const base = useRuntimeConfig().public.apiBase

  async function call<T>(path: string, options: Record<string, unknown> = {}): Promise<T> {
    return await $fetch<T>(`${base}${path}`, options)
  }

  return {
    base,
    health: () => call<{ status: string; llm_configured: boolean }>('/health'),
    targets: () => call<TargetSummary[]>('/api/targets'),
    target: (id: string) => call<TargetSummary>(`/api/targets/${id}`),
    jobs: () => call<JobSummary[]>('/api/jobs'),
    job: (id: string) => call<JobSummary>(`/api/jobs/${id}`),
    createJob: (body: JobPayload) => call<JobSummary>('/api/jobs', { method: 'POST', body }),
    stats: (id: string) => call<ReviewStats>(`/api/jobs/${id}/stats`),
    products: (id: string, query: Record<string, unknown> = {}) =>
      call<ProductSummary[]>(`/api/jobs/${id}/products`, { query }),
    product: (id: string) => call<ProductDetail>(`/api/products/${id}`),
    correct: (id: string, body: CorrectionPayload) =>
      call<ProductDetail>(`/api/products/${id}/correction`, { method: 'POST', body }),
    corrections: (id: string) => call<CorrectionSummary[]>(`/api/jobs/${id}/corrections`),
    relearn: (id: string) => call<ReviewStats>(`/api/jobs/${id}/relearn`, { method: 'POST' }),
    exportJob: (id: string, fmt: string) =>
      call<{ format: string; filename: string; body: string }>(`/api/jobs/${id}/export`, {
        query: { fmt },
      }),
  }
}
