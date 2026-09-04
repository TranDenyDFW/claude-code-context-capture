import { useEffect, useMemo, useState } from 'react'
import type { Band, ColumnMeta, Table, TableMeta } from '@/api'
import { TableToolbar } from './TableToolbar'
import type { Sheet } from './exporters'

/**
 * One table, rendered from what the APP declares about it rather than from what the values happen
 * to look like at runtime.
 *
 * Everything below that matters was got wrong once by guessing:
 *
 * - NUMBER FORMAT comes from the column's d3 specifier. Guessing from the value with
 *   `Number.isInteger` rendered a column declared to one decimal as 43.30, 2.40, 1.20 and then a
 *   bare 1. Three precisions in one column, and the bare 1 reads as a different quantity.
 * - HEADER ALIGNMENT follows the cells. Right-aligning numbers and left-aligning their headings
 *   is neither of the two consistent choices.
 * - COLUMN NAMES come from `column_label()`. The raw ids are schema, not English.
 * - BLANK STAYS BLANK. An unpriced model has an unknown cost, not a zero one, and a 0 there would
 *   look more authoritative than the truth.
 *
 * The state (filters, sort, hidden columns, order, page) is deliberately NOT in a table library.
 * It is six `useState` values over an array already in memory: the largest table here is 317 rows,
 * so sorting and filtering it is microseconds, and a headless library would add a dependency and an
 * abstraction to own what fits on one screen.
 */

function isNumeric(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/**
 * The d3 specifier subset this app actually uses: `,` for grouped thousands and `.Nf` for a fixed
 * number of decimals, in either order (`,.2f`).
 *
 * A subset ON PURPOSE. Pulling in a full d3-format for three specifiers would add a dependency to
 * interpret strings this codebase produces from one function, `numeric_columns()`. If a specifier
 * arrives that this cannot read, the number is rendered plainly rather than wrongly.
 */
function applySpecifier(value: number, specifier: string | null): string {
  if (!specifier) {
    return Number.isInteger(value) ? value.toLocaleString() : String(value)
  }
  const group = specifier.includes(',')
  const fixed = /\.(\d+)f/.exec(specifier)
  if (fixed) {
    const digits = Number(fixed[1])
    return value.toLocaleString(undefined, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
      useGrouping: group,
    })
  }
  if (group) return value.toLocaleString(undefined, { maximumFractionDigits: 20 })
  return String(value)
}

function show(value: unknown, meta?: ColumnMeta): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return ''
    return applySpecifier(value, meta?.specifier ?? null)
  }
  return String(value)
}

/** The LAST matching band wins: rules run shallowest to deepest and a top value matches them all. */
function shadeFor(value: unknown, bands: Band[] | undefined): Band | null {
  if (!bands?.length || !isNumeric(value)) return null
  let hit: Band | null = null
  for (const band of bands) {
    if (band.op === '>=' ? value >= band.at : value <= band.at) hit = band
  }
  return hit
}

export function DataTable({
  table,
  meta,
  title,
  onRowClick,
  onOpenWindow,
  initialQuery,
  onQueryChange,
  onOpenRow,
}: {
  table: Table
  meta?: TableMeta
  title?: string
  onRowClick?: (row: Record<string, unknown>) => void
  /** Open a row in the inspector: its fields, and its full text where a column was cut. */
  onOpenRow?: (row: Record<string, unknown>) => void
  /** Open this table in a window of its own; the toolbar shows the control when given. */
  onOpenWindow?: () => void
  /** What the search box starts with, so a link can point at the rows behind a click. */
  initialQuery?: string
  /** Reports the search box as it changes, so a page that owns the address can publish it. */
  onQueryChange?: (query: string) => void
}) {
  const [sort, setSort] = useState<{ column: string; direction: 1 | -1 } | null>(null)
  const [query, setQuery] = useState(initialQuery ?? '')
  // FOLLOWS THE PROP, not only its first value. A mount-time seed meant a link opened in a window
  // already showing this table kept the old filter, and the address described a screen nobody had.
  useEffect(() => { setQuery(initialQuery ?? '') }, [initialQuery])
  const [columnQuery, setColumnQuery] = useState<Record<string, string>>({})
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [order, setOrder] = useState<string[] | null>(null)
  const [dragging, setDragging] = useState<string | null>(null)
  const [pageSize, setPageSize] = useState(meta?.page_size ?? 25)
  const [page, setPage] = useState(0)

  const columns = useMemo<ColumnMeta[]>(() => {
    // The server's metadata is the source. A column with none still renders, described as plainly
    // as possible, rather than disappearing because nobody declared it.
    const byId = new Map((meta?.columns ?? []).map((c) => [c.id, c]))
    const base = table.columns.map<ColumnMeta>((id) => byId.get(id) ?? {
      id, label: id, numeric: false, specifier: null, align: 'left', hidden: false, bands: [],
    })
    if (!order) return base
    const known = new Map(base.map((c) => [c.id, c]))
    // The reader's arrangement first, with anything they have not touched left where it was.
    const moved = order.map((id) => known.get(id)).filter(Boolean) as ColumnMeta[]
    return [...moved, ...base.filter((c) => !order.includes(c.id))]
  }, [table.columns, meta, order])

  const visible = useMemo(() => columns.filter((c) => !hidden.has(c.id)), [columns, hidden])

  // FILTERED FIRST, then sorted, then paged. Filtering the visible page instead of the whole table
  // would search the rows that happen to be on screen and report nothing for a value three hundred
  // rows down, which looks exactly like an empty result.
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const perColumn = Object.entries(columnQuery)
      .map(([id, text]) => [id, text.trim().toLowerCase()] as const)
      .filter(([, text]) => text)
    if (!needle && !perColumn.length) return table.rows
    const byId = new Map(columns.map((c) => [c.id, c]))
    return table.rows.filter((row) => {
      for (const [id, text] of perColumn) {
        if (!show(row[id], byId.get(id)).toLowerCase().includes(text)) return false
      }
      if (!needle) return true
      // Matched against the TEXT ON SCREEN, so typing what you can see finds the row. Searching the
      // raw value would fail on "1,024" and on a date the reader is looking at. Only VISIBLE
      // columns, so hiding a column also removes it from the search, which is what hiding means.
      return visible.some((c) => show(row[c.id], c).toLowerCase().includes(needle))
    })
  }, [table.rows, columns, visible, query, columnQuery])

  const sorted = useMemo(() => {
    if (!sort) return filtered
    const copy = [...filtered]
    copy.sort((a, b) => {
      const left = a[sort.column]
      const right = b[sort.column]
      // Blanks sort last in both directions: a column of unknowns should not push the rows you can
      // read off the top of the table when you sort by it.
      if (left === null || left === undefined) return 1
      if (right === null || right === undefined) return -1
      if (isNumeric(left) && isNumeric(right)) return (left - right) * sort.direction
      return String(left).localeCompare(String(right)) * sort.direction
    })
    return copy
  }, [filtered, sort])

  // Back to the first page whenever the result set changes under it, or a filter leaves the reader
  // on page 7 of a 2-page result looking at nothing.
  useEffect(() => setPage(0), [query, columnQuery, pageSize, sort])

  const pages = pageSize < 0 ? 1 : Math.max(1, Math.ceil(sorted.length / pageSize))
  const atPage = Math.min(page, pages - 1)
  const rows = pageSize < 0
    ? sorted
    : sorted.slice(atPage * pageSize, atPage * pageSize + pageSize)

  const sheet: Sheet = {
    // Exports what is ON SCREEN: the visible columns, in the reader's order, filtered and sorted,
    // and every matching row rather than the current page. Exporting the raw payload instead would
    // quietly undo the work they just did to narrow it.
    columns: visible,
    rows: sorted,
    name: title || table.id || 'table',
    format: (value, column) => show(value, column),
    fullText: meta?.full_text,
  }

  if (!table.columns.length) return null

  // ONE CLICK, ONE MEANING, and the reader wins. A table whose rows carry a full text says so in
  // its own note ("click a row to read it in full"), so that click must read; a table that only
  // identifies a session navigates. A table with both would have lost its reader silently under
  // the previous rule, which read row 0 alone.
  //
  // `session_id` is a HIDDEN column on the sessions table: in every row, absent from the column
  // list, which is how the identifier travels without showing a uuid. Reading `columns` here made
  // the feature do nothing on the one table it exists for. Read across rows, not just the first:
  // a store can hand back a first row missing a key that every other row has.
  const reads = Boolean(onOpenRow) && Boolean(meta?.full_text)
  const identifies = table.rows.some((row) => 'session_id' in row || 'session' in row)
  const navigable = Boolean(onRowClick) && !reads && identifies
  const openable = Boolean(onOpenRow) && !navigable

  const filtering = Boolean(query.trim() || Object.values(columnQuery).some((v) => v.trim()))

  return (
    <div className="overflow-hidden rounded-lg bg-panel shadow-panel">
      <TableToolbar
        sheet={sheet}
        onOpenWindow={onOpenWindow}
        allColumns={columns}
        hidden={hidden}
        onToggleColumn={(id) =>
          setHidden((was) => {
            const next = new Set(was)
            if (next.has(id)) next.delete(id)
            else next.add(id)
            return next
          })
        }
        onShowAll={() => setHidden(new Set())}
        onHideAll={() => setHidden(new Set(columns.map((c) => c.id)))}
        onHideEmpty={() =>
          setHidden(new Set(columns
            .filter((c) => table.rows.every((row) => show(row[c.id], c).trim() === ''))
            .map((c) => c.id)))
        }
        pageSize={pageSize}
        onPageSize={setPageSize}
      >
        <input
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
            // The page that owns the address publishes it, so a copied link points at the rows
            // on screen rather than at the ones the link happened to open with.
            onQueryChange?.(event.target.value)
          }}
          // Just "Filter". It used to say "Filter 5 rows" while the footer said "5 rows" two
          // inches below, which is the same number twice.
          placeholder="Filter"
          aria-label={`Filter ${sheet.name}`}
          className="w-56 rounded border border-edge bg-page px-2 py-1 text-xs text-ink
                     outline-none placeholder:text-ink-faint focus:border-accent"
        />
        {filtering && (
          <>
            <button
              onClick={() => { setQuery(''); onQueryChange?.(''); setColumnQuery({}) }}
              className="rounded border border-edge px-2 py-1 text-2xs text-ink-dim hover:text-ink"
            >
              Clear
            </button>
            {/* One string, not three interpolations: split across text nodes it reads the same on
                screen and cannot be found by anything asserting on it. */}
            <span className="text-2xs text-ink-faint">
              {`${sorted.length.toLocaleString()} of ${table.rows.length.toLocaleString()} match`}
            </span>
          </>
        )}
      </TableToolbar>

      <div className="max-h-[32rem] overflow-auto">
        <table className="w-full border-collapse text-sm">
          <thead className="sticky top-0 z-10 bg-panel-raised">
            <tr>
              {visible.map((column) => {
                const help = table.tooltips?.[column.id]
                const active = sort?.column === column.id
                return (
                  <th
                    key={column.id}
                    // Dragged to reorder. The reference calls this ColReorder; here it is four
                    // native drag handlers, because the browser implements the hard part already.
                    draggable
                    onDragStart={() => setDragging(column.id)}
                    onDragOver={(event) => event.preventDefault()}
                    onDrop={() => {
                      if (!dragging || dragging === column.id) return
                      const ids = columns.map((c) => c.id)
                      const from = ids.indexOf(dragging)
                      const to = ids.indexOf(column.id)
                      if (from < 0 || to < 0) return
                      ids.splice(to, 0, ids.splice(from, 1)[0])
                      setOrder(ids)
                      setDragging(null)
                    }}
                    title={help}
                    onClick={() =>
                      setSort((was) =>
                        was?.column === column.id
                          ? { column: column.id, direction: was.direction === 1 ? -1 : 1 }
                          : { column: column.id, direction: -1 },
                      )
                    }
                    className={`cursor-pointer border-b border-edge px-3 py-2 font-semibold
                                whitespace-nowrap text-ink-dim select-none hover:text-ink
                                ${column.align === 'right' ? 'text-right' : 'text-left'}`}
                  >
                    <span className={help ? 'has-help' : undefined}>{column.label}</span>
                    {active && (
                      <span className="ml-1 text-accent">{sort.direction === 1 ? '↑' : '↓'}</span>
                    )}
                  </th>
                )
              })}
            </tr>
            <tr>
              {visible.map((column) => (
                <th key={column.id} className="border-b border-edge/60 px-2 pb-1.5">
                  <input
                    value={columnQuery[column.id] ?? ''}
                    onChange={(event) =>
                      setColumnQuery((was) => ({ ...was, [column.id]: event.target.value }))
                    }
                    // No placeholder: a row of eight boxes each saying "Filter" is noise, and the
                    // accessible name says what this one filters.
                    aria-label={`Filter by ${column.label}`}
                    className="w-full rounded border border-edge/60 bg-page px-1.5 py-0.5 text-2xs
                               font-normal text-ink outline-none focus:border-accent"
                  />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr
                key={index}
                onClick={navigable ? () => onRowClick!(row)
                  : openable ? () => onOpenRow!(row) : undefined}
                title={navigable ? 'select this session'
                  : openable ? 'open this row: every field, and the full text' : undefined}
                className={`border-b border-edge/40 last:border-0 hover:bg-panel-raised
                            ${navigable || openable ? 'cursor-pointer' : ''}`}
              >
                {visible.map((column) => {
                  const value = row[column.id]
                  const band = shadeFor(value, column.bands)
                  const text = show(value, column)
                  return (
                    <td
                      key={column.id}
                      style={band ? { backgroundColor: band.background } : undefined}
                      // The full value as a tooltip, so a truncated cell is still readable. The
                      // reference does the same and calls it cheap and reliable.
                      title={text || undefined}
                      className={`px-3 py-1.5 whitespace-nowrap ${
                        column.align === 'right' ? 'text-right font-mono tabular-nums' : ''
                      } ${value === null || value === undefined ? 'text-ink-faint' : ''}`}
                    >
                      {text}
                    </td>
                  )
                })}
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={Math.max(1, visible.length)}
                  className="px-3 py-6 text-center text-xs text-ink-dim"
                >
                  {filtering ? 'Nothing matches that filter.' : 'This table has no rows.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-edge/60 px-3 py-1.5
                      text-2xs text-ink-faint">
        <span>
          {pageSize < 0 || sorted.length === 0
            ? `${sorted.length.toLocaleString()} ${sorted.length === 1 ? 'row' : 'rows'}`
            : `${(atPage * pageSize + 1).toLocaleString()} to ` +
              `${Math.min((atPage + 1) * pageSize, sorted.length).toLocaleString()} of ` +
              `${sorted.length.toLocaleString()}`}
        </span>
        {pages > 1 && (
          <span className="ml-auto flex items-center gap-1">
            {/* First and last as well as step, which is what `pagingType: full_numbers` gives and
                what a 300-row table needs: stepping to the end one page at a time is not paging. */}
            {[
              { label: 'First', to: 0, off: atPage === 0 },
              { label: 'Prev', to: atPage - 1, off: atPage === 0 },
              { label: 'Next', to: atPage + 1, off: atPage >= pages - 1, after: true },
              { label: 'Last', to: pages - 1, off: atPage >= pages - 1, after: true },
            ].map((b) => (
              <span key={b.label} className="contents">
                {b.label === 'Next' && <span className="px-1">{`Page ${atPage + 1} of ${pages}`}</span>}
                <button
                  onClick={() => setPage(b.to)}
                  disabled={b.off}
                  className="rounded border border-edge px-1.5 py-0.5 transition-colors
                             duration-150 hover:text-ink disabled:opacity-40"
                >
                  {b.label}
                </button>
              </span>
            ))}
          </span>
        )}
      </div>
    </div>
  )
}

export { applySpecifier, shadeFor }
