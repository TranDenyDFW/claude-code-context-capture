import { useEffect, useState } from 'react'
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
  const [live, setLive] = useState(false)

  const tabs = useQuery({ queryKey: ['tabs'], queryFn: api.tabs })
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30_000 })
  // NARROWED TO THE COHORT, by the server. Three things were wrong when this was built here:
  // it capped at 300 of 317 so seventeen sessions could not be picked, it ignored the cohort
  // so the picker offered sessions the chosen population excludes, and it sorted by recency
  // where the app sorts by project path then title so the list reads grouped by project.
  const sessions = useQuery({
    queryKey: ['selector', selection.cohort],
    queryFn: () => api.selector(selection.cohort),
  })

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
    // OFF BY DEFAULT, and that is a change from the dashboard, which ticks every five seconds
    // whether anyone is looking or not. Three of these tabs cost over a second of SQL to build, so
    // an always-on tick means a background window spending a second of the machine every five
    // seconds forever, and the store it is reading is one this same machine is writing. A reader
    // watching a session fill up turns it on; everyone else is reading history, which does not
    // move. The cache means an unchanged store answers the tick in about 3 ms.
    refetchInterval: live ? 5_000 : false,
    // NO `placeholderData: previous`. It was here, and it meant that switching from Window to Cost
    // showed WINDOW's tables and charts under the heading "Cost" for about a second: the right
    // label over the wrong numbers, which is the single worst thing a tool like this can display.
    // Dimming it did not help, because a dimmed wrong number is still a wrong number. Caught by
    // sweeping the tabs and comparing what the page showed against what the API returned.
    //
    // Nothing is lost by removing it. React Query still caches per key, so returning to a tab
    // already visited is instant; only a tab being seen for the first time shows the skeleton.
  })

  /**
   * Clicking a row that identifies a session selects it, everywhere.
   *
   * Driven by the ROW, not by which tab is open. Any table with a `session_id` column becomes a way
   * to navigate, which is how the All sessions and Compactions tables behave in the dashboard, and
   * it also picks up every other table that happens to carry one without needing a list here that
   * would go stale. A row with no session id is not clickable, and `DataTable` only shows the hand
   * cursor when there is something to click.
   */
  const selectFromRow = (row: Record<string, unknown>) => {
    const found = row.session_id ?? row.session
    if (typeof found !== 'string' || !found) return
    setSelection((was) => ({ ...was, session: found }))
  }

  // FROM THE SERVER, not derived here. The previous version built this out of every distinct
  // working directory in the store, which is not what this app means by a project: it filled the
  // picker with per-run temp directories, dropped the four section cohorts, and sent a bare path
  // that `cohort_sessions()` does not recognise, so selecting one filtered nothing at all.
  const cohorts = useQuery({ queryKey: ['cohorts'], queryFn: api.cohorts })

  // The selected session, named the way the app names it. The chip leads with WHEN it ran, because
  // that is how a session is actually found: `selector_options` builds its label as
  // "project · title · when", so the last part is the date and time.
  const selected = (sessions.data ?? []).find((o) => o.value === selection.session)
  const parts = selected?.label.split('·').map((p) => p.trim()) ?? []
  const selectedWhen = parts.length >= 3
    ? `${parts[parts.length - 1]}  ·  ${parts[0]}`
    : (selected?.label ?? `${selection.session?.slice(0, 8)}…`)

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 border-b border-edge bg-page/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-3 px-6 py-3">
          <span className="text-[15px] font-semibold tracking-tight text-ink">C4X</span>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            {/*
              NO SESSION DROPDOWN. Nobody remembers a session by name, which is what a list of 317
              titles asks of a reader. A session is found by WHEN it ran: sort or filter any table
              by Date & Time and click the row.

              What stays is a chip saying which one is selected and letting you drop it, because a
              selection you cannot see or undo is worse than no selection at all.
            */}
            {selection.session && (
              <span
                className="flex items-center gap-2 rounded-md border border-accent/60 bg-accent/10
                           px-2.5 py-1.5 text-[12px] text-accent"
                title={selected?.label ?? selection.session}
              >
                <span className="max-w-[26rem] truncate">{selectedWhen}</span>
                <button
                  onClick={() => setSelection((was) => ({ ...was, session: null }))}
                  title="Clear the selected session"
                  className="text-accent/70 hover:text-accent"
                >
                  ×
                </button>
              </span>
            )}
            <Picker
              label="population"
              value={selection.cohort ?? ''}
              onChange={(value) => setSelection((was) => ({ ...was, cohort: value || null }))}
              // Labels and values straight through. Taking a value apart to prettify it is how the
              // filter broke the first time.
              options={[
                { value: '', label: 'no restriction' },
                ...(cohorts.data ?? []).map((c) => ({ value: c.value, label: c.label })),
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
            <button
              onClick={() => setLive((was) => !was)}
              title="Re-read this tab every five seconds. Useful while a session is still running."
              className={`rounded-md border px-2.5 py-1.5 text-[12px] transition-colors ${
                live
                  ? 'border-good/60 bg-good/10 text-good'
                  : 'border-edge bg-panel text-ink-dim hover:text-ink'
              }`}
            >
              {live ? 'live' : 'paused'}
            </button>
          </div>
        </div>

        <nav className="mx-auto flex max-w-[1600px] gap-1 overflow-x-auto px-6">
          {(tabs.data ?? []).map((entry) => (
            <button
              key={entry.id}
              // `btn-<tab id>`, the same id the Dash page used. `tools/screenshots.py` selects on
              // it to regenerate the README's images, and keeping the name means that tool did not
              // have to learn a second convention when the page underneath it changed.
              id={`btn-${entry.id}`}
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
            <Pane payload={pane.data} onRowClick={selectFromRow} />
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
