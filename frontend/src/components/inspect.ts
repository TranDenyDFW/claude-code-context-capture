import type { Table, TableMeta } from '@/api'
import type { InspectorContent } from './Inspector'
import type { PlotPoint } from './Plot'

/**
 * Turning a clicked chart point into something a reader can check: what it identifies, and which
 * rows of this tab are actually about it.
 *
 * THE FIRST VERSION TOOK customdata[0] AND CALLED IT A SESSION ID. On the Sessions chart the
 * server puts the TITLE there and the id third (c4x/tabs/sessions.py:80), so every point was
 * identified by its title; on a store where 210 of 1,327 sessions have no title the drawer named
 * them all "(untitled)" and showed all 49 fixture sessions as "the rows behind it". Found by an
 * independent review, which is also why nothing here guesses a position any more: a candidate has
 * to MATCH a cell, and a match that returns the whole table is refused rather than shown.
 */

/** A value as the inspector prints it. */
export function shown(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') return Number.isInteger(value) ? value.toLocaleString() : String(value)
  if (Array.isArray(value)) return value.map(shown).join(', ')
  return String(value)
}

/** Columns that identify a ROW rather than describe it, most specific first. */
const KEYS = ['session_id', 'session', 'uuid', 'tool_use_id', 'compaction_uuid']

/**
 * Every string a point carries that could name something: each element of customdata, the label of
 * a treemap or pie cell, and a categorical x or y. In the order tried, not ranked; `locate` picks.
 */
export function candidates(point: PlotPoint): string[] {
  const out: string[] = []
  const push = (value: unknown) => {
    if (typeof value === 'string' && value.trim()) out.push(value)
  }
  if (Array.isArray(point.customdata)) point.customdata.forEach(push)
  else push(point.customdata)
  push(point.label)
  push(point.x)
  push(point.y)
  return out
}

export interface Located {
  index: number
  rows: Record<string, unknown>[]
  /** The candidate that found them. */
  value: string
  /** The column it matched, when that column identifies a row. */
  key: string | null
}

/**
 * The rows of `tables` that are ABOUT one of `values`, or null.
 *
 * A match on a key column wins, because that is one row and it is the row. Otherwise the most
 * specific match wins: the fewest rows. A candidate that matches EVERY row of a table has
 * identified nothing, and showing that table would be a wrong answer that looks checkable, so it
 * is refused, unless the table holds ONE row: there the whole table is that row.
 */
const specific = (rows: Record<string, unknown>[], table: Table) =>
  rows.length > 0 && (rows.length < table.rows.length || table.rows.length === 1)

export function locate(tables: Table[], values: string[]): Located | null {
  let best: Located | null = null
  for (let index = 0; index < tables.length; index++) {
    const table = tables[index]
    if (!table.rows.length) continue
    for (const value of values) {
      for (const key of KEYS) {
        const rows = table.rows.filter((row) => row[key] === value)
        if (specific(rows, table)) return { index, rows, value, key }
      }
      const rows = table.rows.filter((row) =>
        Object.values(row).some((cell) => typeof cell === 'string' && cell === value))
      if (specific(rows, table) && (!best || rows.length < best.rows.length)) {
        best = { index, rows, value, key: null }
      }
    }
  }
  return best
}

/** The value the standalone page can FILTER BY: one that shows in a column the page draws. */
export function visibleValue(
  found: Located, meta: TableMeta | undefined, table: Table,
): string | null {
  const hidden = new Set((meta?.columns ?? []).filter((c) => c.hidden).map((c) => c.id))
  const columns = (meta?.columns ?? []).map((c) => c.id)
  const drawn = (columns.length ? columns : table.columns).filter((id) => !hidden.has(id))
  for (const row of found.rows) {
    for (const id of drawn) {
      if (typeof row[id] === 'string' && row[id] === found.value) return found.value
    }
  }
  return null
}

/** What the inspector shows for a clicked point. */
export function describePoint(
  point: PlotPoint, figureTitle: string | null, tables: Table[], meta: TableMeta[], names: string[],
): {
  content: InspectorContent
  tableIndex: number | null
  /** The exact filter for a link: the column and the value, so a hidden key still works. */
  filter: { key: string; value: string } | null
  /** A text filter for a link, when the value is one the page actually draws. */
  query: string | null
} {
  const fields: [string, string][] = []
  if (point.traceName) fields.push(['series', point.traceName])
  if (point.label !== undefined) fields.push(['label', shown(point.label)])
  if (point.x !== undefined) fields.push(['x', shown(point.x)])
  if (point.y !== undefined) fields.push(['y', shown(point.y)])
  if (point.value !== undefined) fields.push(['value', shown(point.value)])
  if (point.text !== undefined && point.text !== null) fields.push(['text', shown(point.text)])
  if (point.customdata !== undefined) fields.push(['data', shown(point.customdata)])

  const found = locate(tables, candidates(point))
  const title = found
    ? found.value
    : (point.traceName ? `${point.traceName} at ${shown(point.x)}` : `point ${point.pointIndex + 1}`)
  return {
    content: {
      title,
      source: figureTitle,
      fields,
      rows: found
        ? { name: names[found.index], table: { ...tables[found.index], rows: found.rows }, meta: meta[found.index] }
        : null,
      // SAID, not left blank. Most points on a turn chart identify no row at all, and a drawer
      // that just stops reads as a drawer that failed.
      noRows: found ? null : 'No table on this tab has rows for this point.',
    },
    tableIndex: found ? found.index : null,
    filter: found && found.key ? { key: found.key, value: found.value } : null,
    query: found ? visibleValue(found, meta[found.index], tables[found.index]) : null,
  }
}
