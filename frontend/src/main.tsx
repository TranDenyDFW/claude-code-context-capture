import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './index.css'
import App from './App.tsx'

const client = new QueryClient({
  defaultOptions: {
    queries: {
      // The store is local and read-only from here, so refetching because a window regained focus
      // buys nothing and costs a rebuild of whatever tab is open, which can be over a second.
      refetchOnWindowFocus: false,
      // One retry, not three. A failure here is almost always "the API is not running", and three
      // attempts turn an instant, accurate error message into a nine-second wait for the same one.
      retry: 1,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
