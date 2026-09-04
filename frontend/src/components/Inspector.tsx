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
 * IT IS A REAL DIALOG, which took a review to get right. The first version focused its container in
 * an effect that ran on every parent render, so a reader who tabbed to the close button lost focus
 * the moment anything above re-rendered; it also had no aria-modal, no focus trap and no return of
 * focus on close, which makes "role=dialog" a claim rather than a behaviour.
 */
export interface InspectorContent {
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
  /** The rows behind the thing, already filtered, drawn as the same table they came from. */
  rows?: { name: string; table: Table; meta?: TableMeta } | null
  /** Why there are none, when there are none. */
  noRows?: string | null
  /** Open those rows on a page of their own. */
  onOpen?: (() => void) | null
  /** Select the session this row belongs to, when it names one. */
  onSelectSession?: (() => void) | null
}

const FOCUSABLE = 'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])'

export function Inspector({ content, onClose }: { content: InspectorContent; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null)
  const [copied, setCopied] = useState(false)
  // Read at event time, so a new closure from the parent never rebinds the handlers below.
  const close = useRef(onClose)
  close.current = onClose

  // ON MOUNT AND WHEN THE SUBJECT CHANGES, never on every render. `content.title` is what the
  // drawer is about; clicking a different point moves focus here, a re-render above does not.
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null
    panel.current?.focus()
    return () => {
      // Focus goes back where the reader left it, which is what closing a dialog means.
      if (before && document.contains(before)) before.focus()
    }
  }, [content.title])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        close.current()
        return
      }
      if (event.key !== 'Tab' || !panel.current) return
      // A focus trap, so Tab cycles the drawer rather than walking the page behind it.
      const stops = Array.from(panel.current.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter((node) => node.offsetParent !== null || node === document.activeElement)
      if (!stops.length) return
      const first = stops[0]
      const last = stops[stops.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || active === panel.current)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div
      ref={panel}
      role="dialog"
      aria-modal="true"
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
            title="Make this row's session the header selection"
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
