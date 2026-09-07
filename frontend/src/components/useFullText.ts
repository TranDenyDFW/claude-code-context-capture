import { useEffect, useRef, useState } from 'react'
import type { TableMeta } from '@/api'

/**
 * The whole of a column the server had to cut, fetched once, so the search box can see it.
 *
 * WHAT THE SEARCH COULD SEE BEFORE. The Messages table ships `substr(text, 1, 220)`. Measured on
 * this store: 205,775 of 330,857 messages are longer than that, and the mean message is 2,313
 * characters, so typing a word into the search box searched about a tenth of the average message
 * and silently reported no match for the other nine. The reader has no way to tell that apart from
 * "the word is not there", which is the failure that matters: a search that quietly under-reports
 * is worse than one that refuses, because the empty result looks like an answer.
 *
 * ON DEMAND, NOT ON RENDER. Hydrating every table on load would fetch the full text of 400 messages
 * for a reader who is going to sort a column and leave. Nothing is fetched until the first
 * keystroke, and then only once per table: the rows do not change under the search box, so a second
 * fetch would return the same bytes. The route caps a request at 1,000 keys, so a table with
 * more rows than that is fetched in batches rather than cut to fit.
 *
 * WHAT THIS DOES NOT FIX, stated because the number is easy to mistake for coverage. The server
 * caps the rows it renders, and this reaches only the rows the browser was given. On this store 62
 * of 1,352 sessions hold more than 400 messages, and in those the search still cannot see past the
 * cap. The table already states its own cap in its note; what this hook restores is the TEXT, not the
 * row count, and the caller is the one that says which of the two it is talking about.
 */
export type FullTextState = {
  /** key -> the whole value, for the rows that have one. Empty until a fetch lands. */
  text: Map<string, string>
  /** Which column those values replace, or null when this table declares none. */
  column: string | null
  /** A fetch is in flight. The search still runs, against the previews, and says so. */
  loading: boolean
  /** The fetch failed. The search falls back to the previews rather than showing nothing. */
  failed: boolean
}

const EMPTY: FullTextState = { text: new Map(), column: null, loading: false, failed: false }

export function useFullText(
  rows: Record<string, unknown>[],
  meta: TableMeta | undefined,
  wanted: boolean,
  fetcher: typeof fetch = fetch,
): FullTextState {
  const spec = meta?.full_text
  const [state, setState] = useState<FullTextState>(EMPTY)
  // The rows this state describes. A table swapped under the same component must not keep the
  // previous table's text, which would match rows that no longer exist.
  const hydratedFor = useRef<Record<string, unknown>[] | null>(null)

  useEffect(() => {
    if (!spec || !wanted) return
    if (hydratedFor.current === rows) return
    const keys = [...new Set(rows
      .map((row) => row[spec.key])
      .filter((k): k is string => typeof k === 'string' && k !== ''))]
    if (!keys.length) return
    hydratedFor.current = rows
    let mine = true
    setState({ text: new Map(), column: spec.column, loading: true, failed: false })
    // IN BATCHES, because the route answers 422 past 1,000 keys and the table can now deliver
    // 2,000 rows. Slicing to the first 1,000 instead would leave the rest searchable only by
    // their previews, silently, which is the exact defect this hook exists to remove.
    const batches: string[][] = []
    for (let at = 0; at < keys.length; at += 1000) batches.push(keys.slice(at, at + 1000))
    Promise.all(batches.map((batch) =>
      fetcher(spec.url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ [`${spec.key}s`]: batch }),
      }).then((response) => {
        if (!response.ok) throw new Error(String(response.status))
        return response.json() as Promise<Record<string, string>>
      })))
      .then((parts) => {
        if (!mine) return
        const text = new Map<string, string>()
        for (const part of parts) for (const [k, v] of Object.entries(part)) text.set(k, v)
        setState({ text, column: spec.column, loading: false, failed: false })
      })
      .catch(() => {
        if (!mine) return
        // ALL OR NOTHING, and reported. A partial hydrate would search some rows fully and others
        // to 220 characters with one status line covering both, which is a narrower search
        // wearing the appearance of a complete one. The rows are still searchable against their
        // previews, so this narrows the search rather than breaking it, and it says so.
        hydratedFor.current = null
        setState({ text: new Map(), column: spec.column, loading: false, failed: true })
      })
    return () => { mine = false }
  }, [rows, spec, wanted, fetcher])

  return spec ? state : EMPTY
}
