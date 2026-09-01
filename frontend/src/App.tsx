import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type Selection } from '@/api'
import { Pane } from '@/components/Pane'
import { Palette, type Choice } from '@/components/Palette'
import { CompareArms } from '@/components/CompareArms'
import { ProjectMoves } from '@/components/ProjectMoves'
import { Sidebar, useCollapsed } from '@/components/Sidebar'

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
  const [palette, setPalette] = useState(false)
  const [collapsed, setCollapsed] = useCollapsed()

  const client = useQueryClient()
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
    // EVERY FIELD `api.tab` SENDS. A key that omits one caches two different requests under one
    // entry, so the arm-B picker would move and the pane would keep showing the previous answer,
    // with nothing on screen saying it was stale.
    queryKey: ['tab', tab, selection.session, selection.scope, selection.cohort,
               selection.compareWith, selection.compareKind],
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

  // Cmd/Ctrl+K anywhere. Registered here rather than inside the palette so the shortcut works
  // when the palette is closed, which is the only time it matters.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPalette((was) => !was)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  const pick = (choice: Choice) => {
    if (choice.kind === 'tab') setTab(choice.id)
    else if (choice.kind === 'population') {
      setSelection((was) => ({ ...was, cohort: choice.id === '__all__' ? null : choice.id }))
    } else setSelection((was) => ({ ...was, session: choice.id }))
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
    <div className="flex min-h-full">
      <Sidebar
        tabs={tabs.data ?? []}
        active={tab}
        onPick={setTab}
        collapsed={collapsed}
        onToggle={() => setCollapsed(!collapsed)}
      />

      <div className="flex min-w-0 flex-1 flex-col">
      <header className="sticky top-0 z-20 border-b border-edge/60 bg-page/85 backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-3 px-6 py-3">
          {/* A keyboard shortcut nobody is told about does not exist. */}
          <button
            onClick={() => setPalette(true)}
            aria-label="Search tabs, populations and sessions"
            className="flex items-center gap-2 rounded-md bg-panel px-2.5 py-1.5 text-xs
                       text-ink-faint shadow-panel transition-colors duration-150
                       hover:text-ink-dim"
          >
            <span>Search</span>
            <kbd className="rounded bg-page px-1.5 py-0.5 text-2xs text-ink-faint">
              Ctrl K
            </kbd>
          </button>

          {/*
            WHAT THIS TAB IS DESCRIBING, in three words, next to the controls that change it.

            The app produces a sentence for this and it used to be printed across the top of every
            pane, where it read as one more grey paragraph: on Diagnostics the sentence "Store-wide.
            Not affected by the header selection." sat above a table somebody was asking why it
            never changed. It is a property of the current view, not content, so it belongs beside
            the selection controls. The full sentence is the tooltip.
          */}
          {pane.data?.population && (
            // FROM WHAT THE TAB IS DESCRIBING, not from whether it COULD respond to a selection.
            // Those are different facts and the chip used the wrong one: with nothing selected,
            // Compactions, Window, Cost and Compare each described the whole store while the chip
            // said "This Selection". `scoped` stays on the element for anything measuring which
            // tabs answer to the header.
            (() => {
              const onSelection = (pane.data.population_scope ?? 'store') === 'selection'
              return (
                <span
                  data-scoped={pane.data.scoped ? 'true' : 'false'}
                  data-population={pane.data.population_scope ?? 'store'}
                  title={pane.data.population}
                  className={`rounded-md px-2 py-1 text-2xs ${
                    onSelection ? 'bg-panel text-ink-faint' : 'bg-warn/10 text-warn'
                  }`}
                >
                  {onSelection ? 'This Selection' : 'Store-Wide'}
                </span>
              )
            })()
          )}

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
                           px-2.5 py-1.5 text-sm text-accent"
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
              label="Population"
              value={selection.cohort ?? ''}
              onChange={(value) => setSelection((was) => ({ ...was, cohort: value || null }))}
              // Labels and values straight through. Taking a value apart to prettify it is how the
              // filter broke the first time.
              options={[
                { value: '', label: 'No restriction' },
                ...(cohorts.data ?? []).map((c) => ({ value: c.value, label: c.label })),
              ]}
            />
            <ProjectMoves
              cohort={selection.cohort ?? null}
              cohorts={cohorts.data ?? []}
              // Two facts, kept apart. `read_only` means this server never harvests and is always
              // true; `writes_enabled` is whether these three routes answer. Reading the wrong one
              // would disable the controls on every server.
              writesEnabled={health.data?.writes_enabled ?? false}
              // A delete or an import changes what every pane describes, and the cohort list
              // itself: a deleted project must stop appearing under Population. Invalidate rather
              // than reload, so the open tab refetches and nothing else is thrown away.
              onChanged={() => {
                void client.invalidateQueries()
                setSelection((was) => ({ ...was, session: null }))
              }}
            />
            <button
              onClick={() =>
                setSelection((was) => ({ ...was, scope: was.scope === 'all' ? 'main' : 'all' }))
              }
              title="Subagent turns run inside a session but are not part of its own context."
              className={`rounded-md border px-2.5 py-1.5 text-sm transition-colors ${
                selection.scope === 'all'
                  ? 'border-accent/60 bg-accent/10 text-accent'
                  : 'border-edge bg-panel text-ink-dim hover:text-ink'
              }`}
            >
              {selection.scope === 'all' ? 'Including Subagents' : 'Main Thread Only'}
            </button>
            <button
              onClick={() => setLive((was) => !was)}
              title="Re-read this tab every five seconds. Useful while a session is still running."
              className={`rounded-md border px-2.5 py-1.5 text-sm transition-colors ${
                live
                  ? 'border-good/60 bg-good/10 text-good'
                  : 'border-edge bg-panel text-ink-dim hover:text-ink'
              }`}
            >
              {live ? 'Live' : 'Paused'}
            </button>
          </div>
        </div>

      </header>

      <main className="mx-auto w-full max-w-[1600px] flex-1 px-6 py-5">
        {/*
          Compare is the one tab whose question needs TWO selections, and it is the only place a
          chat picker belongs. The header has none on purpose: nobody recalls a session by name out
          of 317. Here the reader already knows which two they mean.
        */}
        {tab === 'tab-compare' && (
          <CompareArms
            selection={selection}
            sessions={sessions.data ?? []}
            cohorts={cohorts.data ?? []}
            onChange={(next) => setSelection((was) => ({ ...was, ...next }))}
          />
        )}
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

      <Palette
        open={palette}
        onClose={() => setPalette(false)}
        tabs={tabs.data ?? []}
        cohorts={cohorts.data ?? []}
        sessions={sessions.data ?? []}
        onPick={pick}
      />

      <footer className="border-t border-edge px-6 py-2 text-xs text-ink-faint">
        {health.data?.read_only && 'read only, this server never writes to the store. '}
        {health.data?.cache && (
          <>
            cache {health.data.cache.hits} hit / {health.data.cache.misses} miss,{' '}
            {Math.round((health.data.cache.bytes ?? 0) / 1024)} KB
          </>
        )}
      </footer>
      </div>
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
    <label className="flex items-center gap-1.5 text-xs text-ink-faint">
      {label}
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="max-w-[22rem] rounded-md border border-edge bg-panel px-2 py-1.5 text-sm
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
          className="animate-pulse rounded-lg bg-panel shadow-panel"
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
      <h2 className="mb-1 text-md font-semibold text-bad">This tab did not load</h2>
      <p className="font-mono text-sm text-ink-dim">
        {error instanceof Error ? error.message : String(error)}
      </p>
      {!isApi && (
        <p className="mt-3 text-sm text-ink-dim">
          Nothing answered on <code className="text-ink">/api</code>. Start the server with{' '}
          <code className="text-ink">python -m c4x.api</code>.
        </p>
      )}
      {isApi && Boolean(error.detail) && (
        <pre className="mt-3 overflow-auto text-xs text-ink-faint">
          {JSON.stringify(error.detail, null, 2)}
        </pre>
      )}
    </div>
  )
}
