import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'

// The API runs on 8059 (the dashboard is on 8056; see c4x/api/__main__.py for why they are
// separate). Everything under /api is proxied there so the browser sees ONE origin during
// development: no CORS preflight, and the same relative URLs work when this is built and served
// by the API itself rather than by Vite.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    strictPort: true, // Windows will let a second process bind a port already in use rather than
                      // refusing it, and this repo has already paid once for two servers answering
                      // one port. Fail loudly instead.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8059',
        changeOrigin: false,
      },
    },
  },
})
