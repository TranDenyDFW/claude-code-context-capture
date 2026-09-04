/**
 * The compacted information, reachable from the row that records it.
 *
 * The store has held it since the tab existed: `compactions.summary_uuid` joins to the summary
 * message, 12,000 to 17,000 characters of it. The Dash page has shown it on a row click through a
 * callback. This page drew the token counts, built no route and no handler, and printed a note
 * telling the reader to click a row and read the summary. A page that instructs a reader to do
 * something it does not implement is worse than one that says nothing.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { DataTable } from './DataTable'
import { useRowInspector } from './useRowInspector'
import { Inspector } from './Inspector'
import type { TabPayload } from '@/api'

const SUMMARY = 'This session is being continued from a previous conversation that ran out of context.'

const table = {
  id: 'tbl-compactions',
  columns: ['ts', 'pre_tokens', 'post_tokens'],
  rows: [{ ts: '2026-08-05', pre_tokens: 1028902, post_tokens: 88094, uuid: 'abc-123' }],
}
const meta = {
  id: 'tbl-compactions', title: 'Compactions', note: null, columns: [], filterable: false,
  page_size: null, absorbed: [], detail: { url: '/api/compaction', key: 'uuid' },
}

function Harness({ payload }: { payload: TabPayload }) {
  const reader = useRowInspector(payload, ['Compactions'])
  return (
    <>
      <DataTable table={table} meta={meta} title="Compactions" onOpenRow={reader.openRow(0)} />
      {reader.content && <Inspector content={reader.content} onClose={reader.close} />}
    </>
  )
}

const payload = (): TabPayload => ({
  tab: 'tab-compactions', session: null, scope: 'main', cohort: null,
  tables: [table], figures: [], text: [], plotly: [], meta: [meta],
})

beforeEach(() => vi.restoreAllMocks())
afterEach(() => vi.restoreAllMocks())

describe('a compaction row', () => {
  it('opens the summary that replaced the dropped context', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      json: async () => ({ summary: { text: SUMMARY, chars: 12847 }, dropped_total: 1236 }),
    })))
    render(<Harness payload={payload()} />)
    fireEvent.click(screen.getByText('2026-08-05').closest('tr')!)
    expect(await screen.findByText(new RegExp(SUMMARY.slice(0, 40)))).toBeTruthy()
    // The dropped count is a LOWER BOUND, computed by subtracting recorded survivors, and the
    // reader has to be told that rather than left to read it as a measurement.
    expect(screen.getByText(/1,236 messages were present.*lower bound/)).toBeTruthy()
  })

  it('says so when the store never harvested a summary, instead of showing nothing', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, json: async () => ({ summary: null, dropped_total: 0 }),
    })))
    render(<Harness payload={payload()} />)
    fireEvent.click(screen.getByText('2026-08-05').closest('tr')!)
    expect(await screen.findByText(/No summary message was harvested/)).toBeTruthy()
    // Nothing to qualify, so nothing is said.
    expect(screen.queryByText(/lower bound/)).toBeNull()
  })

  it('reports a failed fetch rather than leaving the drawer waiting forever', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })))
    render(<Harness payload={payload()} />)
    fireEvent.click(screen.getByText('2026-08-05').closest('tr')!)
    await waitFor(() =>
      expect(screen.getByText('the summary could not be fetched')).toBeTruthy())
  })
})
