/**
 * The section's CSV button and the full text it must fetch first.
 *
 * The toolbar's five export paths say "Could not fetch the full text" when the fetch fails. This
 * button awaited the same fetch with no handler, so a failure exported nothing, said nothing, and
 * left an unhandled rejection: a button that appears not to work. Found by review.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

vi.mock('./exporters', async (importOriginal) => {
  const real = await importOriginal<typeof import('./exporters')>()
  return { ...real, hydrate: vi.fn(async () => { throw new Error('500') }) }
})

import { Section } from './Section'
import { hydrate } from './exporters'

const table = { id: 'tbl-messages', columns: ['uuid', 'preview'], rows: [{ uuid: 'a', preview: 'cut' }] }
const meta = {
  id: 'tbl-messages', title: 'Messages', columns: [], filterable: true, page_size: null,
  full_text: { url: '/api/messages/text', key: 'uuid', column: 'preview', as: 'text' },
}
const section = { summary: 'The query behind this table', body: ['SELECT 1'], table_index: 0 }

describe('the section CSV button names its rows the way the heading does', () => {
  it('says "1 row" for one row and "2 rows" for two', () => {
    const { unmount } = render(<Section section={section} table={table} meta={meta} />)
    expect(screen.getByText(/export 1 row as CSV/)).toBeTruthy()
    unmount()
    const two = { ...table, rows: [...table.rows, { uuid: 'b', preview: 'cut too' }] }
    render(<Section section={section} table={two} meta={meta} />)
    expect(screen.getByText(/export 2 rows as CSV/)).toBeTruthy()
  })
})

describe('the section CSV button when the full text cannot be fetched', () => {
  it('says so next to the button and exports nothing', async () => {
    const clicks = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<Section section={section} table={table} meta={meta} />)
    fireEvent.click(screen.getByText(/export 1 row as CSV/))
    expect((await screen.findByRole('alert')).textContent).toContain('Could not fetch the full text')
    expect(hydrate).toHaveBeenCalledTimes(1)
    expect(clicks).not.toHaveBeenCalled()
    clicks.mockRestore()
  })

  it('does not fetch at all for a table with nothing cut (gate can fail)', async () => {
    vi.mocked(hydrate).mockClear()
    const clicks = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<Section section={section} table={table} meta={{ ...meta, full_text: undefined }} />)
    fireEvent.click(screen.getByText(/export 1 row as CSV/))
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(hydrate).not.toHaveBeenCalled()
    expect(clicks).toHaveBeenCalledTimes(1)
    clicks.mockRestore()
  })
})
