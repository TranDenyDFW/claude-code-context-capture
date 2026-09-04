/**
 * The three things that open in a window of their own, and the state that gets them back.
 *
 * An independent review found 270 lines of new UI here with no test at all, two "Window" buttons
 * that navigated in place while the third opened a window, a `figureUrl` exported and called by
 * nothing, and a popstate handler that restored the table view and neither of the two added after
 * it. Every one of those is a thing a test would have said out loud.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { EMPTY, compactionUrl, figureUrl, fromSearch, toSearch, type ViewState } from '@/state'
import { CompactionPage, crossings, csv, type CompactionDetail } from './CompactionPage'
import { FigurePage } from './FigurePage'
import type { TabPayload } from '@/api'

vi.mock('./Plot', () => ({ Plot: () => <div data-testid="chart" /> }))

const base: ViewState = { ...EMPTY, tab: 'tab-cost' }

describe('an address for one thing', () => {
  it('round trips a chart view, and drops the other single-thing fields', () => {
    const url = figureUrl({ ...base, table: 4, query: 'x' }, 2)
    const back = fromSearch(new URL(url).search)
    expect(back.view).toBe('figure')
    expect(back.figure).toBe(2)
    // One state, one address: a table index left behind would make two addresses for one screen.
    expect(back.table).toBeNull()
    expect(back.query).toBe('')
  })

  it('round trips a compaction view, keyed by uuid rather than an index', () => {
    const url = compactionUrl(base, 'abc-123')
    const back = fromSearch(new URL(url).search)
    expect(back.view).toBe('compaction')
    expect(back.compaction).toBe('abc-123')
    expect(back.figure).toBeNull()
  })

  it('refuses a figure index that is not a whole number the app could hold', () => {
    for (const bad of ['-1', '1.5', '99999999999999999999', 'abc']) {
      expect(fromSearch(`?tab=t&view=figure&figure=${bad}`).view).toBe('dashboard')
    }
  })

  it('keeps every single-thing field out of a dashboard address', () => {
    const search = toSearch({ ...base, view: 'dashboard', figure: 3, compaction: 'x', table: 1 })
    expect(search).not.toContain('figure')
    expect(search).not.toContain('compaction')
    expect(search).not.toContain('table')
  })
})

describe('the single-chart page', () => {
  const payload = (over: Partial<TabPayload> = {}): TabPayload => ({
    tab: 'tab-cost', session: null, scope: 'main', cohort: null, tables: [],
    figures: [{ title: 'Reread Concentration', traces: [] }],
    text: [], plotly: [{ data: [], layout: {} }],
    figure_meta: [{ note: 'Across 1,192 groups.', absorbed: [] }],
    ...over,
  })

  it('names the chart and carries its caption on the hover', () => {
    render(<FigurePage payload={payload()} index={0} onBack={() => {}} />)
    const heading = screen.getByRole('heading', { level: 1 })
    expect(heading.textContent).toBe('Reread Concentration')
    expect(heading.getAttribute('title')).toBe('Across 1,192 groups.')
  })

  it('says so, with a way back, when the address names a chart the tab does not have', () => {
    render(<FigurePage payload={payload()} index={7} onBack={() => {}} />)
    expect(screen.getByRole('alert').textContent).toContain('has no chart 8')
    expect(screen.getByText('Back to the dashboard')).toBeTruthy()
  })
})

describe('a compaction, ordered by size with what survived marked', () => {
  const detail: CompactionDetail = {
    uuid: 'c-1',
    summary: { text: 'This session is being continued', chars: 12847, ts: '2026-08-05' },
    kept: [{ uuid: 'k1', ts: '10:00', role: 'user', type: 'tool_result', chars: 500, preview: 'kept one' }],
    dropped: [
      { uuid: 'd1', ts: '09:00', role: 'user', type: 'typed', chars: 900, preview: 'dropped big' },
      { uuid: 'd2', ts: '08:00', role: 'user', type: 'typed', chars: 100, preview: 'dropped small' },
    ],
    kept_recorded: 695, kept_total: 268, kept_shown: 1,
    dropped_total: 22346, dropped_shown: 2,
  }

  it('interleaves both sides into one list, largest first', () => {
    expect(crossings(detail).map((r) => [r.chars, r.kept]))
      .toEqual([[900, false], [500, true], [100, false]])
  })

  it('exports both sides with the outcome as a column', () => {
    const text = csv(crossings(detail))
    expect(text.split('\n')[0]).toBe('outcome,ts,role,type,chars,uuid,preview')
    expect(text).toContain('"kept"')
    expect(text).toContain('"dropped"')
    expect(text.split('\n')).toHaveLength(4)
  })

  it('quotes a preview that contains a comma or a quote, so the columns do not shift', () => {
    const text = csv([{ ...detail.dropped[0], kept: false, preview: 'a,b "c"' }])
    expect(text).toContain('"a,b ""c"""')
  })

  beforeEach(() => vi.restoreAllMocks())
  afterEach(() => vi.restoreAllMocks())

  it('states the three counts under the words they actually mean', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => detail })))
    render(<CompactionPage uuid="c-1" onBack={() => {}} />)
    // It printed "270 kept of 270 recorded" while the boundary recorded 697 and only 268 of them
    // are messages this store holds. A count under the wrong word is a wrong number.
    const line = await screen.findByText(/recorded 695 survivors/)
    expect(line.textContent).toContain('268 are messages this store holds')
    expect(line.textContent).toContain('dropped at least 22,346')
    // The list is capped, and says so rather than implying it is everything.
    expect(line.textContent).toMatch(/largest, not every\s+row/)
  })

  it('marks the survivors and can narrow to them', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => detail })))
    const { container } = render(<CompactionPage uuid="c-1" onBack={() => {}} />)
    await screen.findByText(/recorded 695 survivors/)
    expect(container.querySelectorAll('[data-outcome="kept"]')).toHaveLength(1)
    expect(container.querySelectorAll('[data-outcome="dropped"]')).toHaveLength(2)
    fireEvent.click(screen.getByLabelText('Only what it kept'))
    expect(container.querySelectorAll('[data-outcome="dropped"]')).toHaveLength(0)
    expect(container.querySelectorAll('[data-outcome="kept"]')).toHaveLength(1)
  })

  it('reports a failed fetch instead of waiting forever', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })))
    render(<CompactionPage uuid="c-1" onBack={() => {}} />)
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toContain('could not be fetched'))
  })
})
