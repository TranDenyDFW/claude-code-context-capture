import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

// Separate from vite.config.ts so the dev server's proxy and port settings, which mean nothing to a
// test run, cannot affect it. The alias is repeated because it is the one thing both need.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'jsdom',
    // TRUE, and not as a convenience. Testing Library registers its own `afterEach` cleanup only
    // when it can see a global `afterEach`; without it, every render stacks into the same document
    // and the second test onwards fails with "found multiple elements with the role table". The
    // failure looks like a bug in the component and is a bug in this config.
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
    // A test file that renders nothing and asserts nothing would otherwise pass. This suite's whole
    // purpose is catching a renderer that drops data, so an empty run is a failure.
    passWithNoTests: false,
  },
})
