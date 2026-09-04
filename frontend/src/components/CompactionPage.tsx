import { useEffect, useState } from 'react'

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

/** The whole set, both sides, as CSV. The export is the full data, not what the page draws. */
export function csv(rows: Crossing[]): string {
  const cell = (v: unknown) => '"' + String(v ?? '').replace(/"/g, '""') + '"'
  const head = ['outcome', 'ts', 'role', 'type', 'chars', 'uuid', 'preview']
  const body = rows.map((r) =>
    [r.kept ? 'kept' : 'dropped', r.ts, r.role, r.type, r.chars, r.uuid, r.preview]
      .map(cell).join(','))
  return [head.join(','), ...body].join('\n')
}

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
  const shown = onlyKept ? rows.filter((r) => r.kept) : rows

  // A data: URL rather than a Blob object URL, for the reason the table exports already give:
  // both work, this one needs no revoke, so a page left open all afternoon accumulates nothing.
  const save = () => {
    const a = document.createElement('a')
    a.href = `data:text/csv;charset=utf-8,${encodeURIComponent(csv(rows))}`
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
              <pre
                className="max-h-[40vh] overflow-auto whitespace-pre-wrap rounded bg-page px-3 py-2
                           font-mono text-xs leading-relaxed text-ink"
              >
                {detail.summary.text}
              </pre>
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
            {/* A LOWER BOUND IN BOTH DIRECTIONS, said rather than left to be inferred from two
                counts that do not add up to the total. A survivor uuid the store never harvested
                cannot be shown on either side. */}
            <p className="mb-2 text-2xs text-ink-faint">
              {detail.kept_shown.toLocaleString()} kept and{' '}
              {detail.dropped_shown.toLocaleString()} dropped can be shown, of{' '}
              {detail.kept_total.toLocaleString()} and {detail.dropped_total.toLocaleString()}{' '}
              recorded. Both are lower bounds: a survivor this store never harvested cannot appear
              on either side.
            </p>
            <div className="max-h-[55vh] overflow-auto rounded border border-edge">
              {shown.map((row) => (
                <div
                  key={row.uuid}
                  data-outcome={row.kept ? 'kept' : 'dropped'}
                  className={`border-b border-edge/40 px-3 py-2 last:border-0 ${
                    row.kept ? 'border-l-2 border-l-good bg-good/5' : ''
                  }`}
                >
                  <div className="flex items-baseline gap-2 text-2xs">
                    <span className={row.kept ? 'font-semibold text-good' : 'text-ink-faint'}>
                      {row.kept ? 'KEPT' : 'dropped'}
                    </span>
                    <span className="tabular-nums text-ink-faint">
                      {row.chars.toLocaleString()} chars
                    </span>
                    <span className="text-ink-faint">{row.role}</span>
                    <span className="text-ink-faint">{row.type}</span>
                    <span className="ml-auto tabular-nums text-ink-faint">
                      {String(row.ts).slice(0, 19)}
                    </span>
                  </div>
                  <p className="mt-0.5 truncate font-mono text-2xs text-ink-dim" title={row.preview}>
                    {row.preview}
                  </p>
                </div>
              ))}
              {shown.length === 0 && (
                <p className="px-3 py-3 text-2xs text-ink-faint">
                  Nothing from before this boundary can be shown.
                </p>
              )}
            </div>
          </section>
        </>
      )}
    </main>
  )
}
