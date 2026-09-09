/**
 * A table drawn into a slot another table used must not inherit its state.
 *
 * DataTable seeds hidden columns, column order and page size from the server's metadata in
 * `useState` initialisers, which run ONCE per component instance. Pane keyed its sections by index
 * alone, so moving from one tab to another handed table 0 of the new tab the instance that already
 * belonged to table 0 of the old one. The new table's declared hidden columns were then ignored,
 * which silently undid 0d881f1 on every return to an already-visited tab and made an export's
 * column set depend on where the reader had been.
 *
 * Found by an independent sweep of the branch, in two dimensions at once.
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Pane } from './Pane'
import type { TabPayload } from '@/api'

function payloadWith(tab: string, columns: string[], hidden: string[]): TabPayload {
  return {
    tab,
    session: null,
    scope: 'main',
    cohort: null,
    tables: [{
      id: '(anonymous)',
      columns,
      rows: [Object.fromEntries(columns.map((c) => [c, `${c}-value`]))],
    }],
    figures: [],
    text: [],
    meta: [{
      id: '(anonymous)',
      title: `${tab} table`,
      columns: columns.map((c) => ({
        id: c,
        label: c,
        numeric: false,
        specifier: null,
        align: 'left' as const,
        hidden: hidden.includes(c),
        bands: [],
      })),
      filterable: false,
      page_size: null,
    }],
  }
}

describe('a table drawn where another table was', () => {
  it('honours its OWN hidden columns, not the ones the previous table declared', () => {
    // Two tabs, one table each, at the same index. The first hides nothing; the second hides
    // `secret`. Before the fix the second rendered `secret` because the instance was reused.
    const { rerender } = render(
      <Pane payload={payloadWith('tab-first', ['visible', 'other'], [])} />,
    )
    expect(screen.getByText('other')).toBeTruthy()

    rerender(<Pane payload={payloadWith('tab-second', ['visible', 'secret'], ['secret'])} />)
    expect(screen.queryByText('secret')).toBeNull()
    expect(screen.getByText('visible')).toBeTruthy()
  })

  it('and the other direction: a column hidden on one tab is shown on the next', () => {
    const { rerender } = render(
      <Pane payload={payloadWith('tab-first', ['visible', 'secret'], ['secret'])} />,
    )
    expect(screen.queryByText('secret')).toBeNull()

    rerender(<Pane payload={payloadWith('tab-second', ['visible', 'secret'], [])} />)
    expect(screen.getByText('secret')).toBeTruthy()
  })
})
