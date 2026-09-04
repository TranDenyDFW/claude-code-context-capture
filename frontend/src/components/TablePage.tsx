import { useEffect } from 'react'
import type { TabPayload } from '@/api'
import { DataTable } from './DataTable'
import { Inspector } from './Inspector'
import { Section } from './Section'
import { TableHeading, joinNotes, tableName } from './TableHeading'
import { useRowInspector } from './useRowInspector'

/**
 * One table of a tab, on a page of its own.
 *
 * Opened from a table's toolbar into a new window, or reached by a link that names the tab, the
 * table and the selection in its address. Everything the table does in the pane it does here:
 * filters, column choices, paging, every export, the query that built it, and the row reader. The
 * first version had neither the reader nor the query, on a page that still printed the note
 * promising the reader; a review found both.
 *
 * NO SESSION NAVIGATION FROM A ROW CLICK. In the pane a row click can change the header selection,
 * which rebuilds every tab. On a window opened for one table there is no header to see it happen:
 * the rows would swap under the reader with nothing saying why. A row opens the drawer instead,
 * and where that row names a session the drawer offers the selection as a labelled button, which
 * is the deliberate version of the same act. Three of this app's tables carry a session key; on
 * the others the button is absent because there is no session to offer.
 */
export function TablePage({
  payload,
  index,
  query,
  filter,
  onBack,
  onQueryChange,
  onSelectSession,
}: {
  payload: TabPayload
  index: number
  /** Seeds the table's search box, and follows it as the reader types. */
  query?: string
  /** An exact row filter from a chart click: a column and the value, so a hidden key still works. */
  filter?: { key: string; value: string } | null
  onBack: () => void
  onQueryChange?: (query: string) => void
  /** Make a row's session the selection, when the reader asks for it in the drawer. */
  onSelectSession?: (row: Record<string, unknown>) => void
}) {
  const table = payload.tables[index]
  const meta = payload.meta?.[index]
  const sections = payload.details ?? []
  const wrapping = sections.find((s) => s.wraps === 'table' && s.wraps_index === index)
  const name = tableName(index, meta, wrapping?.summary)
  // The same note the pane shows, from the same two halves, so a table opened in its own window is
  // not a table that quietly lost its description on the way.
  const note = joinNotes(wrapping?.summary_note, meta?.note)
  const names = payload.tables.map((_, at) => tableName(
    at, payload.meta?.[at], sections.find((s) => s.wraps === 'table' && s.wraps_index === at)?.summary))
  const reader = useRowInspector(payload, names, onSelectSession)

  useEffect(() => {
    const before = document.title
    document.title = `${name} · c4x`
    return () => { document.title = before }
  }, [name])

  if (!table) {
    return (
      <main className="mx-auto w-full max-w-[1600px] px-6 py-5">
        <p role="alert" className="text-sm text-ink-dim">
          This tab has no table {index + 1}; it has {payload.tables.length}.
        </p>
        <button onClick={onBack} className="mt-3 text-sm text-accent hover:underline">
          Back to the dashboard
        </button>
      </main>
    )
  }

  // The exact filter a chart click carried, applied here rather than as a text search: the value
  // may live in a hidden column (a session id does), where a search over what is drawn finds it.
  const filtered = filter
    ? { ...table, rows: table.rows.filter((row) => row[filter.key] === filter.value) }
    : table
  // The collapsible that belongs to this table, which on the Cost tab is the query that built it.
  const attached = sections.filter((s) => (s.wraps ?? 'text') === 'text' && s.table_index === index)

  return (
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-3 px-6 py-5">
      <div className="flex items-baseline justify-between gap-4">
        <TableHeading name={name} table={filtered} note={note} as="h1" />
        <button
          onClick={onBack}
          className="text-sm text-accent hover:underline"
          title="Show the whole tab again in this window"
        >
          Back to the dashboard
        </button>
      </div>
      {payload.population && (
        <p className="text-2xs text-ink-faint">{payload.population}</p>
      )}
      {filter && (
        <p className="text-2xs text-ink-faint">
          Filtered to {filter.key} = {filter.value}, {filtered.rows.length.toLocaleString()} of{' '}
          {table.rows.length.toLocaleString()} rows.
        </p>
      )}
      <DataTable
        table={filtered}
        meta={meta}
        title={name}
        initialQuery={query}
        onQueryChange={onQueryChange}
        onOpenRow={reader.openRow(index)}
      />
      {attached.map((section, at) => (
        <Section key={at} section={section} table={filtered} meta={meta} />
      ))}
      {reader.content && <Inspector content={reader.content} onClose={reader.close} />}
    </main>
  )
}
