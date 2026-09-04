import { useRef, useState } from 'react'
import type { TabPayload } from '@/api'
import { hydrate } from './exporters'
import type { InspectorContent } from './Inspector'
import { shown } from './inspect'

/**
 * Opening a table row: every field, and the message itself where a column was cut.
 *
 * A hook, because the pane and the single-table page both need it and the page shipped without it:
 * the Messages table carries the note "Click a row to read it in full", the page showed that note,
 * and a click there did nothing. Found by a review.
 *
 * THE STALE GUARD IS A TOKEN, not the drawer's title. The title is the same string for every row of
 * a table ("Messages: row"), so two quick opens could put the first row's text under the second
 * row's fields, and the reader would have no way to know. Each open takes the next token and only
 * the current one may write.
 */
export function useRowInspector(
  payload: TabPayload,
  names: string[],
  onSelectSession?: (row: Record<string, unknown>) => void,
) {
  const [content, setContent] = useState<InspectorContent | null>(null)
  const token = useRef(0)

  const openRow = (index: number) => (row: Record<string, unknown>) => {
    const mine = ++token.current
    const meta = payload.meta?.[index]
    const spec = meta?.full_text
    const fields = Object.entries(row)
      .filter(([key]) => !spec || key !== spec.column)
      .map(([key, value]) => [key, shown(value)] as [string, string])
    const session = row.session_id ?? row.session
    const base: InspectorContent = {
      title: `${names[index]}: row`,
      source: names[index],
      fields,
      onSelectSession: onSelectSession && typeof session === 'string' && session
        ? () => onSelectSession(row)
        : null,
    }
    if (!spec) {
      setContent(base)
      return
    }
    setContent({ ...base, text: null })
    const columns = payload.tables[index].columns.map((id) => ({
      id, label: id, numeric: false, specifier: null, align: 'left' as const, hidden: false, bands: [],
    }))
    const write = (patch: Partial<InspectorContent>) => {
      if (token.current === mine) setContent((was) => (was ? { ...was, ...patch } : was))
    }
    hydrate({ columns, rows: [row], name: names[index], format: (v) => shown(v), fullText: spec })
      .then((full) => write({ text: shown(full.rows[0]?.[spec.column]) }))
      .catch(() => write({
        text: shown(row[spec.column]),
        textProblem: 'the full text could not be fetched; this is the preview',
      }))
  }

  return { content, setContent, openRow, close: () => setContent(null) }
}
