/**
 * A message that outlives the moment reads as a permanent state of the button.
 *
 * Both export surfaces say the same thing when a fetch fails and both clear it after 1800 ms.
 * Nothing advanced a clock, so either timer could have been deleted and the suite would not have
 * noticed; a stuck "Could not fetch the full text" beside a working button is a worse lie than no
 * message at all.
 *
 * NO findBy OR waitFor HERE. Testing Library decides whether fake timers are installed by looking
 * for a global `jest`, which vitest does not define, so its async helpers hang under
 * vi.useFakeTimers(). These flush promises with act() and move the clock by hand.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'

vi.mock('./exporters', async (importOriginal) => {
  const real = await importOriginal<typeof import('./exporters')>()
  return { ...real, hydrate: vi.fn(async () => { throw new Error('500') }) }
})

import { DataTable } from './DataTable'
import { Section } from './Section'

const table = {
  id: 'tbl-messages', columns: ['uuid', 'preview'], rows: [{ uuid: 'a', preview: 'cut' }],
}
const meta = {
  id: 'tbl-messages', title: 'Messages', columns: [], filterable: true, page_size: null,
  full_text: { url: '/api/messages/text', key: 'uuid', column: 'preview', as: 'text' },
}
const section = { summary: 'The query behind this table', body: ['SELECT 1'], table_index: 0 }

const settle = async () => { await act(async () => { await Promise.resolve() }) }
const tick = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('a message that says an export failed', () => {
  it('stops saying it, in the section, so the button does not read as broken', async () => {
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<Section section={section} table={table} meta={meta} />)
    fireEvent.click(screen.getByText(/export 1 row as CSV/))
    await settle()
    expect(screen.getByRole('alert').textContent).toContain('Could not fetch the full text')
    tick(1800)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('stops saying it in the toolbar too, on the same clock', async () => {
    render(<DataTable table={table} meta={meta} title="Messages" />)
    fireEvent.click(screen.getByText('Export'))
    fireEvent.click(screen.getByText('CSV'))
    await settle()
    expect(screen.getByText('Could not fetch the full text')).toBeTruthy()
    tick(1800)
    expect(screen.queryByText('Could not fetch the full text')).toBeNull()
  })
})
