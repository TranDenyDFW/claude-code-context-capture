/**
 * The search box searches the whole message, not the fifth of it that fits in a cell.
 *
 * The Messages table ships `substr(text, 1, 220)`. Measured on this store: 205,775 of 330,857
 * messages are longer than that, and the mean message is 2,313 characters, so the box searched
 * about a tenth of the average message and reported nothing for the other nine. A search that
 * quietly under-reports is worse than one that refuses, because the empty result looks like an
 * answer.
 *
 * The two states where it still under-reports are the two that have to be legible, so they are
 * tested as hard as the fix: the fetch in flight, and the fetch that failed.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { Table, TableMeta } from '@/api'
import { DataTable } from './DataTable'

const PREVIEW = 'Target confirmed present at 75 bytes. Plan written'
const WHOLE = `${PREVIEW} and the word ONLYINFULL appears well past the cut.`

const table: Table = {
  id: 'tbl-messages',
  columns: ['ts', 'preview'],
  rows: [
    { uuid: 'u1', ts: '2026-09-07 05:01:47', preview: PREVIEW },
    { uuid: 'u2', ts: '2026-09-07 05:01:51', preview: 'Something else entirely' },
  ],
}

const meta: TableMeta = {
  id: 'tbl-messages', title: 'Messages', columns: [], filterable: true, page_size: null,
  full_text: { url: '/api/messages/text', key: 'uuid', column: 'preview', as: 'text' },
}

function search(text: string) {
  // The exact label. Each COLUMN has a 'Filter <column>' box of its own, so a prefix match finds
  // all of them, and the table's own box is named for the sheet, which falls back to the table id.
  fireEvent.change(screen.getByLabelText('Filter tbl-messages'), { target: { value: text } })
}

// The tbody only. A DataTable's thead holds TWO rows, the headings and the per-column filter
// boxes, so counting every role=row and subtracting one is off by one in the direction that
// makes a failing filter look like it matched.
const bodyRows = () => document.querySelectorAll('tbody tr').length

beforeEach(() => { vi.restoreAllMocks() })

describe('searching a table whose text the server had to cut', () => {
  it('finds a word that is only in the part the cell does not show', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ u1: WHOLE, u2: 'Something else entirely' }),
      { headers: { 'content-type': 'application/json' } })))
    render(<DataTable table={table} meta={meta} />)
    search('ONLYINFULL')
    await waitFor(() => expect(bodyRows()).toBe(1))
    expect(screen.getByText(PREVIEW)).toBeTruthy()
  })

  it('fetches nothing until somebody actually searches (gate can fail)', () => {
    // Hydrating on render would fetch the full text of every message for a reader who is going to
    // sort a column and leave.
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    render(<DataTable table={table} meta={meta} />)
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('fetches once, not once per keystroke', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ u1: WHOLE }),
      { headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetcher)
    render(<DataTable table={table} meta={meta} />)
    search('O'); search('ON'); search('ONL')
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
  })

  it('still searches the previews while the fetch is in flight, and says so', async () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))   // never settles
    render(<DataTable table={table} meta={meta} />)
    search('Target confirmed')
    await waitFor(() => expect(screen.getByRole('status').textContent)
      .toContain('searching previews while the full text loads'))
    expect(bodyRows()).toBe(1)
  })

  it('says so when the fetch fails, rather than reporting a narrower search as an answer', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('nope', { status: 500 })))
    render(<DataTable table={table} meta={meta} />)
    search('Target confirmed')
    await waitFor(() => expect(screen.getByRole('status').textContent)
      .toContain('the full text could not be loaded'))
    // Narrowed, not broken: the preview match still stands.
    expect(bodyRows()).toBe(1)
  })

  it('says nothing extra once the full text is in hand', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ u1: WHOLE }),
      { headers: { 'content-type': 'application/json' } })))
    render(<DataTable table={table} meta={meta} />)
    search('ONLYINFULL')
    await waitFor(() => expect(bodyRows()).toBe(1))
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('leaves a table that declares no cut column alone (gate can fail)', () => {
    // THE NEGATIVE CONTROL. Without it, a hook that fetched for every table would pass everything
    // above.
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    const plain: TableMeta = { ...meta, full_text: undefined }
    render(<DataTable table={table} meta={plain} />)
    search('Target')
    expect(fetcher).not.toHaveBeenCalled()
    expect(bodyRows()).toBe(1)
  })

  it('does not match a row whose full text lacks the word', async () => {
    // The fix must not turn the search into "match everything once hydrated".
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ u1: WHOLE, u2: 'Something else entirely' }),
      { headers: { 'content-type': 'application/json' } })))
    render(<DataTable table={table} meta={meta} />)
    search('ONLYINFULL')
    await waitFor(() => expect(bodyRows()).toBe(1))
    expect(screen.queryByText('Something else entirely')).toBeNull()
  })
})
