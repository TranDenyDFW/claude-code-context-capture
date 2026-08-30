/**
 * The last link in the verification chain.
 *
 *   Dash == API          proven by tools/parity.py, 40 comparisons
 *   API payload == DOM   proven here
 *
 * Without this second link, "the API and the dashboard agree" says nothing about what a reader
 * sees, because the payload could be correct and the renderer could drop half of it. That is not a
 * hypothetical: the browser sweep that found it caught the Cost tab showing ONE table where the
 * payload had six, and the pane showing the previous tab's numbers under the new tab's name.
 *
 * Rendered with jsdom rather than a real browser on purpose: this asserts what the renderer does
 * with a payload, which needs no GPU, no Plotly and no network, so it can run in the same suite as
 * everything else instead of needing a machine with a display.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { Pane } from './Pane'
import type { TabPayload } from '@/api'

// Plotly is 4 MB of canvas rendering that jsdom cannot do and this file is not testing. The chart
// COUNT still matters, so the stub renders a marker rather than nothing.
vi.mock('./Plot', () => ({
  Plot: () => <div data-testid="chart" />,
}))

function payload(over: Partial<TabPayload> = {}): TabPayload {
  return {
    tab: 'tab-test',
    session: null,
    scope: 'main',
    cohort: null,
    tables: [],
    figures: [],
    text: [],
    plotly: [],
    ...over,
  }
}

function table(id: string | null, rows: Record<string, unknown>[]) {
  return { id, columns: rows.length ? Object.keys(rows[0]) : [], rows }
}

describe('a pane shows everything the payload carries', () => {
  it('renders every table, not just the first', () => {
    // The real shape of the Cost tab: six tables, five of them with no id.
    const tables = [
      table('tbl-reread', [{ a: 1 }]),
      ...Array.from({ length: 5 }, () => table('(anonymous)', [{ a: 1 }, { a: 2 }])),
    ]
    render(<Pane payload={payload({ tables })} />)
    expect(screen.getAllByRole('table')).toHaveLength(6)
  })

  it('renders every row of a table', () => {
    const rows = Array.from({ length: 37 }, (_, index) => ({ n: index }))
    render(<Pane payload={payload({ tables: [table('tbl-x', rows)] })} />)
    const body = screen.getByRole('table').querySelector('tbody')!
    expect(within(body).getAllByRole('row')).toHaveLength(37)
  })

  it('does not silently drop rows past the first page, it says how many there are', () => {
    // 100 is the page size. The rows that are not shown must be ANNOUNCED, because a table that
    // quietly shows its first hundred rows looks exactly like a table with a hundred rows.
    const rows = Array.from({ length: 317 }, (_, index) => ({ n: index }))
    render(<Pane payload={payload({ tables: [table('tbl-x', rows)] })} />)
    expect(screen.getByText(/showing 100 of 317 rows/)).toBeTruthy()
  })

  it('renders one chart per figure', () => {
    render(
      <Pane
        payload={payload({
          figures: [{ title: 'one', traces: [] }, { title: 'two', traces: [] }],
          plotly: [{ data: [] }, { data: [] }],
        })}
      />,
    )
    expect(screen.getAllByTestId('chart')).toHaveLength(2)
  })

  it('says so when a selection produces nothing, rather than showing a blank page', () => {
    render(<Pane payload={payload()} />)
    expect(screen.getByText(/produced nothing/)).toBeTruthy()
  })
})

describe('a table tells the truth about its values', () => {
  it('leaves an unknown value BLANK and never renders it as zero', () => {
    // The distinction the Cost tab exists to preserve: a model with no published price has an
    // unknown cost, not a zero cost. The API sends null. Printing 0 there would state something
    // false, and it would look more authoritative than the truth.
    render(<Pane payload={payload({ tables: [table('t', [{ est_usd: null, other: 0 }])] })} />)
    const cells = screen.getByRole('table').querySelectorAll('tbody td')
    expect(cells[0].textContent).toBe('')
    expect(cells[1].textContent).toBe('0')
  })

  it('shows a small cost at enough precision to still be a number', () => {
    render(<Pane payload={payload({ tables: [table('t', [{ usd: 0.0031 }])] })} />)
    // Rounded to two places this would print 0.00, which reads as free.
    expect(screen.getByRole('table').querySelector('tbody td')!.textContent).not.toBe('0.00')
  })

  it('marks a column that carries an explanation', () => {
    const one = { ...table('t', [{ a: 1, b: 2 }]), tooltips: { a: 'what a means' } }
    render(<Pane payload={payload({ tables: [one] })} />)
    const headers = screen.getAllByRole('columnheader')
    expect(headers[0].querySelector('.has-help')).toBeTruthy()
    expect(headers[1].querySelector('.has-help')).toBeNull()
  })
})

describe('clicking a row to select a session', () => {
  it('acts on a table that identifies a session in a HIDDEN column', () => {
    // The real shape of the All sessions table: `session_id` is in every row and absent from the
    // column list, which is how the dashboard carries an identifier without showing a uuid. The
    // first version of this check read `columns`, which made the one table the feature exists for
    // the only table where it did nothing.
    const clicked: Record<string, unknown>[] = []
    const rows = [{ session_id: 'abc', turns: 10 }]
    const hidden = { id: 't', columns: ['turns'], rows }
    render(
      <Pane payload={payload({ tables: [hidden] })} onRowClick={(r) => clicked.push(r)} />,
    )
    const row = screen.getByRole('table').querySelector('tbody tr')!
    row.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(clicked).toEqual([{ session_id: 'abc', turns: 10 }])
  })

  it('is INERT on a table with no session column, rather than looking clickable and doing nothing', () => {
    const clicked: Record<string, unknown>[] = []
    render(
      <Pane
        payload={payload({ tables: [table('t', [{ model: 'opus', usd: 1 }])] })}
        onRowClick={(r) => clicked.push(r)}
      />,
    )
    const row = screen.getByRole('table').querySelector('tbody tr')!
    row.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(clicked).toEqual([])
    expect(row.className).not.toContain('cursor-pointer')
  })
})

describe('the query behind a table', () => {
  const query = 'SELECT session_id FROM turns'

  function withSections() {
    return payload({
      tables: [table('tbl-a', [{ a: 1 }]), table('(anonymous)', [{ a: 2 }])],
      // `text` carries the query too, because extract.texts() flattens the whole pane including
      // what is inside a collapsed block. That is the duplication this must not reproduce.
      text: ['a caveat about the population', query, 'The query behind this table'],
      details: [{ summary: 'The query behind this table', body: [query], table_index: 1 }],
    })
  }

  it('puts the query under the table it belongs to, not the first one', () => {
    render(<Pane payload={withSections()} />)
    const sections = document.querySelectorAll('details')
    expect(sections).toHaveLength(1)
    // The section must be inside the SECOND table's block. Attributing a query to a table that did
    // not produce it is worse than showing no query: it is a wrong answer that looks checkable.
    const blocks = [...document.querySelectorAll('main, div > section')]
    const owner = [...document.querySelectorAll('section')].find((s) => s.querySelector('details'))
    expect(owner?.textContent).toContain('2')
    expect(blocks.length).toBeGreaterThan(0)
  })

  it('does not also print the query as prose, which would show it twice', () => {
    render(<Pane payload={withSections()} />)
    const shown = document.body.textContent ?? ''
    expect(shown.split(query).length - 1).toBe(1)
  })

  it('keeps prose that is NOT part of a section', () => {
    render(<Pane payload={withSections()} />)
    expect(screen.getByText('a caveat about the population')).toBeTruthy()
  })

  it('renders a section on its own when the server could not attribute it', () => {
    render(
      <Pane
        payload={payload({
          tables: [table('tbl-a', [{ a: 1 }])],
          details: [{ summary: 'unattached', body: ['something'], table_index: null }],
        })}
      />,
    )
    expect(document.querySelectorAll('details')).toHaveLength(1)
    expect(screen.getByText('unattached')).toBeTruthy()
  })
})

describe('headings', () => {
  it('never shows "(anonymous)", which is a placeholder and not a name', () => {
    render(<Pane payload={payload({ tables: [table('(anonymous)', [{ a: 1 }])] })} />)
    expect(screen.queryByText('(anonymous)')).toBeNull()
  })

  it('shows a real table id without its DOM prefix', () => {
    render(<Pane payload={payload({ tables: [table('tbl-compactions', [{ a: 1 }])] })} />)
    expect(screen.getByText('compactions')).toBeTruthy()
  })
})
