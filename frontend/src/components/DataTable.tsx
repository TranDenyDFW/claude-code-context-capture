import { useMemo, useState } from 'react'
import type { Table } from '@/api'

/**
 * One table, with the things the Dash version had and a browser table usually does not: an
 * explanation on every column that carries one, sorting, and a row count that says how much is not
 * on screen.
 *
 * NUMBERS ARE RIGHT-ALIGNED AND FORMATTED, blanks stay blank. That last part is load-bearing in
 * this app: an unpriced model has no cost, and rendering that as 0 would state something false and
 * cheap-looking rather than something unknown. The API sends null for it; this renders nothing.
 */

const PAGE = 100

function isNumeric(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function show(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return ''
    // Integers keep their thousands separators; fractions keep enough digits to stay honest about
    // small costs, where rounding to two places would print $0.00 for a real charge.
    if (Number.isInteger(value)) return value.toLocaleString()
    return Math.abs(value) < 0.01 ? value.toPrecision(2) : value.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  }
  return String(value)
}

export function DataTable({
  table,
  onRowClick,
}: {
  table: Table
  onRowClick?: (row: Record<string, unknown>) => void
}) {
  const [sort, setSort] = useState<{ column: string; direction: 1 | -1 } | null>(null)
  const [limit, setLimit] = useState(PAGE)

  const rows = useMemo(() => {
    if (!sort) return table.rows
    const copy = [...table.rows]
    copy.sort((a, b) => {
      const left = a[sort.column]
      const right = b[sort.column]
      // Blanks sort last in both directions. A column of unknown costs should not push the rows
      // you can actually read off the top of the table when you sort by it.
      if (left === null || left === undefined) return 1
      if (right === null || right === undefined) return -1
      if (isNumeric(left) && isNumeric(right)) return (left - right) * sort.direction
      return String(left).localeCompare(String(right)) * sort.direction
    })
    return copy
  }, [table.rows, sort])

  if (!table.columns.length) return null

  const visible = rows.slice(0, limit)

  return (
    <div className="overflow-hidden rounded-lg border border-edge bg-panel">
      <div className="max-h-[32rem] overflow-auto">
        <table className="w-full border-collapse text-[12.5px]">
          <thead className="sticky top-0 z-10 bg-panel-raised">
            <tr>
              {table.columns.map((column) => {
                const help = table.tooltips?.[column]
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
                    className="cursor-pointer border-b border-edge px-3 py-2 text-left font-semibold
                               whitespace-nowrap text-ink-dim select-none hover:text-ink"
                  >
                    <span className={help ? 'has-help' : undefined}>{column}</span>
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
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={`border-b border-edge/40 last:border-0 hover:bg-panel-raised
                            ${onRowClick ? 'cursor-pointer' : ''}`}
              >
                {table.columns.map((column) => {
                  const value = row[column]
                  return (
                    <td
                      key={column}
                      className={`px-3 py-1.5 whitespace-nowrap ${
                        isNumeric(value) ? 'text-right font-mono tabular-nums' : ''
                      } ${value === null || value === undefined ? 'text-ink-faint' : ''}`}
                    >
                      {show(value)}
                    </td>
                  )
                })}
              </tr>
            ))}
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
        <div className="border-t border-edge px-3 py-1.5 text-[11px] text-ink-faint">
          {rows.length.toLocaleString()} {rows.length === 1 ? 'row' : 'rows'}
        </div>
      )}
    </div>
  )
}
