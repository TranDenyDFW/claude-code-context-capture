import { useEffect, useState } from 'react'
import { TextBody } from './Markdown'
import { matches } from './Palette'
import { ResizeHandle } from './ResizeHandle'
import { useColumnWidths } from './useColumnWidths'

/** One message from before a boundary, and whether it crossed it. */
export interface Crossing {
  uuid: string
  ts: string
  role: string
  type: string
  chars: number
  preview: string
  kept: boolean
}

export interface CompactionDetail {
  uuid: string
  summary: { text: string; chars: number; ts: string } | null
  kept: Omit<Crossing, 'kept'>[]
  dropped: Omit<Crossing, 'kept'>[]
  /** What the boundary RECORDED, which is the number its own row reports. */
  kept_recorded: number
  /** Of those, how many are messages this store actually holds. Not the same number. */
  kept_total: number
  kept_shown: number
  dropped_total: number
  dropped_shown: number
}

/** Every message before the boundary, largest first, each marked with what happened to it. */
export function crossings(detail: CompactionDetail): Crossing[] {
  const rows = [
    ...(detail.kept ?? []).map((r) => ({ ...r, kept: true })),
    ...(detail.dropped ?? []).map((r) => ({ ...r, kept: false })),
  ]
  return rows.sort((a, b) => b.chars - a.chars)
}

/**
 * Both sides as CSV, and it carries what was FETCHED rather than what the page draws.
 *
 * Not "the full data", which is what this claimed while the route caps at 5,000 rows and one
 * boundary here dropped 22,346. The page states the cap beside the list; the file cannot, so the
 * claim had to go rather than the number.
 */
export function csv(rows: Crossing[]): string {
  const cell = (v: unknown) => '"' + String(v ?? '').replace(/"/g, '""') + '"'
  const head = ['outcome', 'ts', 'role', 'type', 'chars', 'uuid', 'preview']
  const body = rows.map((r) =>
    [r.kept ? 'kept' : 'dropped', r.ts, r.role, r.type, r.chars, r.uuid, r.preview]
      .map(cell).join(','))
  return [head.join(','), ...body].join('\n')
}

const crossingCell = 'px-2 py-1.5 align-top truncate'

/**
 * The six columns, declared once so the header, the colgroup and the widths agree.
 *
 * `align` is the same word `ColumnMeta` uses, because these go straight into `useColumnWidths`,
 * which is the main table's own width machinery: the estimate reads every row, so the widths hold
 * still while the search box narrows the list, and each edge can be dragged.
 */
export const CROSSING_COLUMNS = [
  { id: 'outcome', label: 'Outcome', numeric: false, specifier: null, align: 'left' as const, hidden: false, bands: [] },
  { id: 'chars', label: 'Chars', numeric: true, specifier: ',', align: 'right' as const, hidden: false, bands: [] },
  { id: 'role', label: 'Role', numeric: false, specifier: null, align: 'left' as const, hidden: false, bands: [] },
  { id: 'type', label: 'Type', numeric: false, specifier: null, align: 'left' as const, hidden: false, bands: [] },
  { id: 'ts', label: 'Date and Time', numeric: false, specifier: null, align: 'left' as const, hidden: false, bands: [] },
  // The one column long enough to push the others off the screen: 220 characters, capped the same
  // way the server caps the widest column of every other table.
  { id: 'preview', label: 'Message', numeric: false, specifier: null, align: 'left' as const, hidden: false, bands: [], wide: true },
]

/**
 * One compaction, in a window of its own: what it wrote, and what it did to everything before it.
 *
 * A boundary is TWO documents and the drawer can only show one at a time. The summary runs 12,000
 * to 17,000 characters and the messages it replaced are hundreds of rows, so the drawer shows
 * enough to decide whether to come here, and this is where the whole thing is.
 *
 * KEPT IS HIGHLIGHTED IN PLACE RATHER THAN LISTED SEPARATELY. Two lists answer "what stayed" and
 * "what went" independently. One list ordered by size answers the question actually worth asking,
 * which is why the boundary kept what it kept, and that is only visible with both in one column.
 */
export function CompactionPage({
  uuid,
  onBack,
}: {
  uuid: string
  onBack: () => void
}) {
  const [detail, setDetail] = useState<CompactionDetail | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [onlyKept, setOnlyKept] = useState(false)
  const [needle, setNeedle] = useState('')

  useEffect(() => {
    let live = true
    setDetail(null)
    setProblem(null)
    fetch(`/api/compaction/${encodeURIComponent(uuid)}?limit=5000`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((body) => { if (live) setDetail(body) })
      .catch(() => { if (live) setProblem('This compaction could not be fetched.') })
    return () => { live = false }
  }, [uuid])

  useEffect(() => {
    const before = document.title
    document.title = `Compaction ${uuid.slice(0, 8)} · C4X`
    return () => { document.title = before }
  }, [uuid])

  const rows = detail ? crossings(detail) : []
  // TWO FILTERS, ONE LIST, AND THE EXPORT FOLLOWS BOTH. The checkbox used to narrow the view while
  // the CSV carried everything, so the file never matched the screen it was saved from.
  //
  // WHAT THE SEARCH CAN SEE is the 220-character preview, which is all the server sends and all
  // the page shows; the line under the box says so, because a search that quietly reports "no
  // match" for a word plainly in the message is worse than one that says what it read.
  const kept = onlyKept ? rows.filter((r) => r.kept) : rows
  const shown = needle.trim()
    ? kept.filter((r) => matches(`${r.preview} ${r.role} ${r.type} ${r.ts}`, needle))
    : kept
  const widths = useColumnWidths({
    tableId: 'tbl-crossings',
    columns: CROSSING_COLUMNS,
    rows: rows as unknown as Record<string, unknown>[],
    format: (value) => (value === null || value === undefined ? '' : String(value)),
  })

  // A data: URL rather than a Blob object URL, for the reason the table exports already give:
  // both work, this one needs no revoke, so a page left open all afternoon accumulates nothing.
  const save = () => {
    const a = document.createElement('a')
    a.href = `data:text/csv;charset=utf-8,${encodeURIComponent(csv(shown))}`
    a.download = `compaction-${uuid.slice(0, 8)}.csv`
    a.click()
  }

  return (
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-4 px-6 py-5">
      <div className="flex items-baseline justify-between gap-4">
        <h1 className="text-md font-semibold text-ink-dim">Compaction {uuid.slice(0, 8)}</h1>
        <div className="flex shrink-0 items-center gap-3">
          {detail && rows.length > 0 && (
            <button
              onClick={save}
              className="rounded border border-edge px-2 py-0.5 text-2xs text-ink-dim hover:text-ink"
            >
              Export CSV
            </button>
          )}
          <button onClick={onBack} className="text-sm text-accent hover:underline">
            Back to the dashboard
          </button>
        </div>
      </div>

      {problem && <p role="alert" className="text-sm text-ink-dim">{problem}</p>}
      {!detail && !problem && <p className="text-sm text-ink-faint">Fetching this compaction</p>}

      {detail && (
        <>
          <section className="rounded-lg bg-panel p-4 shadow-panel">
            <h2 className="mb-2 text-sm font-semibold text-ink-dim">
              The summary it wrote
              {detail.summary && (
                <span className="ml-2 text-2xs font-normal tabular-nums text-ink-faint">
                  {detail.summary.chars.toLocaleString()} chars
                </span>
              )}
            </h2>
            {detail.summary ? (
              // RENDERED, because this IS markdown: Claude Code's compactor writes headings, lists
              // and fenced code, and 12,000 to 17,000 characters of it read as one wall in a `pre`.
              // Raw is one click away and is the same `pre` byte for byte, and both exports carry
              // the source rather than what is on screen.
              <TextBody
                source={detail.summary.text}
                name={`compaction-${uuid.slice(0, 8)}-summary`}
                boxClass="max-h-[40vh]"
              />
            ) : (
              <p className="text-xs text-ink-faint">
                No summary message was harvested for this compaction. Older boundaries record token
                counts only.
              </p>
            )}
          </section>

          <section className="rounded-lg bg-panel p-4 shadow-panel">
            <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-sm font-semibold text-ink-dim">
                What was in the window before it, largest first
              </h2>
              <label className="flex items-center gap-1.5 text-2xs text-ink-faint">
                <input
                  type="checkbox"
                  checked={onlyKept}
                  onChange={(e) => setOnlyKept(e.target.checked)}
                />
                Only what it kept
              </label>
            </div>
            {/* THREE NUMBERS PER SIDE, AND THEY ARE NOT INTERCHANGEABLE. This printed
                "270 kept of 270 recorded" while the boundary recorded 697 and only 268 of them
                are messages the store holds. A count under the wrong word is a wrong number. */}
            <p className="mb-2 text-2xs text-ink-faint">
              Showing {shown.length.toLocaleString()} of{' '}
              {(detail.kept_shown + detail.dropped_shown).toLocaleString()} fetched.
              {' '}This boundary recorded {detail.kept_recorded.toLocaleString()} survivors, of
              which {detail.kept_total.toLocaleString()} are messages this store holds; it dropped
              at least {detail.dropped_total.toLocaleString()}, of which{' '}
              {detail.dropped_shown.toLocaleString()} were fetched. A survivor this store never
              harvested cannot appear on either side, so both are lower bounds.
              {detail.dropped_shown < detail.dropped_total && (
                <> The list and the export carry the{' '}
                  {(detail.kept_shown + detail.dropped_shown).toLocaleString()} largest, not every
                  row.</>
              )}
            </p>
            {/*
              COLUMNS, BECAUSE THIS IS A TABLE. It was a stack of divs with the timestamp pushed
              right by a margin and the message on a line of its own, which is two columns drawn as
              one and neither of them alignable. The user asked for the text and the date to be
              split, so each is now a cell, the widths hold still when the search narrows the list,
              and every one of them can be dragged (`useColumnWidths`, shared with the main table).

              KEPT STAYS GREEN. The tint and the left border are the fastest way to read which
              messages crossed the boundary, so the row keeps them rather than relying on a word
              in a column.
            */}
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <input
                type="search"
                value={needle}
                onChange={(event) => setNeedle(event.target.value)}
                aria-label="Search the messages before this boundary"
                placeholder="Search these messages"
                className="w-72 rounded border border-edge bg-page px-2 py-1 text-2xs text-ink
                           outline-none placeholder:text-ink-faint focus:border-accent"
              />
              <span className="text-2xs text-ink-faint">
                {needle.trim()
                  ? `${shown.length.toLocaleString()} of ${kept.length.toLocaleString()} match`
                  : 'Searches the first 220 characters of each message, which is what the server sends'}
              </span>
            </div>
            <div ref={widths.containerRef} className="max-h-[55vh] overflow-auto rounded border border-edge">
              <table className="table-fixed border-collapse text-2xs" style={{ width: widths.total }}>
                <colgroup>
                  {CROSSING_COLUMNS.map((column) => (
                    <col key={column.id} style={{ width: widths.width(column.id) }} />
                  ))}
                </colgroup>
                <thead className="sticky top-0 z-10 bg-panel">
                  <tr>
                    {CROSSING_COLUMNS.map((column) => (
                      <th
                        key={column.id}
                        scope="col"
                        className={`relative border-b border-edge px-2 py-1.5 text-left font-medium
                                    text-ink-faint ${column.align === 'right' ? 'text-right' : ''}`}
                      >
                        {column.label}
                        <ResizeHandle
                          label={`Resize the ${column.label} column`}
                          size={widths.width(column.id)}
                          min={widths.boundsFor(column).min}
                          max={widths.boundsFor(column).max}
                          onSize={(px) => widths.setWidth(column.id, px)}
                          onReset={() => widths.reset(column.id)}
                          className="absolute inset-y-0 -right-1 z-20 w-2"
                        />
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {shown.map((row) => (
                    <tr
                      key={row.uuid}
                      data-outcome={row.kept ? 'kept' : 'dropped'}
                      className={`border-b border-edge/40 ${
                        row.kept ? 'border-l-2 border-l-good bg-good/5' : ''
                      }`}
                    >
                      <td className={`${crossingCell} ${row.kept ? 'font-semibold text-good' : 'text-ink-faint'}`}>
                        {row.kept ? 'KEPT' : 'dropped'}
                      </td>
                      <td className={`${crossingCell} text-right tabular-nums text-ink-faint`}>
                        {row.chars.toLocaleString()}
                      </td>
                      <td className={`${crossingCell} text-ink-faint`}>{row.role}</td>
                      <td className={`${crossingCell} text-ink-faint`}>{row.type}</td>
                      <td className={`${crossingCell} tabular-nums text-ink-faint`}>
                        {String(row.ts).slice(0, 19)}
                      </td>
                      <td className={`${crossingCell} font-mono text-ink-dim`} title={row.preview}>
                        {row.preview}
                      </td>
                    </tr>
                  ))}
                  {shown.length === 0 && (
                    <tr>
                      <td colSpan={CROSSING_COLUMNS.length} className="px-3 py-3 text-2xs text-ink-faint">
                        {needle
                          ? 'Nothing from before this boundary matches that search.'
                          : 'Nothing from before this boundary can be shown.'}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </main>
  )
}
