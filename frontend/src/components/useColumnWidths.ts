import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ColumnMeta } from '@/api'
import { COLUMN, estimateWidths, layoutWidths } from './columnWidths'
import { clampSize } from './useDragSize'

/**
 * The widths one table uses: derived from its data, overridden by whatever the reader dragged,
 * and remembered per table.
 *
 * REMEMBERED BY THE TABLE, NOT BY THE SLOT IT SITS IN. The key is the table's id plus its sorted
 * column ids, so the same table keeps its widths wherever it is drawn and two different tables
 * that happen to land in the same position on two tabs cannot inherit each other's. Keying on the
 * slot is the defect `PaneTableIdentity.test.tsx` exists for, and it would have come back here.
 *
 * ONLY THE OVERRIDES ARE STORED. A better estimate then improves every table nobody has touched,
 * and a stored entry can never pin a column to a width the data no longer justifies.
 */
const KEY = 'c4x.table.widths.'
const VERSION = 1

export function widthKey(tableId: string, columnIds: string[]): string {
  return `${KEY}${tableId}|${[...columnIds].sort().join(',')}`
}

/** What survives a read: version 1, a known column, a number inside that column's bounds. */
export function readRemembered(
  raw: string | null,
  columns: ColumnMeta[],
): Record<string, number> {
  if (!raw) return {}
  let said: unknown
  try {
    said = JSON.parse(raw)
  } catch {
    return {}
  }
  if (!said || typeof said !== 'object') return {}
  const body = said as { v?: unknown; cols?: unknown }
  if (body.v !== VERSION || !body.cols || typeof body.cols !== 'object') return {}
  const out: Record<string, number> = {}
  for (const column of columns) {
    const value = (body.cols as Record<string, unknown>)[column.id]
    if (typeof value !== 'number' || !Number.isFinite(value)) continue
    // The same bound the drag uses, not the estimate's: a width somebody chose for the wide
    // column must survive a reload.
    out[column.id] = clampSize(value, COLUMN.MIN, COLUMN.MAX)
  }
  return out
}

export interface ColumnWidths {
  width: (id: string) => number
  total: number
  setWidth: (id: string, px: number) => void
  reset: (id?: string) => void
  /** True once anything has been dragged, so a Reset control appears only when it would do work. */
  resized: boolean
  boundsFor: (column: ColumnMeta) => { min: number; max: number }
  containerRef: React.RefObject<HTMLDivElement | null>
}

export function useColumnWidths({
  tableId,
  columns,
  rows,
  format,
  measure,
  remember = true,
}: {
  tableId: string
  /** In display order, hidden ones already removed. */
  columns: ColumnMeta[]
  /** EVERY row the table holds. Never a page: a width that moves with the page is the defect. */
  rows: Record<string, unknown>[]
  format: (value: unknown, column: ColumnMeta) => string
  measure?: (text: string, mono: boolean) => number
  remember?: boolean
}): ColumnWidths {
  const ids = columns.map((c) => c.id)
  const signature = widthKey(tableId, ids)
  const [override, setOverride] = useState<Record<string, number>>({})
  const [containerWidth, setContainerWidth] = useState<number | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)

  // READ ON EVERY CHANGE OF TABLE, not once on mount. A `useState` initialiser runs for the first
  // table a slot ever held and never again, which is exactly how a component reused across tabs
  // ends up showing another table's state.
  useEffect(() => {
    if (!remember) {
      setOverride({})
      return
    }
    try {
      setOverride(readRemembered(localStorage.getItem(signature), columns))
    } catch {
      setOverride({})
    }
    // `columns` changes identity on every render of the caller; the signature is the fact that
    // matters and it changes only when the table or its column set does.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, remember])

  const save = useCallback(
    (next: Record<string, number>) => {
      if (!remember) return
      try {
        if (Object.keys(next).length === 0) localStorage.removeItem(signature)
        else localStorage.setItem(signature, JSON.stringify({ v: VERSION, cols: next }))
      } catch {
        /* a preference that cannot be saved is not worth an error */
      }
    },
    [remember, signature],
  )

  const base = useMemo(
    () => estimateWidths(columns, rows, format, measure),
    // Same reasoning as above: the inputs that matter are the column ids and the rows.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ids.join(','), rows, format, measure],
  )

  // The container is measured only to hand out slack. Guarded, because jsdom has no
  // ResizeObserver, and a zero measurement is not a measurement.
  useLayoutEffect(() => {
    const node = containerRef.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const watch = new ResizeObserver((entries) => {
      const seen = entries[0]?.contentRect.width ?? 0
      setContainerWidth(seen > 0 ? seen : null)
    })
    watch.observe(node)
    return () => watch.disconnect()
  }, [])

  const { widths, total } = layoutWidths(base, override, ids, containerWidth)

  // WHAT A READER MAY DRAG TO, which is not the same as what the estimate may choose. The `wide`
  // column is capped at 24rem so one long value cannot push every other column off the screen;
  // capping the DRAG there too meant the widest column, the one most worth widening, refused to
  // move at all. Measured in Chrome on the Sessions table: Title sat at its 384px cap, and an
  // arrow key on its handle did nothing.
  const boundsFor = useCallback((_column: ColumnMeta) => ({ min: COLUMN.MIN, max: COLUMN.MAX }), [])

  return {
    width: (id: string) => widths[id] ?? COLUMN.MIN,
    total,
    setWidth: (id: string, px: number) =>
      setOverride((was) => {
        const next = { ...was, [id]: px }
        save(next)
        return next
      }),
    reset: (id?: string) =>
      setOverride((was) => {
        const next = { ...was }
        if (id === undefined) for (const key of Object.keys(next)) delete next[key]
        else delete next[id]
        save(next)
        return next
      }),
    resized: Object.keys(override).length > 0,
    boundsFor,
    containerRef,
  }
}
