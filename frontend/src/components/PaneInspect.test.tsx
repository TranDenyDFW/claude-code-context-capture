/**
 * Clicking a chart, and opening a row: the pane opens the drawer with the right content.
 *
 * Plotly is stubbed as a button per figure that fires the click the real chart would, with the
 * point the test chooses; hydrate is stubbed so the message reader's fetch is observable.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { TabPayload } from '@/api'

vi.mock('./Plot', () => ({
  Plot: ({ onPointClick }: { onPointClick?: (p: unknown) => void }) => (
    <button
      data-testid="chart"
      onClick={() => onPointClick?.({
        traceName: 'sessions', x: 12, y: 990000, customdata: ['sess-1', 'title'], pointIndex: 0,
      })}
    >
      chart
    </button>
  ),
}))

vi.mock('./exporters', async (importOriginal) => {
  const real = await importOriginal<typeof import('./exporters')>()
  return {
    ...real,
    hydrate: vi.fn(async (sheet: { rows: Record<string, unknown>[] }) => ({
      ...sheet, rows: sheet.rows.map((r) => ({ ...r, preview: 'the whole message, every word of it' })),
    })),
  }
})

import { Pane } from './Pane'
import { hydrate } from './exporters'

function payload(over: Partial<TabPayload> = {}): TabPayload {
  return {
    tab: 'tab-sessions', session: null, scope: 'main', cohort: null,
    tables: [], figures: [], text: [], plotly: [], ...over,
  }
}

describe('clicking a point on a chart', () => {
  it('opens the drawer with the point, the rows that carry its value, and a way to open them', () => {
    const open = vi.fn()
    render(
      <Pane
        payload={payload({
          figures: [{ title: 'sessions by peak', traces: [] }],
          plotly: [{ data: [] }],
          tables: [{
            id: 'tbl-session', columns: ['session_id', 'title'],
            rows: [{ session_id: 'sess-1', title: 'the one' }, { session_id: 'sess-2', title: 'another' }],
          }],
          meta: [{ id: 'tbl-session', title: 'Sessions', columns: [], filterable: true, page_size: null }],
        })}
        onOpenTable={open}
      />,
    )
    fireEvent.click(screen.getByTestId('chart'))
    const dialog = screen.getByRole('dialog', { name: 'sess-1' })
    expect(dialog.textContent).toContain('sessions by peak')
    expect(dialog.textContent).toContain('990,000')
    expect(dialog.textContent).toContain('the one')
    expect(dialog.textContent).not.toContain('another')
    fireEvent.click(screen.getByText('Open in new window'))
    expect(open).toHaveBeenCalledWith(0, {
      query: 'sess-1', filter: { key: 'session_id', value: 'sess-1' },
    })
  })

  it('still opens for a point that identifies no row, with its fields alone', () => {
    render(
      <Pane payload={payload({ figures: [{ title: 'a curve', traces: [] }], plotly: [{ data: [] }] })} />,
    )
    fireEvent.click(screen.getByTestId('chart'))
    const dialog = screen.getByRole('dialog')
    expect(dialog.textContent).toContain('990,000')
    expect(screen.queryByText('Open in new window')).toBeNull()
  })
})

describe('opening a row of the messages table', () => {
  it('fetches the full text by the row key and shows it in the drawer', async () => {
    render(
      <Pane
        payload={payload({
          tables: [{ id: 'tbl-messages', columns: ['ts', 'role', 'preview'],
                     rows: [{ uuid: 'u1', ts: '10:00', role: 'user', preview: 'the whole' }] }],
          meta: [{
            id: 'tbl-messages', title: 'Messages', columns: [], filterable: true, page_size: null,
            full_text: { url: '/api/messages/text', key: 'uuid', column: 'preview', as: 'text' },
          }],
        })}
      />,
    )
    fireEvent.click(screen.getByRole('table').querySelector('tbody tr')!)
    await waitFor(() => expect(screen.getByText('the whole message, every word of it')).toBeTruthy())
    expect(hydrate).toHaveBeenCalledTimes(1)
    const dialog = screen.getByRole('dialog')
    expect(dialog.textContent).toContain('10:00')
    expect(dialog.textContent).toContain('user')
  })

  it('keeps the preview and says so when the fetch fails (gate can fail)', async () => {
    vi.mocked(hydrate).mockRejectedValueOnce(new Error('500'))
    render(
      <Pane
        payload={payload({
          tables: [{ id: 'tbl-messages', columns: ['preview'], rows: [{ uuid: 'u2', preview: 'only the preview' }] }],
          meta: [{
            id: 'tbl-messages', title: 'Messages', columns: [], filterable: true, page_size: null,
            full_text: { url: '/api/messages/text', key: 'uuid', column: 'preview', as: 'text' },
          }],
        })}
      />,
    )
    fireEvent.click(screen.getByRole('table').querySelector('tbody tr')!)
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('could not be fetched'))
    expect(screen.getByRole('dialog').textContent).toContain('only the preview')
  })
})
