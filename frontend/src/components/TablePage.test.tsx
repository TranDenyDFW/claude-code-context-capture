/**
 * One table on a page of its own: the same heading and the same table as the pane, nothing else,
 * seeded with the filter a link carries.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { TablePage } from './TablePage'
import type { TabPayload } from '@/api'

vi.mock('./Plot', () => ({ Plot: () => <div data-testid="chart" /> }))

const payload: TabPayload = {
  tab: 'tab-cost',
  session: null,
  scope: 'main',
  cohort: null,
  tables: [
    { id: 'tbl-reread', columns: ['target', 'reads'], rows: [{ target: 'a.json', reads: 3 }, { target: 'b.md', reads: 1 }] },
    { id: '(anonymous)', columns: ['x'], rows: [{ x: 1 }] },
  ],
  figures: [{ title: 'a chart', traces: [] }],
  text: ['prose that belongs to the tab'],
  plotly: [{ data: [] }],
  meta: [
    { id: 'tbl-reread', title: 'Files read repeatedly', note: 'why it matters', columns: [], filterable: true, page_size: null },
    { id: '(anonymous)', title: null, columns: [], filterable: true, page_size: null },
  ],
  population: 'the whole store, every session',
}

describe('the single-table page', () => {
  it('draws exactly the one table, under the same heading the pane uses, and nothing else', () => {
    render(<TablePage payload={payload} index={0} onBack={() => {}} />)
    const heading = screen.getByRole('heading', { level: 1 })
    expect(heading.textContent).toContain('Files read repeatedly')
    expect(heading.textContent).not.toContain('2 rows')
    expect(heading.getAttribute('title')).toBe('why it matters')
    expect(screen.queryByTestId('chart')).toBeNull()
    expect(screen.queryByText('prose that belongs to the tab')).toBeNull()
    expect(screen.getAllByRole('table')).toHaveLength(1)
    expect(document.title).toContain('Files read repeatedly')
  })

  it('seeds the search box with the filter the link carried, so the rows behind a click show first', () => {
    render(<TablePage payload={payload} index={0} query="b.md" onBack={() => {}} />)
    expect(screen.getByText('b.md')).toBeTruthy()
    expect(screen.queryByText('a.json')).toBeNull()
  })

  it('names a table the server could not, and offers the way back', () => {
    const back = vi.fn()
    render(<TablePage payload={payload} index={1} onBack={back} />)
    expect(screen.getByRole('heading', { level: 1 }).textContent).toContain('Table 2')
    fireEvent.click(screen.getByText('Back to the dashboard'))
    expect(back).toHaveBeenCalledTimes(1)
  })

  it('says so when the index names a table the tab does not have (gate can fail)', () => {
    render(<TablePage payload={payload} index={7} onBack={() => {}} />)
    expect(screen.getByRole('alert').textContent).toContain('no table 8')
  })

  it('has no open-in-window control of its own', () => {
    render(<TablePage payload={payload} index={0} onBack={() => {}} />)
    expect(screen.queryByRole('button', { name: /in a new window/ })).toBeNull()
  })
})

describe('the standalone page does everything the pane does with that table', () => {
  const messages: TabPayload = {
    tab: 'tab-session', session: 's1', scope: 'main', cohort: null,
    tables: [{ id: 'tbl-messages', columns: ['ts', 'preview'],
               rows: [{ uuid: 'u1', session_id: 's1', ts: '10:00', preview: 'cut short' }] }],
    figures: [], text: [], plotly: [],
    meta: [{
      id: 'tbl-messages', title: 'Messages', note: 'Click a row to read it in full.',
      columns: [], filterable: true, page_size: null,
      full_text: { url: '/api/messages/text', key: 'uuid', column: 'preview', as: 'text' },
    }],
    details: [{ summary: 'The query behind this table', body: ['SELECT 1'], table_index: 0 }],
  }

  it('opens a row in the reader, on the page whose own note promises it', async () => {
    render(<TablePage payload={messages} index={0} onBack={() => {}} />)
    expect(screen.getByRole('heading', { level: 1 }).getAttribute('title')).toContain('read it in full')
    fireEvent.click(screen.getByRole('table').querySelector('tbody tr')!)
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toContain('10:00')
  })

  it('never changes the header selection from a row: there is no header here to see it happen', () => {
    render(<TablePage payload={messages} index={0} onBack={() => {}} />)
    const row = screen.getByRole('table').querySelector('tbody tr')!
    expect(row.getAttribute('title')).not.toContain('select this session')
  })

  it('carries the query that built the table, which the first version dropped', () => {
    render(<TablePage payload={messages} index={0} onBack={() => {}} />)
    expect(screen.getByText('The query behind this table')).toBeTruthy()
  })

  it('applies an exact filter by a hidden key and says how many rows that left', () => {
    const two = {
      ...messages,
      tables: [{ ...messages.tables[0], rows: [
        { uuid: 'u1', session_id: 's1', ts: '10:00', preview: 'first' },
        { uuid: 'u2', session_id: 's2', ts: '11:00', preview: 'second' },
      ] }],
    }
    render(<TablePage payload={two} index={0} filter={{ key: 'session_id', value: 's2' }} onBack={() => {}} />)
    expect(screen.getByText(/Filtered to session_id = s2, 1 of 2 rows/)).toBeTruthy()
    // The heading names the table; the filter line above states what the filter left, and
    // the pager under the table states the count. The heading restates neither.
    const heading = screen.getByRole('heading', { level: 1 })
    expect(heading.textContent).toBe('Messages')
    expect(screen.getByText('11:00')).toBeTruthy()
    expect(screen.queryByText('10:00')).toBeNull()
  })

  it('reports the filter box upward, so the address can follow the screen', () => {
    const changes: string[] = []
    render(<TablePage payload={messages} index={0} onQueryChange={(q) => changes.push(q)} onBack={() => {}} />)
    fireEvent.change(screen.getByLabelText('Filter Messages'), { target: { value: 'cut' } })
    expect(changes).toEqual(['cut'])
  })
})

describe('selecting a session from the drawer, which is the only way to do it here', () => {
  const sessions: TabPayload = {
    tab: 'tab-sessions', session: null, scope: 'main', cohort: null,
    tables: [{ id: 'tbl-session', columns: ['title'],
               rows: [{ session_id: 's7', title: 'the one' }] }],
    figures: [], text: [], plotly: [],
    meta: [{ id: 'tbl-session', title: 'Sessions', columns: [], filterable: true, page_size: null }],
  }

  it('offers the button for a row that names a session, and closes as it fires', async () => {
    const picked: Record<string, unknown>[] = []
    render(<TablePage payload={sessions} index={0} onSelectSession={(r) => picked.push(r)} onBack={() => {}} />)
    fireEvent.click(screen.getByRole('table').querySelector('tbody tr')!)
    fireEvent.click(await screen.findByRole('button', { name: 'Select this session' }))
    expect(picked).toEqual([{ session_id: 's7', title: 'the one' }])
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('offers no such button where the row names no session (gate can fail)', async () => {
    render(<TablePage payload={payload} index={0} onSelectSession={() => {}} onBack={() => {}} />)
    fireEvent.click(screen.getByRole('table').querySelector('tbody tr')!)
    await screen.findByRole('dialog')
    expect(screen.queryByRole('button', { name: 'Select this session' })).toBeNull()
  })
})

describe('what the standalone page follows and what it shows', () => {
  it('follows the filter the address moves to, not only the one it opened with', () => {
    const { rerender } = render(<TablePage payload={payload} index={0} query="b.md" onBack={() => {}} />)
    expect(screen.getByText('b.md')).toBeTruthy()
    expect(screen.queryByText('a.json')).toBeNull()
    rerender(<TablePage payload={payload} index={0} query="a.json" onBack={() => {}} />)
    expect(screen.getByText('a.json')).toBeTruthy()
    expect((screen.getByLabelText('Filter Files read repeatedly') as HTMLInputElement).value)
      .toBe('a.json')
  })

  it('puts the query under the table this page IS, not under the first one', () => {
    const two: TabPayload = {
      ...payload,
      details: [
        { summary: 'The query behind the first table', body: ['SELECT 1'], table_index: 0 },
        { summary: 'The query behind the second table', body: ['SELECT 2'], table_index: 1 },
      ],
    }
    render(<TablePage payload={two} index={1} onBack={() => {}} />)
    expect(screen.getByText('The query behind the second table')).toBeTruthy()
    expect(screen.queryByText('The query behind the first table')).toBeNull()
  })
})
