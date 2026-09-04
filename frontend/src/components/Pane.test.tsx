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
import { fireEvent, render, screen, within } from '@testing-library/react'
import { Pane } from './Pane'
import type { Band, ColumnMeta, TableMeta, TabPayload } from '@/api'

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

  it('renders a page of rows, and says which page of what', () => {
    // 25 is the default page size. The rows NOT shown must be accounted for, because a table that
    // quietly shows its first page looks exactly like a table with one page of rows.
    const rows = Array.from({ length: 37 }, (_, index) => ({ n: index }))
    render(<Pane payload={payload({ tables: [table('tbl-x', rows)] })} />)
    const body = screen.getByRole('table').querySelector('tbody')!
    expect(within(body).getAllByRole('row')).toHaveLength(25)
    expect(screen.getByText('1 to 25 of 37')).toBeTruthy()
  })

  it('steps to the next page and shows the rest', () => {
    const rows = Array.from({ length: 37 }, (_, index) => ({ n: index }))
    render(<Pane payload={payload({ tables: [table('tbl-x', rows)] })} />)
    fireEvent.click(screen.getByText('Next'))
    expect(screen.getByText('26 to 37 of 37')).toBeTruthy()
    const body = screen.getByRole('table').querySelector('tbody')!
    expect(within(body).getAllByRole('row')).toHaveLength(12)
  })

  it('never implies the page is the whole table', () => {
    const rows = Array.from({ length: 317 }, (_, index) => ({ n: index }))
    render(<Pane payload={payload({ tables: [table('tbl-x', rows)] })} />)
    expect(screen.getByText('1 to 25 of 317')).toBeTruthy()
    expect(screen.getByText('Page 1 of 13')).toBeTruthy()
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

  it('opens the inspector on a table with no session column, and never selects a session from it', () => {
    // This used to assert the row was inert. A row that looks clickable must do something, and
    // now every row does: it opens the drawer with its fields, which is the message reader for the
    // Messages table and a plain field list for every other one.
    const clicked: Record<string, unknown>[] = []
    render(
      <Pane
        payload={payload({ tables: [table('t', [{ model: 'opus', usd: 1 }])] })}
        onRowClick={(r) => clicked.push(r)}
      />,
    )
    const row = screen.getByRole('table').querySelector('tbody tr')!
    fireEvent.click(row)
    expect(clicked).toEqual([])
    const drawer = screen.getByRole('dialog')
    expect(drawer.textContent).toContain('opus')
    expect(drawer.textContent).toContain('usd')
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

describe('headings come from the server, not from a rule in here', () => {
  const meta = (over: Partial<TableMeta> = {}): TableMeta => ({
    id: 't', title: null, columns: [], filterable: true, page_size: null, ...over,
  })

  it('shows the title the app gave the table', () => {
    render(
      <Pane
        payload={payload({
          tables: [table('tbl-reread', [{ a: 1 }])],
          meta: [meta({ id: 'tbl-reread', title: 'Files Read More Than Once' })],
        })}
      />,
    )
    expect(screen.getByText('Files Read More Than Once')).toBeTruthy()
  })

  it('never shows a raw DOM id as a heading', () => {
    // The previous version derived the heading here by stripping a `tbl-` prefix, which is a
    // naming rule living in the browser. With no title from the server there is no heading.
    render(
      <Pane payload={payload({ tables: [table('tbl-compactions', [{ a: 1 }])], meta: [meta()] })} />,
    )
    expect(screen.queryByText(/tbl-/)).toBeNull()
    expect(screen.queryByText('compactions')).toBeNull()
  })

  it('never shows "(anonymous)", which is a placeholder and not a name', () => {
    render(<Pane payload={payload({ tables: [table('(anonymous)', [{ a: 1 }])] })} />)
    expect(screen.queryByText('(anonymous)')).toBeNull()
  })
})

describe('a column is rendered the way the APP declares it', () => {
  const withMeta = (columns: ColumnMeta[], rows: Record<string, unknown>[]) =>
    payload({
      tables: [{ id: 't', columns: columns.map((c) => c.id), rows }],
      meta: [{ id: 't', title: null, columns, filterable: true, page_size: null }],
    })

  const column = (over: Partial<ColumnMeta> = {}): ColumnMeta => ({
    id: 'n', label: 'N', numeric: true, specifier: null, align: 'right', hidden: false, bands: [],
    ...over,
  })

  it('honours a fixed-precision specifier instead of guessing from the value', () => {
    // THE DEFECT THIS EXISTS FOR. `percent` is declared `.1f`, so every row gets one decimal. The
    // old renderer asked `Number.isInteger` and wrote 43.3, 2.4 and then a bare 1, which reads as
    // a different quantity from the rows above it.
    render(
      <Pane
        payload={withMeta(
          [column({ id: 'percent', label: 'Percent', specifier: '.1f' })],
          [{ percent: 43.3 }, { percent: 1 }, { percent: 0.8 }],
        )}
      />,
    )
    const cells = [...screen.getByRole('table').querySelectorAll('tbody td')].map((c) => c.textContent)
    expect(cells).toEqual(['43.3', '1.0', '0.8'])
  })

  it('groups thousands when the specifier says so', () => {
    render(<Pane payload={withMeta([column({ specifier: ',' })], [{ n: 1234567 }])} />)
    expect(screen.getByRole('table').querySelector('tbody td')!.textContent).toBe(
      (1234567).toLocaleString(),
    )
  })

  it('uses the label the server gave, never the raw column id', () => {
    render(<Pane payload={withMeta([column({ id: 'ts', label: 'Date & Time' })], [{ ts: 'x' }])} />)
    // Two header rows now: the titles, then the per-column filter inputs.
    const [title] = screen.getAllByRole('columnheader')
    expect(title.textContent).toContain('Date & Time')
    expect(title.textContent).not.toContain('ts')
  })

  it('aligns a numeric HEADER the same way as its cells', () => {
    render(<Pane payload={withMeta([column({ align: 'right' })], [{ n: 1 }])} />)
    const [title] = screen.getAllByRole('columnheader')
    expect(title.className).toContain('text-right')
    expect(screen.getByRole('table').querySelector('tbody td')!.className).toContain('text-right')
  })

  it('shades a cell with the LAST band it matches, not the first', () => {
    // Bands are emitted shallowest first and a top value matches them all, so stopping at the
    // first match would paint every outlier the palest colour.
    const bands: Band[] = [
      { op: '>=', at: 10, background: '#111111' },
      { op: '>=', at: 100, background: '#999999' },
    ]
    render(<Pane payload={withMeta([column({ bands })], [{ n: 500 }, { n: 12 }, { n: 1 }])} />)
    const cells = [...screen.getByRole('table').querySelectorAll('tbody td')] as HTMLElement[]
    expect(cells[0].style.backgroundColor).toBe('rgb(153, 153, 153)')
    expect(cells[1].style.backgroundColor).toBe('rgb(17, 17, 17)')
    expect(cells[2].style.backgroundColor).toBe('')
  })
})

describe('every table can be filtered', () => {
  it('narrows the rows and says how many of the whole table matched', () => {
    const rows = [{ project: 'alpha' }, { project: 'beta' }, { project: 'gamma' }]
    render(<Pane payload={payload({ tables: [table('t', rows)] })} />)
    // "ga", not "a". The first version of this used "a" and expected two matches, which was wrong:
    // alpha, beta and gamma all contain one, so the correct answer was 3 of 3 and the test failed
    // on its own arithmetic rather than on the code.
    fireEvent.change(screen.getByPlaceholderText('Filter'), { target: { value: 'ga' } })
    // The count is stated so a filtered table can never be mistaken for the whole one.
    expect(screen.getByText('1 of 3 match')).toBeTruthy()
    expect(screen.getByRole('table').querySelectorAll('tbody tr')).toHaveLength(1)
  })

  it('filters the WHOLE table, not the page that happens to be on screen', () => {
    // 100 is the page size. The match is row 250, which is not rendered until it is found.
    const rows = Array.from({ length: 300 }, (_, index) => ({ name: `row-${index}` }))
    render(<Pane payload={payload({ tables: [table('t', rows)] })} />)
    fireEvent.change(screen.getByPlaceholderText('Filter'), { target: { value: 'row-250' } })
    expect(screen.getByText('1 of 300 match')).toBeTruthy()
  })

  it('says so when nothing matches, rather than showing an empty table', () => {
    render(<Pane payload={payload({ tables: [table('t', [{ a: 'x' }])] })} />)
    fireEvent.change(screen.getByPlaceholderText('Filter'), { target: { value: 'zzz' } })
    expect(screen.getByText(/Nothing matches/)).toBeTruthy()
  })
})

describe('the headline figures', () => {
  const stats = [
    { label: 'sessions', value: '1,325', sub: 'in the store, 317 listed on All sessions' },
    { label: 'API calls', value: '158,835', sub: '346,909 transcript rows behind them' },
  ]

  it('renders one card per stat, with its number', () => {
    render(<Pane payload={payload({ stats })} />)
    expect(screen.getByText('1,325')).toBeTruthy()
    expect(screen.getByText('158,835')).toBeTruthy()
  })

  it('does NOT also print the same strings as prose', () => {
    // Every part of a stat card is in `text` as well, because extract.texts() flattens the pane.
    // Rendered naively the page shows "sessions / 1,325 / in the store" again as body prose,
    // directly under the card that already says it.
    const text = stats.flatMap((s) => [s.label, s.value, s.sub])
    render(<Pane payload={payload({ stats, text: [...text, 'a real caveat'] })} />)
    expect(screen.getAllByText('1,325')).toHaveLength(1)
    expect(screen.getByText('a real caveat')).toBeTruthy()
  })
})

describe('the population sentence is NOT body text', () => {
  const line = 'Store-wide. Not affected by the header selection.'

  it('is kept out of the pane entirely', () => {
    // It moved to a chip beside the selection controls, with the sentence as its tooltip. As body
    // text it read as one more grey paragraph: on Diagnostics it sat above a table somebody was
    // asking why it never changed.
    render(<Pane payload={payload({ scoped: false, population: line, text: [line, 'other'] })} />)
    expect(screen.queryByText(line)).toBeNull()
  })

  it('and does not take the rest of the prose with it', () => {
    render(<Pane payload={payload({ scoped: false, population: line, text: [line, 'other'] })} />)
    expect(screen.getByText('other')).toBeTruthy()
  })
})

describe('a section renders what it WRAPS, not an empty box', () => {
  const meta = (id: string, title: string | null): TableMeta => ({
    id, title, columns: [], filterable: true, page_size: null,
  })

  it('a section wrapping a table becomes that table heading', () => {
    // "What to do about it" wraps the findings DataTable. `extract.texts()` reads prose only, so
    // its body came back empty and the page drew a collapsible with nothing inside it.
    render(
      <Pane
        payload={payload({
          tables: [table('tbl-findings', [{ finding: 'x' }])],
          meta: [meta('tbl-findings', 'Findings')],
          details: [{ summary: 'What to do about it 5 finding(s)', body: [],
                      table_index: -1, wraps: 'table', wraps_index: 0 }],
        })}
      />,
    )
    expect(screen.getByText('What to do about it 5 finding(s)')).toBeTruthy()
    expect(document.querySelectorAll('details')).toHaveLength(0)
  })

  it('a section wrapping a figure becomes that chart heading', () => {
    render(
      <Pane
        payload={payload({
          figures: [{ title: 'raw title', traces: [] }],
          plotly: [{ data: [] }],
          details: [{ summary: 'Where the tokens went, top 15', body: [],
                      table_index: 0, wraps: 'figure', wraps_index: 0 }],
        })}
      />,
    )
    expect(screen.getByText('Where the tokens went, top 15')).toBeTruthy()
    expect(document.querySelectorAll('details')).toHaveLength(0)
  })

  it('a section wrapping the stat cards is DROPPED, because the cards already say it', () => {
    render(
      <Pane
        payload={payload({
          stats: [{ label: 'sessions', value: '1,325', sub: 'in the store' }],
          details: [{ summary: 'Store totals', body: ['sessions', '1,325', 'in the store'],
                      table_index: 0, wraps: 'stats', wraps_index: null }],
        })}
      />,
    )
    expect(document.querySelectorAll('details')).toHaveLength(0)
    expect(screen.queryByText('Store totals')).toBeNull()
    expect(screen.getAllByText('1,325')).toHaveLength(1)
  })

  it('a section wrapping PROSE is still a collapsible', () => {
    render(
      <Pane
        payload={payload({
          tables: [table('t', [{ a: 1 }])],
          details: [{ summary: 'The query behind this table', body: ['SELECT 1'],
                      table_index: 0, wraps: 'text', wraps_index: null }],
        })}
      />,
    )
    expect(document.querySelectorAll('details')).toHaveLength(1)
  })
})

describe('the table controls every table now has', () => {
  const rows = [{ project: 'alpha', turns: 3 }, { project: 'beta', turns: 4 }]

  it('offers export on a table with no SQL section at all', () => {
    // The defect this fixes: CSV lived inside the SQL accordion, and only two of eight tabs have
    // one, so eleven of seventeen tables had no way to get the data out.
    render(<Pane payload={payload({ tables: [table('t', rows)] })} />)
    expect(screen.getByText('Export')).toBeTruthy()
    expect(screen.getByText('Columns')).toBeTruthy()
  })

  it('filters by ONE column without touching the others', () => {
    render(<Pane payload={payload({ tables: [table('t', rows)] })} />)
    fireEvent.change(screen.getByLabelText('Filter by project'), { target: { value: 'alph' } })
    expect(screen.getByText('1 of 2 match')).toBeTruthy()
  })

  it('hides a column, and the hidden column leaves the search with it', () => {
    render(<Pane payload={payload({ tables: [table('t', rows)] })} />)
    fireEvent.click(screen.getByText('Columns'))
    fireEvent.click(screen.getByText('Hide All'))
    // Every column hidden means nothing left to match, so a global search finds nothing rather
    // than matching text the reader can no longer see.
    fireEvent.change(screen.getByPlaceholderText('Filter'), { target: { value: 'alpha' } })
    expect(screen.getByText('0 of 2 match')).toBeTruthy()
  })

  it('carries the full value as a cell tooltip, so a truncated cell is still readable', () => {
    const long = 'P:\ClaudeExt\ccx-engineering-work\tmp\fidpool\F2-p6'
    render(<Pane payload={payload({ tables: [table('t', [{ project: long }])] })} />)
    expect(screen.getByRole('table').querySelector('tbody td')!.getAttribute('title')).toBe(long)
  })
})

describe('a card caption', () => {
  it('is SHOWN, not left to a hover', () => {
    /**
     * On at least half of these the caption is what the number MEANS: "Peak Resident" is the
     * largest single API call in the store and not any session's peak, "Sessions" counts every
     * session while only some are listed on All sessions, and "API Calls" exists to be told apart
     * from the transcript row count that its own caption gives. A reader cannot hover for a
     * qualifier they have no reason to suspect is there.
     */
    render(<Pane payload={payload({ stats: [
      { label: 'Sessions', value: '1,325', sub: 'in the store, 317 listed on All sessions' },
      { label: 'Peak Resident', value: '999.8k', sub: 'largest single API call, any session' },
    ] })} />)
    expect(screen.getByText('in the store, 317 listed on All sessions')).toBeTruthy()
    expect(screen.getByText('largest single API call, any session')).toBeTruthy()
  })
})

describe('every table is named and explained the same way', () => {
  const meta = (over: Partial<TableMeta> = {}): TableMeta => ({
    id: 't', title: null, columns: [], filterable: true, page_size: null, ...over,
  })

  it('names a table the server could not, so no table is the one unnamed thing on a page', () => {
    render(<Pane payload={payload({ tables: [table('(anonymous)', [{ a: 1 }])], meta: [meta()] })} />)
    expect(screen.getByRole('heading', { level: 3 }).textContent).toContain('Table 1')
  })

  it('puts the note on the heading, not in the body', () => {
    render(
      <Pane
        payload={payload({
          tables: [table('(anonymous)', [{ a: 1 }])],
          text: ['Probe runs', 'Each row is one spawned session answering the control protocol.'],
          meta: [meta({
            title: 'Probe runs',
            note: 'Each row is one spawned session answering the control protocol.',
            absorbed: ['Probe runs', 'Each row is one spawned session answering the control protocol.'],
          })],
        })}
      />,
    )
    const heading = screen.getByRole('heading', { level: 3 })
    expect(heading.getAttribute('title')).toContain('Each row is one spawned session')
    // Once, as the heading; never again as a paragraph.
    expect(screen.getAllByText('Probe runs')).toHaveLength(1)
    expect(screen.queryByText('Each row is one spawned session answering the control protocol.')).toBeNull()
  })

  it('says "1 row" for one row, the way the table footer already does', () => {
    render(
      <Pane payload={payload({ tables: [table('t', [{ a: 1 }])], meta: [meta({ title: 'One' })] })} />,
    )
    const heading = screen.getByRole('heading', { level: 3 }).textContent ?? ''
    expect(heading).toContain('1 row')
    expect(heading).not.toContain('1 rows')
  })

  it('states the row count beside the name', () => {
    render(
      <Pane payload={payload({ tables: [table('t', [{ a: 1 }, { a: 2 }, { a: 3 }])], meta: [meta({ title: 'Three' })] })} />,
    )
    expect(screen.getByRole('heading', { level: 3 }).textContent).toContain('3 rows')
  })

  it('does not print a stat card label as prose because the server changed its case', () => {
    render(
      <Pane
        payload={payload({
          stats: [{ label: 'Re-read Groups', value: '1,158', sub: '' }],
          text: ['Re-read groups', '1,158', 'A sentence that is real prose.'],
        })}
      />,
    )
    expect(screen.getAllByText(/re-read groups/i)).toHaveLength(1)
    expect(screen.getByText('A sentence that is real prose.')).toBeTruthy()
  })
})

describe('every table can open in a window of its own', () => {
  const meta = (over: Partial<TableMeta> = {}): TableMeta => ({
    id: 't', title: null, columns: [], filterable: true, page_size: null, ...over,
  })

  it('shows the control on each table and reports which table was asked for', () => {
    const open = vi.fn()
    render(
      <Pane
        payload={payload({
          tables: [table('a', [{ a: 1 }]), table('b', [{ b: 2 }])],
          meta: [meta({ id: 'a', title: 'First' }), meta({ id: 'b', title: 'Second' })],
        })}
        onOpenTable={open}
      />,
    )
    const buttons = screen.getAllByRole('button', { name: /in a new window/ })
    expect(buttons).toHaveLength(2)
    fireEvent.click(buttons[1])
    expect(open).toHaveBeenCalledWith(1)
  })

  it('shows no control when nothing can open one (gate can fail)', () => {
    render(<Pane payload={payload({ tables: [table('a', [{ a: 1 }])], meta: [meta({ id: 'a', title: 'First' })] })} />)
    expect(screen.queryByRole('button', { name: /in a new window/ })).toBeNull()
  })
})

describe('text under a Dash-only control is never printed', () => {
  it('drops the calculator labels the server names, and keeps real prose', () => {
    render(
      <Pane
        payload={payload({
          text: ['Resident tokens', 'Window', 'Constants read from tools/mirror-core.mjs', 'A real sentence.'],
          dash_only: ['Resident tokens', 'Window', 'Constants read from tools/mirror-core.mjs'],
        })}
      />,
    )
    expect(screen.queryByText('Resident tokens')).toBeNull()
    expect(screen.queryByText('Window')).toBeNull()
    expect(screen.getByText('A real sentence.')).toBeTruthy()
  })
})

describe('a table whose first row is not the one carrying the session', () => {
  it('is still navigable, because the key is read across rows and not from row zero', () => {
    const clicked: Record<string, unknown>[] = []
    render(
      <Pane
        payload={payload({
          tables: [{ id: 't', columns: ['turns'], rows: [{ turns: 1 }, { session_id: 'abc', turns: 2 }] }],
        })}
        onRowClick={(row) => clicked.push(row)}
      />,
    )
    const rows = screen.getByRole('table').querySelectorAll('tbody tr')
    fireEvent.click(rows[1])
    expect(clicked).toEqual([{ session_id: 'abc', turns: 2 }])
  })
})
