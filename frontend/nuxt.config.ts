export default defineNuxtConfig({
  compatibilityDate: '2026-10-01',
  modules: ['@nuxtjs/tailwindcss'],
  ssr: false,
  devtools: { enabled: false },
  app: {
    head: {
      title: 'CatalogBridge',
      meta: [{ name: 'description', content: 'Review a migrated product catalog' }],
    },
  },
  runtimeConfig: {
    public: {
      apiBase: process.env.NUXT_PUBLIC_API_BASE || 'http://127.0.0.1:8000',
    },
  },
})
