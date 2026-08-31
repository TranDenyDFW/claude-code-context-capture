import { useMemo, useState } from 'react'
import type { Band, ColumnMeta, Table, TableMeta } from '@/api'

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
 */

const PAGE = 100

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
  onRowClick,
}: {
  table: Table
  meta?: TableMeta
  onRowClick?: (row: Record<string, unknown>) => void
}) {
  const [sort, setSort] = useState<{ column: string; direction: 1 | -1 } | null>(null)
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(PAGE)

  const byId = useMemo(() => {
    const out: Record<string, ColumnMeta> = {}
    for (const column of meta?.columns ?? []) out[column.id] = column
    return out
  }, [meta])

  // FILTERED FIRST, then sorted, then paged. Filtering the visible page instead of the whole table
  // would search the hundred rows that happen to be on screen and report nothing for a value three
  // hundred rows down, which looks exactly like an empty result.
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return table.rows
    return table.rows.filter((row) =>
      table.columns.some((column) => {
        const value = row[column]
        if (value === null || value === undefined) return false
        // Matched against the TEXT ON SCREEN, so typing what you can see finds the row. Searching
        // the raw value would fail on "1,024" and on a date the reader is looking at.
        return show(value, byId[column]).toLowerCase().includes(needle)
      }),
    )
  }, [table.rows, table.columns, query, byId])

  const rows = useMemo(() => {
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

  if (!table.columns.length) return null

  const visible = rows.slice(0, limit)
  // Only a table that identifies a session can be navigated from. `session_id` is a HIDDEN column
  // on the sessions table: in every row, absent from the column list, which is how the identifier
  // travels without showing a uuid. Reading `columns` here made the feature do nothing on the one
  // table it exists for.
  const navigable = Boolean(onRowClick) && table.rows.length > 0 &&
    ('session_id' in table.rows[0] || 'session' in table.rows[0])

  return (
    <div className="overflow-hidden rounded-lg bg-panel shadow-panel">
      <div className="flex items-center gap-2 border-b border-edge px-3 py-2">
        <input
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
            setLimit(PAGE)
          }}
          placeholder={`Filter ${table.rows.length.toLocaleString()} rows`}
          className="w-56 rounded border border-edge bg-page px-2 py-1 text-sm text-ink
                     outline-none placeholder:text-ink-faint focus:border-accent"
        />
        {query && (
          <>
            <button
              onClick={() => setQuery('')}
              className="rounded border border-edge px-2 py-1 text-xs text-ink-dim
                         hover:text-ink"
            >
              clear
            </button>
            {/* Said out loud. A filtered table that only showed its remaining rows would look
                exactly like a table that never had the others. */}
            {/* One string, not three interpolations: split across text nodes it reads the same on
                screen and cannot be found by anything asserting on it. */}
            <span className="text-xs text-ink-faint">
              {`${rows.length.toLocaleString()} of ${table.rows.length.toLocaleString()} match`}
            </span>
          </>
        )}
      </div>

      <div className="max-h-[32rem] overflow-auto">
        <table className="w-full border-collapse text-sm">
          <thead className="sticky top-0 z-10 bg-panel-raised">
            <tr>
              {table.columns.map((column) => {
                const help = table.tooltips?.[column]
                const info = byId[column]
                const active = sort?.column === column
                return (
                  <th
                    key={column}
                    title={help}
                    onClick={() =>
                      setSort((was) =>
                        was?.column === column
                          ? { column, direction: was.direction === 1 ? -1 : 1 }
                          : { column, direction: -1 },
                      )
                    }
                    className={`cursor-pointer border-b border-edge px-3 py-2 font-semibold
                                whitespace-nowrap text-ink-dim select-none hover:text-ink
                                ${info?.align === 'right' ? 'text-right' : 'text-left'}`}
                  >
                    <span className={help ? 'has-help' : undefined}>{info?.label ?? column}</span>
                    {active && (
                      <span className="ml-1 text-accent">{sort.direction === 1 ? '↑' : '↓'}</span>
                    )}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, index) => (
              <tr
                key={index}
                onClick={navigable ? () => onRowClick!(row) : undefined}
                title={navigable ? 'select this session' : undefined}
                className={`border-b border-edge/40 last:border-0 hover:bg-panel-raised
                            ${navigable ? 'cursor-pointer' : ''}`}
              >
                {table.columns.map((column) => {
                  const value = row[column]
                  const info = byId[column]
                  const band = shadeFor(value, info?.bands)
                  return (
                    <td
                      key={column}
                      style={band ? { backgroundColor: band.background } : undefined}
                      className={`px-3 py-1.5 whitespace-nowrap ${
                        info?.align === 'right' || (!info && isNumeric(value))
                          ? 'text-right font-mono tabular-nums'
                          : ''
                      } ${value === null || value === undefined ? 'text-ink-faint' : ''}`}
                    >
                      {show(value, info)}
                    </td>
                  )
                })}
              </tr>
            ))}
            {visible.length === 0 && (
              <tr>
                <td
                  colSpan={table.columns.length}
                  className="px-3 py-6 text-center text-sm text-ink-dim"
                >
                  Nothing matches {JSON.stringify(query)}.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {rows.length > limit && (
        <button
          onClick={() => setLimit((was) => was + PAGE * 5)}
          className="w-full border-t border-edge bg-panel-raised px-3 py-2 text-xs text-ink-dim
                     hover:text-ink"
        >
          showing {limit} of {rows.length.toLocaleString()} rows, show more
        </button>
      )}
      {rows.length <= limit && rows.length > 0 && (
        <div className="border-t border-edge px-3 py-1.5 text-xs text-ink-faint">
          {rows.length.toLocaleString()} {rows.length === 1 ? 'row' : 'rows'}
        </div>
      )}
    </div>
  )
}

export { applySpecifier, shadeFor }
