import { useRef, useState } from 'react'
import type { TabPayload } from '@/api'
import { hydrate } from './exporters'
import type { InspectorContent } from './Inspector'
import { shown } from './inspect'

/**
 * What the drawer is showing, and the fetch that fills it in.
 *
 * A hook, because the pane and the single-table page both need it and the page shipped without it:
 * the Messages table carries the note "Click a row to read it in full", the page showed that note,
 * and a click there did nothing.
 *
 * THE STALE GUARD IS A TOKEN, AND THE HOOK DOES NOT HAND OUT THE RAW SETTER. Every change of
 * subject takes the next token; only the current one may write. The first version exported
 * `setContent`, and the chart-click path used it without taking a token, so a row fetch still in
 * flight wrote that message's whole text into a drawer headed with a chart point's name. A guard a
 * caller can step around is documentation, not a guard. `close` takes one too, so nothing issued
 * before a close can land after it, rather than that being true by accident of a null check.
 */
export function useRowInspector(
  payload: TabPayload,
  names: string[],
  onSelectSession?: (row: Record<string, unknown>) => void,
) {
  const [content, setContent] = useState<InspectorContent | null>(null)
  const token = useRef(0)
  /** The next token. EVERY change of subject takes one, which is what makes the guard total. */
  const claim = () => ++token.current

  /** A chart point: no fetch of its own, but it must still retire a row fetch in flight. */
  const showPoint = (next: InspectorContent) => setContent({ ...next, subject: claim() })

  const close = () => {
    claim()
    setContent(null)
  }

  const openRow = (index: number) => (row: Record<string, unknown>) => {
    const mine = claim()
    const meta = payload.meta?.[index]
    const spec = meta?.full_text
    const fields = Object.entries(row)
      .filter(([key]) => !spec || key !== spec.column)
      .map(([key, value]) => [key, shown(value)] as [string, string])
    const session = row.session_id ?? row.session
    const base: InspectorContent = {
      subject: mine,
      title: `${names[index]}: row`,
      source: names[index],
      fields,
      onSelectSession: onSelectSession && typeof session === 'string' && session
        // Closed as it fires: the pane's query key carries the session, so the page this drawer
        // was opened from is about to be rebuilt, and a drawer describing a row of the previous
        // payload would sit over the new one.
        ? () => { onSelectSession(row); close() }
        : null,
    }
    // A ROW THAT POINTS AT SOMETHING BIGGER THAN ITSELF. The compactions table records the token
    // counts of a boundary; what the boundary REPLACED is a 14,000-character summary the store has
    // always held and this page could not reach. The table's own note has said "click a row to
    // read the summary it produced" since the tab existed, over a click that did nothing.
    const detail = meta?.detail
    if (detail) {
      const key = row[detail.key]
      if (typeof key === 'string' && key) {
        setContent({ ...base, text: null })
        const write = (patch: Partial<InspectorContent>) => {
          if (token.current === mine) setContent((was) => (was ? { ...was, ...patch } : was))
        }
        fetch(`${detail.url}/${encodeURIComponent(key)}`)
          .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
          .then((body) => write({
            text: body?.summary?.text
              ?? 'No summary message was harvested for this compaction. Older boundaries record '
               + 'token counts only.',
            // Stated, not implied by the row count: the dropped set is a LOWER BOUND, computed by
            // subtracting recorded survivors, so a survivor the store never saw counts as dropped.
            textNote: body?.dropped_total
              ? `${body.dropped_total.toLocaleString()} messages were present before this `
                + `compaction and are absent from its survivor list, a lower bound.`
              : null,
          }))
          .catch(() => write({
            text: null,
            textProblem: 'the summary could not be fetched',
          }))
        return
      }
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

  return { content, showPoint, openRow, close }
}
