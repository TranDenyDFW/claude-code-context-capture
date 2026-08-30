import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, ApiError, type Selection } from '@/api'
import { Pane } from '@/components/Pane'

/**
 * The shell: what is selected, which tab is open, and what the server says about both.
 *
 * The tab list comes FROM THE SERVER, not from a list written here. A hand-written list would be a
 * second source of truth for something that already has one, and it is exactly how the migration
 * proposal that started this work ended up planning three pages that no longer exist.
 */
export default function App() {
  const [tab, setTab] = useState<string | null>(null)
  const [selection, setSelection] = useState<Selection>({ scope: 'main' })

  const tabs = useQuery({ queryKey: ['tabs'], queryFn: api.tabs })
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30_000 })
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: () => api.sessions(300) })

  useEffect(() => {
    if (!tab && tabs.data?.length) setTab(tabs.data[0].id)
  }, [tab, tabs.data])

  const pane = useQuery({
    queryKey: ['tab', tab, selection.session, selection.scope, selection.cohort],
    queryFn: () => api.tab(tab!, selection),
    enabled: Boolean(tab),
    // The server holds an answer for five seconds; asking again inside that window would spend a
    // round trip to be handed the same bytes back.
    staleTime: 5_000,
    // NO `placeholderData: previous`. It was here, and it meant that switching from Window to Cost
    // showed WINDOW's tables and charts under the heading "Cost" for about a second: the right
    // label over the wrong numbers, which is the single worst thing a tool like this can display.
    // Dimming it did not help, because a dimmed wrong number is still a wrong number. Caught by
    // sweeping the tabs and comparing what the page showed against what the API returned.
    //
    // Nothing is lost by removing it. React Query still caches per key, so returning to a tab
    // already visited is instant; only a tab being seen for the first time shows the skeleton.
  })

  const cohorts = useMemo(() => {
    const found = new Set<string>()
    for (const row of sessions.data?.rows ?? []) if (row.project) found.add(row.project)
    return [...found].sort()
  }, [sessions.data])

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 border-b border-edge bg-page/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-3 px-6 py-3">
          <span className="text-[15px] font-semibold tracking-tight text-ink">c4x</span>
          <span className="text-[11px] text-ink-faint">
            {health.data ? health.data.db.split(/[\\/]/).slice(-2).join('/') : 'connecting'}
          </span>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Picker
              label="session"
              value={selection.session ?? ''}
              onChange={(value) => setSelection((was) => ({ ...was, session: value || null }))}
              options={[
                { value: '', label: 'every session' },
                ...(sessions.data?.rows ?? []).map((row) => ({
                  value: row.session_id,
                  label: `${row.title || row.session_id.slice(0, 8)}${
                    row.turns ? ` (${row.turns.toLocaleString()} turns)` : ''
                  }`,
                })),
              ]}
            />
            <Picker
              label="project"
              value={selection.cohort ?? ''}
              onChange={(value) => setSelection((was) => ({ ...was, cohort: value || null }))}
              options={[
                { value: '', label: 'every project' },
                ...cohorts.map((name) => ({ value: name, label: name })),
              ]}
            />
            <button
              onClick={() =>
                setSelection((was) => ({ ...was, scope: was.scope === 'all' ? 'main' : 'all' }))
              }
              title="Subagent turns run inside a session but are not part of its own context."
              className={`rounded-md border px-2.5 py-1.5 text-[12px] transition-colors ${
                selection.scope === 'all'
                  ? 'border-accent/60 bg-accent/10 text-accent'
                  : 'border-edge bg-panel text-ink-dim hover:text-ink'
              }`}
            >
              {selection.scope === 'all' ? 'including subagents' : 'main thread only'}
            </button>
          </div>
        </div>

        <nav className="mx-auto flex max-w-[1600px] gap-1 overflow-x-auto px-6">
          {(tabs.data ?? []).map((entry) => (
            <button
              key={entry.id}
              onClick={() => setTab(entry.id)}
              className={`-mb-px border-b-2 px-3 py-2 text-[13px] whitespace-nowrap
                          transition-colors ${
                            tab === entry.id
                              ? 'border-accent text-ink'
                              : 'border-transparent text-ink-dim hover:text-ink'
                          }`}
            >
              {entry.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="mx-auto w-full max-w-[1600px] flex-1 px-6 py-5">
        {pane.isError && <Failure error={pane.error} />}
        {!pane.data && !pane.isError && <Waiting />}
        {pane.data && (
          // `data-tab` names which tab these numbers belong to, in the DOM. It is what lets a sweep
          // wait for the pane it asked for instead of measuring whatever is still on screen, which
          // is how the wrong-numbers-under-the-right-label defect above was found in the first
          // place, and then nearly missed a second time.
          <div
            data-tab={pane.data.tab}
            data-loading={pane.isFetching ? 'true' : 'false'}
            className={pane.isFetching ? 'opacity-60 transition-opacity' : undefined}
          >
            <Pane payload={pane.data} />
          </div>
        )}
      </main>

      <footer className="border-t border-edge px-6 py-2 text-[11px] text-ink-faint">
        {health.data?.read_only && 'read only, this server never writes to the store. '}
        {health.data?.cache && (
          <>
            cache {health.data.cache.hits} hit / {health.data.cache.misses} miss,{' '}
            {Math.round((health.data.cache.bytes ?? 0) / 1024)} KB
          </>
        )}
      </footer>
    </div>
  )
}

function Picker({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  options: { value: string; label: string }[]
}) {
  return (
    <label className="flex items-center gap-1.5 text-[11px] text-ink-faint">
      {label}
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="max-w-[22rem] rounded-md border border-edge bg-panel px-2 py-1.5 text-[12px]
                   text-ink outline-none focus:border-accent"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}

function Waiting() {
  return (
    <div className="flex flex-col gap-5">
      {[380, 200, 200].map((height, index) => (
        <div
          key={index}
          className="animate-pulse rounded-lg border border-edge bg-panel"
          style={{ height }}
        />
      ))}
    </div>
  )
}

/** The error says what failed and what to do, because "Failed to fetch" says neither. */
function Failure({ error }: { error: unknown }) {
  const isApi = error instanceof ApiError
  return (
    <div className="rounded-lg border border-bad/40 bg-bad/5 p-5">
      <h2 className="mb-1 text-[14px] font-semibold text-bad">This tab did not load</h2>
      <p className="font-mono text-[12px] text-ink-dim">
        {error instanceof Error ? error.message : String(error)}
      </p>
      {!isApi && (
        <p className="mt-3 text-[12.5px] text-ink-dim">
          Nothing answered on <code className="text-ink">/api</code>. Start the server with{' '}
          <code className="text-ink">python -m c4x.api</code>.
        </p>
      )}
      {isApi && Boolean(error.detail) && (
        <pre className="mt-3 overflow-auto text-[11.5px] text-ink-faint">
          {JSON.stringify(error.detail, null, 2)}
        </pre>
      )}
    </div>
  )
}
