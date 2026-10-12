export interface CategoryOption {
  id: string
  name: string
  path: string[]
  required_attributes: string[]
}

export interface TargetSummary {
  id: string
  name: string
  description: string | null
  languages: string[]
  default_language: string
  confidence_threshold: number
  categories: CategoryOption[]
}

export interface JobSummary {
  id: string
  source: string
  platform: string
  target_id: string
  status: string
  store_name: string | null
  currency: string | null
  product_count: number
  needs_review_count: number
  error: string | null
  created_at: string
  updated_at: string
}

export interface ProductSummary {
  id: string
  job_id: string
  source_id: string
  title: string
  brand: string | null
  source_category: string | null
  price: string | null
  currency: string | null
  image_url: string | null
  variant_count: number
  target_category_id: string | null
  target_category_path: string[]
  category_confidence: number
  category_reason: string
  missing_attributes: string[]
  needs_review: boolean
  reviewed: boolean
}

export interface MappedAttribute {
  target_key: string
  value: string
  unit: string | null
  source_attribute: string | null
  confidence: number | null
}

export interface SourceAttribute {
  name: string | null
  value: string | null
  unit: string | null
  origin: string | null
}

export interface VariantOption {
  name: string
  value: string
}

export interface VariantDetail {
  sku: string | null
  title: string | null
  options: VariantOption[]
  price: string | null
  available: boolean | null
  stock: number | null
}

export interface CategoryCandidate {
  category_id: string
  path: string[]
  similarity: number
  retriever: string
}

export interface ProductDetail extends ProductSummary {
  description: string | null
  source_url: string | null
  images: string[]
  tags: string[]
  attributes: MappedAttribute[]
  source_attributes: SourceAttribute[]
  variants: VariantDetail[]
  candidates: CategoryCandidate[]
}

export interface ReviewStats {
  products: number
  needs_review: number
  reviewed: number
  unmapped: number
  mean_confidence: number
  corrections: number
}

export interface CorrectionSummary {
  product_id: string
  source_id: string
  title: string
  from_category_id: string | null
  to_category_id: string | null
  note: string | null
  created_at: string
}
