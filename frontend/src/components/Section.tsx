import { useState } from 'react'
import type { Section as SectionData, Table, TableMeta } from '@/api'
import { hydrate } from './exporters'

/**
 * A collapsible section under a table, which in this app almost always holds the SQL that produced
 * it.
 *
 * "Every table carries the query that built it" is a feature of this tool, not decoration: it is
 * what lets a reader check a number rather than believe it. It survives the migration with the
 * query attributed to its own table by the server, and with the two things a reader actually wants
 * to do with it: copy the query, and take the rows away as CSV.
 */

function looksLikeQuery(body: string[]): boolean {
  return body.some((line) => /^\s*(SELECT|WITH)\b/i.test(line))
}

/** RFC 4180 enough to survive a value containing a comma, a quote or a newline. */
function toCsv(table: Table): string {
  const escape = (value: unknown) => {
    if (value === null || value === undefined) return ''
    const text = String(value)
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
  }
  const lines = [table.columns.join(',')]
  for (const row of table.rows) lines.push(table.columns.map((c) => escape(row[c])).join(','))
  return lines.join('\r\n')
}

function Copy({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
          setDone(true)
          setTimeout(() => setDone(false), 1500)
        } catch {
          // Clipboard access can be refused, and a button that silently does nothing is worse than
          // one that says it could not. The text is on screen either way.
          setDone(false)
        }
      }}
      className="rounded border border-edge px-2 py-1 text-xs text-ink-dim hover:text-ink"
    >
      {done ? 'copied' : label}
    </button>
  )
}

export function Section({
  section,
  table,
  meta,
}: {
  section: SectionData
  table?: Table
  meta?: TableMeta
}) {
  const body = section.body.join('\n')
  const query = looksLikeQuery(section.body)
  // A failed fetch of the full text is said out loud, here, next to the button that failed. The
  // toolbar's exports already did this; this button dropped the rejection on the floor and
  // exported nothing with no message, which reads as a button that does not work.
  const [problem, setProblem] = useState<string | null>(null)
  return (
    <details className="group rounded-lg bg-panel shadow-panel">
      <summary
        className="cursor-pointer list-none px-3 py-2 text-sm text-ink-dim
                   marker:content-none hover:text-ink"
      >
        <span className="mr-1.5 inline-block transition-transform group-open:rotate-90">›</span>
        {section.summary || (query ? 'The query behind this table' : 'More')}
      </summary>
      {body && (
        <div className="border-t border-edge px-3 py-2">
          <div className="mb-2 flex gap-2">
            <Copy text={body} label={query ? 'copy query' : 'copy'} />
            {table && table.rows.length > 0 && (
              <button
                onClick={async () => {
                  // The full text where a column was cut, fetched once for the file; then a data:
                  // URL rather than a Blob object URL. Both work; this one needs no revoke, so a
                  // page left open for an afternoon does not accumulate them.
                  let rows = table.rows
                  if (meta?.full_text) {
                    const columns = table.columns.map((id) => ({
                      id, label: id, numeric: false, specifier: null, align: 'left' as const,
                      hidden: false, bands: [],
                    }))
                    try {
                      const full = await hydrate({
                        columns, rows, name: table.id ?? 'table', format: (v) => String(v ?? ''),
                        fullText: meta.full_text,
                      })
                      rows = full.rows
                    } catch {
                      // Said for as long as the toolbar says its messages, then gone: a message
                      // that outlives the moment reads as a permanent state of the button.
                      setProblem('Could not fetch the full text; nothing was exported')
                      setTimeout(() => setProblem(null), 1800)
                      return
                    }
                  }
                  setProblem(null)
                  const link = document.createElement('a')
                  link.href = `data:text/csv;charset=utf-8,${encodeURIComponent(toCsv({ ...table, rows }))}`
                  link.download = `${table.id && table.id !== '(anonymous)' ? table.id : 'table'}.csv`
                  link.click()
                }}
                className="rounded border border-edge px-2 py-1 text-xs text-ink-dim
                           hover:text-ink"
              >
                export {table.rows.length.toLocaleString()} {table.rows.length === 1 ? 'row' : 'rows'} as CSV
              </button>
            )}
            {problem && (
              <span role="alert" className="self-center text-2xs text-ink-dim">
                {problem}
              </span>
            )}
          </div>
          <pre
            className="overflow-auto rounded bg-page px-3 py-2 font-mono text-xs
                       leading-relaxed whitespace-pre-wrap text-ink-dim"
          >
            {body}
          </pre>
        </div>
      )}
    </details>
  )
}

export { toCsv }
