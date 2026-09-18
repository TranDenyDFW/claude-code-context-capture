import { useEffect, useMemo, useState } from 'react'
import type { Band, ColumnMeta, Table, TableMeta } from '@/api'
import { ResizeHandle } from './ResizeHandle'
import { useColumnWidths } from './useColumnWidths'
import { useFullText } from './useFullText'
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
  // SEEDED FROM THE SERVER, which declares `hidden` per column and was being ignored. The payload
  // marks the Findings table's `session_id` and `goes to` hidden:true, this started empty, and so
  // both rendered: the declaration meant nothing and every future table would meet the same bug.
  //
  // A READER CAN STILL UNHIDE THEM from the Columns menu. This is a default, not a lock, which is
  // the difference between a hidden column and a dropped one: `session_id` is how a row click
  // knows which session it points at, so it has to travel even when it is not shown.
  const [hidden, setHidden] = useState<Set<string>>(
    () => new Set((meta?.columns ?? []).filter((c) => c.hidden).map((c) => c.id)))
  const [order, setOrder] = useState<string[] | null>(null)
  const [dragging, setDragging] = useState<string | null>(null)
  const [pageSize, setPageSize] = useState(meta?.page_size ?? 25)
  const [page, setPage] = useState(0)
  // Closed until asked for. The query used to be a collapsible printed under every table; it is
  // the same content, moved to where a reader goes when they want something FROM the table.
  const [showQuery, setShowQuery] = useState(false)

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

  // AN EDGE BEING DRAGGED, which the header cells read to switch their reorder off for the moment.
  const [resizing, setResizing] = useState(false)
  // EVERY ROW, NEVER THE PAGE. This is the whole of the fix for "do not resize columns when
  // sorting": the widths are derived from data the sort cannot change.
  const widths = useColumnWidths({
    // A table with no id of its own (the Cost tab draws several) is keyed by its columns alone,
    // which is what the signature already carries.
    tableId: meta?.id ?? table.id ?? 'table',
    columns: visible,
    rows: table.rows,
    format: show,
  })

  // FILTERED FIRST, then sorted, then paged. Filtering the visible page instead of the whole table
  // would search the rows that happen to be on screen and report nothing for a value three hundred
  // rows down, which looks exactly like an empty result.
  // THE WHOLE OF A CUT COLUMN, fetched the moment somebody actually searches. The Messages table
  // ships the first 220 characters of each message; measured on this store, 205,775 of 330,857
  // messages are longer than that and the mean is 2,313 characters, so the box searched about a
  // tenth of the average message and reported "no match" for the other nine. Nothing is fetched
  // for a reader who never types.
  const searching = Boolean(query.trim())
  const full = useFullText(table.rows, meta, searching)

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
      //
      // EXCEPT WHERE THE SERVER HAD TO CUT ONE. There the text on screen is a fifth of a sentence
      // and matching only that is what made the box report nothing for words that are plainly in
      // the message. The full value is searched and the preview stays on screen, so the rule above
      // still holds for every column that was never cut.
      return visible.some((c) => {
        if (c.id === full.column) {
          const whole = full.text.get(String(row[meta?.full_text?.key ?? ""] ?? ""))
          if (typeof whole === "string") return whole.toLowerCase().includes(needle)
        }
        return show(row[c.id], c).toLowerCase().includes(needle)
      })
    })
  }, [table.rows, columns, visible, query, columnQuery, full, meta])

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
  // A ROW THAT CAN BE READ beats a row that navigates, and `detail` is a second way to be
  // readable: the compactions table carries no full text of its own, it carries a POINTER to
  // the summary that replaced the dropped context. Without it here, a table that both
  // identifies a session and points at a document would navigate instead of opening.
  const reads = Boolean(onOpenRow) && Boolean(meta?.full_text || meta?.detail)
  const identifies = table.rows.some((row) => 'session_id' in row || 'session' in row)
  const navigable = Boolean(onRowClick) && !reads && identifies
  const openable = Boolean(onOpenRow) && !navigable
  // A DOCUMENT REACHABLE FROM THE ROW, WITHOUT TAKING THE CLICK. Deliberately outside `reads`, so
  // `navigable` is untouched: on the Sessions list a click still selects the session, and this is
  // a control in a column of its own. Folding it into `reads` would have changed what the main
  // list does on every click, which is the interaction the whole dashboard is built on.
  const inspects = Boolean(onOpenRow) && Boolean(meta?.row_detail)

  const filtering = Boolean(query.trim() || Object.values(columnQuery).some((v) => v.trim()))

  return (
    <div className="overflow-hidden rounded-lg bg-panel shadow-panel">
      <TableToolbar
        sheet={sheet}
        onOpenWindow={onOpenWindow}
        hasQuery={Boolean(meta?.query)}
        queryOpen={showQuery}
        onToggleQuery={() => setShowQuery((was) => !was)}
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
        onResetWidths={() => widths.reset()}
        widthsChanged={widths.resized}
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
            {/* WHAT THE SEARCH CAN SEE RIGHT NOW, said out loud while it is not the whole thing.
                An under-reporting search is indistinguishable from an absent word, so the two
                states where it under-reports are the two states that have to be legible: the
                fetch is still in flight, or it failed and the previews are all there is. Nothing
                is said in the ordinary case, because a label on every search would be noise. */}
            {full.loading && (
              <span role="status" className="text-2xs text-ink-faint">
                searching previews while the full text loads
              </span>
            )}
            {full.failed && (
              <span role="status" className="text-2xs text-warn">
                the full text could not be loaded, so this searches the previews only
              </span>
            )}
          </>
        )}
      </TableToolbar>

      {/* FULL WIDTH, not a dropdown. A query is wide and a menu panel would wrap it into
          unreadability; this is the one place the SQL is meant to be READ. */}
      {showQuery && meta?.query && (
        <div className="border-b border-edge/60 px-3 py-2">
          <div className="mb-1.5 flex items-center gap-2">
            <span className="text-2xs text-ink-faint">The query that produced this table</span>
            <button
              onClick={() => { void navigator.clipboard?.writeText(meta.query ?? '') }}
              className="rounded border border-edge px-2 py-0.5 text-2xs text-ink-dim
                         hover:text-ink"
            >
              Copy
            </button>
          </div>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-page px-3 py-2
                          font-mono text-xs leading-relaxed text-ink-dim">{meta.query}</pre>
        </div>
      )}

      {/*
        FIXED LAYOUT, AND THE WIDTHS COME FROM THE DATA. With `table-layout: auto` the browser
        measures the cells that happen to be in the DOM, and only one page of rows ever is, so
        every sort re-fitted the columns and the table jumped sideways. `w-full` goes with it: under
        fixed layout a 100% width redistributes any surplus across the columns and quietly
        overrides the colgroup, which is the one thing that has to stay authoritative. The surplus
        is handed to a single column by `layoutWidths` instead.
      */}
      <div ref={widths.containerRef} className="max-h-[32rem] overflow-auto">
        <table className="table-fixed border-collapse text-sm" style={{ width: widths.total }}>
          <colgroup>
            {inspects && <col style={{ width: 32 }} />}
            {visible.map((column) => (
              <col key={column.id} style={{ width: widths.width(column.id) }} />
            ))}
          </colgroup>
          <thead className="sticky top-0 z-10 bg-panel-raised">
            <tr>
              {inspects && (
                <th className="w-8 border-b border-edge px-2 py-2" aria-label="open" />
              )}
              {visible.map((column) => {
                const help = table.tooltips?.[column.id]
                const active = sort?.column === column.id
                return (
                  <th
                    key={column.id}
                    // WHICH WAY THIS COLUMN IS SORTED, for a screen reader. The arrow beside the
                    // label is `aria-hidden`, so without this the sort was visible and nothing else.
                    aria-sort={active ? (sort.direction === 1 ? 'ascending' : 'descending') : 'none'}
                    // Dragged to reorder. The reference calls this ColReorder; here it is four
                    // native drag handlers, because the browser implements the hard part already.
                    // Off while an edge is being dragged, so a resize cannot start a reorder.
                    draggable={!resizing}
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
                    className={`relative cursor-pointer border-b border-edge px-3 py-2
                                font-semibold whitespace-nowrap text-ink-dim select-none
                                hover:text-ink
                                ${column.align === 'right' ? 'text-right' : 'text-left'}`}
                  >
                    <span className={help ? 'has-help' : undefined}>{column.label}</span>
                    {/*
                      THE ARROW'S SPACE IS ALWAYS THERE, empty when the column is not the sorted
                      one. It used to appear on click, which widened that one header by a glyph.
                    */}
                    <span aria-hidden="true" className="ml-1 inline-block w-3 text-accent">
                      {active ? (sort.direction === 1 ? '↑' : '↓') : ''}
                    </span>
                    <ResizeHandle
                      label={`Resize the ${column.label} column`}
                      size={widths.width(column.id)}
                      min={widths.boundsFor(column).min}
                      max={widths.boundsFor(column).max}
                      onSize={(px) => widths.setWidth(column.id, px)}
                      onReset={() => widths.reset(column.id)}
                      onDragChange={setResizing}
                      className="absolute inset-y-0 -right-1 z-20 w-2"
                    />
                  </th>
                )
              })}
            </tr>
            <tr>
              {inspects && <th className="border-b border-edge/60 px-2 pb-1.5" />}
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
                {inspects && (
                  <td className="px-2 py-1.5 align-middle">
                    <button
                      type="button"
                      // STOPS THE CLICK HERE. Without this the row's own handler fires too, so
                      // the session is selected and the whole pane rebuilds underneath the panel
                      // that was just opened.
                      onClick={(event) => { event.stopPropagation(); onOpenRow!(row) }}
                      title={meta?.row_detail?.title
                        ? `open the ${meta.row_detail.title} of this row`
                        : 'open this row'}
                      aria-label={meta?.row_detail?.title ?? 'open this row'}
                      className="rounded border border-edge px-1.5 py-0.5 text-2xs text-ink-faint
                                 transition-colors hover:border-accent hover:text-accent"
                    >
                      ›
                    </button>
                  </td>
                )}
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
                      // EVERY CELL TRUNCATES NOW. Under fixed layout a value wider than its
                      // column is clipped whatever this says, so the choice is an ellipsis or a
                      // hard cut mid-character. The `wide` column keeps its 24rem cap through the
                      // width estimate instead (the server still picks which column that is:
                      // measured across every tab, Messages.preview reaches 220 characters,
                      // Sessions.title 200, Cost.target 161, while Last Active is 19).
                      //
                      // The value is not lost: `title` above carries the whole of it, and an
                      // export reads the rows rather than the cells.
                      className={`truncate px-3 py-1.5 whitespace-nowrap ${
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
                  colSpan={Math.max(1, visible.length + (inspects ? 1 : 0))}
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
