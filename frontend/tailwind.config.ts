import type { Config } from 'tailwindcss'

export default <Partial<Config>>{
  content: [
    './app.vue',
    './components/**/*.vue',
    './pages/**/*.vue',
    './layouts/**/*.vue',
  ],
  theme: {
    extend: {
      colors: {
        ink: '#12151c',
        slate: {
          950: '#0b0e14',
        },
      },
    },
  },
}
