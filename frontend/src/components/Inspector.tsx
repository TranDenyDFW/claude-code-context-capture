import { useEffect, useRef, useState } from 'react'
import type { Table, TableMeta } from '@/api'
import { DataTable } from './DataTable'

/**
 * What one thing on the page is made of: a point somebody clicked on a chart, or a row they opened.
 *
 * A drawer, not a page. The chart stays in view, so the reader can click the next point without
 * finding their way back; the drawer carries a way to open the same rows in a window of their own
 * for anyone who wants the page. Transient by design: nothing about it goes into the URL, because a
 * link that reopened a drawer would surprise the person it was sent to.
 *
 * A NON-MODAL DIALOG, ON PURPOSE, AND aria-modal AND A TAB TRAP HAVE BOTH BEEN TRIED AND REVERTED.
 * There is no backdrop and nothing behind is inert or aria-hidden: the chart stays live so the
 * reader can click the next point. `aria-modal` would tell assistive technology that everything
 * outside is unavailable, which is false here, and a Tab trap would strand a screen-reader user in
 * the drawer with no way back to the chart it exists to keep in view. The two places in this
 * codebase that ARE modal, the palette and the project mover, each have a full-viewport backdrop
 * and each declare aria-modal; the attribute tracks modality, it is not decoration. A review said
 * all of this and the finding was "fixed" against it anyway, which is why the reasoning is here
 * rather than only the verdict.
 *
 * What it does have: a role and an accessible name, Escape, a close button, focus moved here when
 * it opens and when its SUBJECT changes, and focus returned where the reader left it on close.
 */
export interface InspectorContent {
  /**
   * Which thing this drawer is about, from the hook's token. NOT the title, which repeats: every
   * row of the Messages table is titled "Messages: row", so a second row would move nothing.
   */
  subject?: number
  /** The accessible name of the drawer and its heading. */
  title: string
  /** Where it came from: the figure's title, or the table's name. */
  source?: string | null
  /** Label and value, already formatted for reading. */
  fields: [string, string][]
  /** A message's full text. `undefined` when there is none, `null` while it is being fetched. */
  text?: string | null
  /** A problem with the text, said next to it rather than in place of it. */
  textProblem?: string | null
  /**
   * What was REPLACED, when `text` is a summary of something.
   *
   * A compaction is two documents: the summary that survived and the messages that did not. The
   * summary alone reads as the whole story, and the whole point of looking at a boundary is what
   * it cost, so the dropped side is shown beside it rather than being a count.
   */
  dropped?: { ts: string; role: string; type: string; chars: number; preview: string }[] | null
  /** A qualifier on `text` that the reader must see, such as a count being a lower bound. */
  textNote?: string | null
  /** The rows behind the thing, already filtered, drawn as the same table they came from. */
  rows?: { name: string; table: Table; meta?: TableMeta } | null
  /** Why there are none, when there are none. */
  noRows?: string | null
  /** Open those rows on a page of their own. */
  onOpen?: (() => void) | null
  /** Select the session this row belongs to, when it names one. */
  onSelectSession?: (() => void) | null
}

export function Inspector({ content, onClose }: { content: InspectorContent; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null)
  const [copied, setCopied] = useState(false)
  // Read at event time, so a new closure from the parent never rebinds the handlers below.
  const close = useRef(onClose)
  close.current = onClose

  // ON OPEN AND ON A CHANGE OF SUBJECT, never on every render, and never when the same subject
  // merely gains its text. A re-render of the page behind must not take focus off whatever the
  // reader had chosen.
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null
    panel.current?.focus()
    return () => {
      // Focus goes back where the reader left it, which is what closing a dialog means.
      if (before && document.contains(before)) before.focus()
    }
  }, [content.subject])

  useEffect(() => {
    // Escape only. This is a non-modal dialog and it must not swallow Tab; see the docstring.
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close.current()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div
      ref={panel}
      role="dialog"
      aria-label={content.title}
      tabIndex={-1}
      className="fixed inset-y-0 right-0 z-30 flex w-full max-w-xl flex-col gap-3 overflow-y-auto
                 border-l border-edge bg-panel p-4 shadow-panel outline-none"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-md font-semibold text-ink">{content.title}</h2>
          {content.source && <p className="truncate text-2xs text-ink-faint">{content.source}</p>}
        </div>
        <button
          onClick={onClose}
          aria-label="Close"
          title="Close (Esc)"
          className="rounded border border-edge px-2 py-1 text-xs text-ink-dim hover:text-ink"
        >
          ×
        </button>
      </div>

      {content.fields.length > 0 && (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
          {content.fields.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-ink-faint">{label}</dt>
              <dd className="min-w-0 break-words font-mono text-xs text-ink">{value}</dd>
            </div>
          ))}
        </dl>
      )}

      {content.onSelectSession && (
        <div>
          <button
            onClick={content.onSelectSession}
            className="rounded border border-edge px-2 py-1 text-xs text-ink-dim hover:text-ink"
            title="Make this session the selection in this window; Back to the dashboard then shows it"
          >
            Select this session
          </button>
        </div>
      )}

      {content.text !== undefined && (
        <section className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <h3 className="text-2xs tracking-[0.06em] text-ink-faint">FULL TEXT</h3>
            {content.text && (
              <button
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(content.text ?? '')
                    setCopied(true)
                    setTimeout(() => setCopied(false), 1500)
                  } catch {
                    setCopied(false)
                  }
                }}
                className="rounded border border-edge px-2 py-0.5 text-2xs text-ink-dim hover:text-ink"
              >
                {copied ? 'copied' : 'copy'}
              </button>
            )}
            {content.textNote && (
              <span className="text-2xs text-ink-faint">{content.textNote}</span>
            )}
            {content.textProblem && (
              <span role="alert" className="text-2xs text-ink-dim">{content.textProblem}</span>
            )}
          </div>
          {content.text === null ? (
            <p className="text-xs text-ink-faint">fetching the full text</p>
          ) : (
            <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded bg-page px-3 py-2
                            font-mono text-xs leading-relaxed text-ink">
              {content.text}
            </pre>
          )}

          {content.dropped && content.dropped.length > 0 && (
            <div className="mt-4">
              <div className="mb-1 text-2xs uppercase tracking-[0.06em] text-ink-faint">
                What it dropped, largest first
              </div>
              <div className="max-h-[40vh] overflow-auto rounded border border-edge">
                {content.dropped.map((row, index) => (
                  <div key={index} className="border-b border-edge/40 px-3 py-2 last:border-0">
                    <div className="flex items-baseline gap-2 text-2xs text-ink-faint">
                      <span className="tabular-nums">{row.chars.toLocaleString()} chars</span>
                      <span>{row.role}</span>
                      <span>{row.type}</span>
                      <span className="ml-auto tabular-nums">{String(row.ts).slice(0, 19)}</span>
                    </div>
                    <p className="mt-0.5 truncate font-mono text-2xs text-ink-dim"
                       title={row.preview}>{row.preview}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>
      )}

      {content.rows && (
        <section className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="text-2xs tracking-[0.06em] text-ink-faint">
              THE ROWS BEHIND IT: {content.rows.name}, {content.rows.table.rows.length.toLocaleString()}{' '}
              {content.rows.table.rows.length === 1 ? 'row' : 'rows'}
            </h3>
            {content.onOpen && (
              <button
                onClick={content.onOpen}
                className="rounded border border-edge px-2 py-0.5 text-2xs text-ink-dim hover:text-ink"
                title="Open these rows on a page of their own, in a new window"
              >
                Open in new window
              </button>
            )}
          </div>
          <DataTable table={content.rows.table} meta={content.rows.meta} title={content.rows.name} />
        </section>
      )}

      {!content.rows && content.noRows && (
        <p className="text-2xs text-ink-faint">{content.noRows}</p>
      )}
    </div>
  )
}
