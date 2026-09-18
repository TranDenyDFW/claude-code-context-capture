import { useEffect, useRef, useState } from 'react'
import type { Table, TableMeta } from '@/api'
import { DataTable } from './DataTable'
import { ResizeHandle } from './ResizeHandle'
import { clampSize } from './useDragSize'
import { TextBody } from './Markdown'

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
   * Whether `text` is a document somebody wrote, or output something produced.
   *
   * Decided where the text is FETCHED, from the row's own `type`, rather than guessed here from
   * the characters: a tool result full of hashes and pipes looks exactly like markdown.
   */
  textKind?: 'markdown' | 'plain'
  /** The filename stem an export of `text` gets. */
  fileName?: string | null
  /**
   * What was REPLACED, when `text` is a summary of something.
   *
   * A compaction is two documents: the summary that survived and the messages that did not. The
   * summary alone reads as the whole story, and the whole point of looking at a boundary is what
   * it cost, so the dropped side is shown beside it rather than being a count.
   */
  dropped?: { ts: string; role: string; type: string; chars: number; preview: string }[] | null
  /** Open the whole of this subject in a window of its own, when the drawer shows part. */
  onOpenDetail?: (() => void) | null
  /** A qualifier on `text` that the reader must see, such as a count being a lower bound. */
  textNote?: string | null
  /**
   * The rows behind the thing, already filtered, drawn as the same table they came from.
   *
   * A LIST OF LISTS, because a drawer about a chat has four of them: the plans it wrote, the
   * agents it ran, the workflows it launched and the tasks it was told about. One list was the
   * shape when the only subject was one row of one table; pretending four are one would have made
   * the second one a rebuild rather than an entry.
   */
  rows?: { name: string; table: Table; meta?: TableMeta }[] | null
  /** Why there are none, when there are none. */
  noRows?: string | null
  /** Open those rows on a page of their own. */
  onOpen?: (() => void) | null
  /** Select the session this row belongs to, when it names one. */
  onSelectSession?: (() => void) | null
}

const WIDTH = 'c4x.inspector.width'
/** `max-w-xl`, the width this drawer has had; below the floor its field grid stops working. */
export const DRAWER = { DEFAULT: 576, MIN: 320, MAX: 1400 } as const

/**
 * The widest the drawer may get: always leaving a strip of the page beside it.
 *
 * Not an arbitrary margin. This drawer is non-modal precisely so the chart or table behind it
 * stays live and the reader can click the next point; one that reaches the left edge destroys the
 * only reason it is not a modal.
 */
export function drawerCeiling(viewport = window.innerWidth): number {
  return Math.max(DRAWER.MIN, Math.min(DRAWER.MAX, viewport - 160))
}

export function useInspectorWidth(): [number, (next: number) => void] {
  const [width, setWidth] = useState(() => {
    try {
      const said = Number(localStorage.getItem(WIDTH))
      return Number.isFinite(said) && said > 0 ? clampSize(said, DRAWER.MIN, DRAWER.MAX) : DRAWER.DEFAULT
    } catch {
      return DRAWER.DEFAULT
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem(WIDTH, String(width))
    } catch {
      /* a preference that cannot be saved is not worth an error */
    }
  }, [width])
  return [width, setWidth]
}

export function Inspector({ content, onClose }: { content: InspectorContent; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null)
  const [copied, setCopied] = useState(false)
  const [width, setWidth] = useInspectorWidth()
  // Read by the Escape handler below, which is bound once and must see the current value.
  const [resizing, setResizing] = useState(false)
  const dragging = useRef(false)
  dragging.current = resizing
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
    //
    // EXCEPT WHILE THE EDGE IS BEING DRAGGED, where Escape belongs to the drag: it puts the width
    // back. Without this, cancelling a resize closed the panel and threw the width away. A flag,
    // not event ordering: both listeners sit on `window`, and when the event's target IS `window`
    // a capturing listener cannot stop a bubbling one that was registered first.
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !dragging.current) close.current()
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
      // `max-w-full` is load bearing: it keeps the full-bleed drawer a narrow screen has always
      // had, and it makes a remembered 900px harmless on a phone.
      style={{ width }}
      className="fixed inset-y-0 right-0 z-30 flex max-w-full flex-col gap-3 overflow-y-auto
                 border-l border-edge bg-panel p-4 shadow-panel outline-none"
    >
      {/*
        THE HANDLE SITS INSIDE THE EDGE, not straddling it like a column's. This panel floats over
        live table rows, and a handle hanging into the page would put a divider on top of a row
        somebody is trying to click.
      */}
      <ResizeHandle
        label="Resize the details panel"
        size={width}
        min={DRAWER.MIN}
        max={drawerCeiling()}
        direction={-1}
        onSize={setWidth}
        onReset={() => setWidth(DRAWER.DEFAULT)}
        onDragChange={setResizing}
        className="absolute inset-y-0 left-0 z-20 w-2"
      />
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

      {content.onOpenDetail && (
        <div>
          <button
            onClick={content.onOpenDetail}
            className="rounded border border-edge px-2 py-1 text-xs text-ink-dim hover:text-ink"
            title="Open the whole of this in a window of its own, with an export of every row"
          >
            Open in a window
          </button>
        </div>
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
            // MARKDOWN ONLY WHERE THE ROW SAYS SO. This one slot carries three different things:
            // a compaction summary and a chat's newest plan, which are documents, and a message's
            // full text, which is a document only when a person or Claude wrote it. 86.5% of the
            // records typed `user` are tool results (`c4x/theme.py` records the measurement), and
            // markdown over a directory listing eats its indentation and turns a `#` comment into
            // a heading, so anything else opens raw with the toggle one click away.
            <TextBody
              source={content.text}
              name={content.fileName ?? 'text'}
              boxClass="max-h-[60vh]"
              defaultView={content.textKind === 'markdown' ? 'markdown' : 'raw'}
            />
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

      {(content.rows ?? []).map((group, index) => (
        <section key={group.name} className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="text-2xs tracking-[0.06em] text-ink-faint">
              THE ROWS BEHIND IT: {group.name}, {group.table.rows.length.toLocaleString()}{' '}
              {group.table.rows.length === 1 ? 'row' : 'rows'}
            </h3>
            {/* The window button belongs to the FIRST list only: it opens the subject, and four
                buttons saying the same thing would each look like they opened a different one. */}
            {index === 0 && content.onOpen && (
              <button
                onClick={content.onOpen}
                className="rounded border border-edge px-2 py-0.5 text-2xs text-ink-dim hover:text-ink"
                title="Open these rows on a page of their own, in a new window"
              >
                Open in new window
              </button>
            )}
          </div>
          <DataTable table={group.table} meta={group.meta} title={group.name} />
        </section>
      ))}

      {!(content.rows ?? []).length && content.noRows && (
        <p className="text-2xs text-ink-faint">{content.noRows}</p>
      )}
    </div>
  )
}
