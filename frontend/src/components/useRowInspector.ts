import { useRef, useState } from 'react'
import type { ChatWork, TabPayload } from '@/api'
import { hydrate } from './exporters'
import type { InspectorContent } from './Inspector'
import { shown } from './inspect'
import { chatWorkContent } from './chatWork'

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
  /** Open the whole of what a row points at, when the drawer can only show part of it. */
  onOpenDetail?: (key: string) => void,
  /**
   * The same, for a chat's plans and background work.
   *
   * A SECOND CALLBACK, not a reuse of the one above. Both branches open "the whole of this in a
   * window", and the windows are two different pages; one handler would have had to guess which,
   * from state the hook does not hold.
   */
  onOpenChatWork?: (sessionId: string) => void,
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
    // THE DRAWER SPEAKS THE TABLE'S LANGUAGE, NOT THE SCHEMA'S. This listed the raw column ids, so
    // clicking a Messages row produced `ts`, `role`, `type` under a table headed `Date & Time`,
    // `Record Type`, `Written By`. Same data, two vocabularies, and one of them is the one
    // DataTable.tsx names as wrong in as many words: "COLUMN NAMES come from column_label(). The
    // raw ids are schema, not English."
    //
    // The two ids it exposed are exactly the two the label map exists to correct, and that
    // correction is not cosmetic. c4x/theme.py records why: `role` and `type` were BOTH the
    // transcript record's own type field, so a directory listing and a question both read "user",
    // and 86.5% of the records typed 'user' were tool results. `Record Type` and `Written By` carry
    // the whole of that distinction, and the drawer was throwing it away.
    //
    // The payload already carries the answer, one label per column, so nothing new is fetched. A
    // column the payload does not describe keeps its id: an unlabelled field is still worth showing,
    // and inventing a label for it would be guessing.
    const labels = new Map((meta?.columns ?? []).map((c) => [c.id, c.label]))
    const fields = Object.entries(row)
      .filter(([key]) => !spec || key !== spec.column)
      .map(([key, value]) => [labels.get(key) ?? key, shown(value)] as [string, string])
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
    // WHAT THIS CHAT PLANNED AND RAN. A row of the Sessions list names a chat, and the chat has
    // five lists of its own behind it: the plans it wrote, the subagents it ran, the workflows it
    // launched, the tasks it was told about and the files it changed. `row_detail` is deliberately not `detail`: the
    // click on this table still selects the session, and the control beside the row opens this.
    const work = meta?.row_detail
    if (work) {
      const key = row[work.key]
      if (typeof key === 'string' && key) {
        setContent({
          ...base,
          title: 'Work in this chat',
          text: null,
          onOpenDetail: onOpenChatWork ? () => onOpenChatWork(key) : null,
        })
        const write = (patch: Partial<InspectorContent>) => {
          if (token.current === mine) setContent((was) => (was ? { ...was, ...patch } : was))
        }
        fetch(`${work.url}/${encodeURIComponent(key)}`)
          .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
          .then((body: ChatWork) => write(chatWorkContent(body)))
          .catch(() => write({
            text: null,
            textProblem: 'the plans and background work of this chat could not be fetched',
          }))
        return
      }
    }
    const detail = meta?.detail
    if (detail) {
      const key = row[detail.key]
      if (typeof key === 'string' && key) {
        setContent({
          ...base,
          text: null,
          // THE DRAWER SHOWS PART, THE WINDOW SHOWS ALL. A boundary is a 14,000-character summary
          // AND hundreds of messages; the drawer is the wrong shape for both at once, so it shows
          // enough to decide and offers the place where the whole thing is.
          onOpenDetail: onOpenDetail ? () => onOpenDetail(key) : null,
        })
        const write = (patch: Partial<InspectorContent>) => {
          if (token.current === mine) setContent((was) => (was ? { ...was, ...patch } : was))
        }
        fetch(`${detail.url}/${encodeURIComponent(key)}`)
          .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
          .then((body) => write({
            dropped: body?.dropped ?? null,
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
