/**
 * A column keeps its width, and the reader can change it.
 *
 * The first case here FAILS on the code as it was, which is the proof it tests the fix: with
 * `table-layout: auto` the browser measured the rows on the page, so sorting re-fitted the columns
 * and the table jumped. jsdom computes no layout, so what is asserted is the `<col>` widths the
 * component writes, which is the thing that decides the layout in a real browser.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import type { Table, TableMeta } from '@/api'
import { DataTable } from './DataTable'
import { Pane } from './Pane'
import type { TabPayload } from '@/api'
import { COLUMN } from './columnWidths'

const rows = [
  { name: 'a', note: 'short' },
  { name: 'b', note: 'a considerably longer note than the others carry' },
  { name: 'c', note: 'mid length note' },
  { name: 'd', note: 'x' },
]

const table: Table = { id: 'tbl-notes', columns: ['name', 'note'], rows }
const meta: TableMeta = {
  id: 'tbl-notes', title: 'Notes', note: null, filterable: true, page_size: 2,
  columns: [
    { id: 'name', label: 'Name', numeric: false, specifier: null, align: 'left', hidden: false, bands: [] },
    { id: 'note', label: 'Note', numeric: false, specifier: null, align: 'left', hidden: false, bands: [] },
  ],
}

function show(over: Partial<TableMeta> = {}) {
  return render(<DataTable table={table} meta={{ ...meta, ...over }} title="Notes" />)
}

const cols = (container: HTMLElement) =>
  [...container.querySelectorAll('col')].map((c) => (c as HTMLElement).style.width)

const header = (name: string) => screen.getByRole('columnheader', { name: new RegExp(name) })
/** The LABEL row only: the second `thead` row holds one filter box per column and those are
 *  column headers too, so `getAllByRole('columnheader')` returns twice as many as there are
 *  columns. */
const labels = (container: HTMLElement) =>
  [...container.querySelectorAll('thead tr:first-child th')]
    .map((th) => (th.textContent ?? '').trim().replace(/[↑↓]/, ''))
const handleFor = (name: string) => screen.getByRole('separator', { name: `Resize the ${name} column` })

beforeEach(() => {
  try {
    localStorage.clear()
  } catch { /* storage is not required */ }
})

describe('sorting never moves a column', () => {
  it('keeps every width when the sort order changes (gate can fail)', () => {
    // Page size 2: page one holds the short values, and the longest note is on page two. Under
    // the old rule the sorted page was what got measured, so this is the case that moved.
    const { container } = show()
    const before = cols(container)
    fireEvent.click(header('Note'))
    expect(cols(container)).toEqual(before)
    fireEvent.click(header('Note'))
    expect(cols(container)).toEqual(before)
  })

  it('keeps them when the page changes', () => {
    const { container } = show()
    const before = cols(container)
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(cols(container)).toEqual(before)
  })

  it('keeps them while a filter narrows the table to one row', () => {
    const { container } = show()
    const before = cols(container)
    fireEvent.change(screen.getByLabelText('Filter Notes'), { target: { value: 'short' } })
    expect(cols(container)).toEqual(before)
  })

  it('says which way it is sorted, for a reader who cannot see the arrow', () => {
    show()
    expect(header('Note').getAttribute('aria-sort')).toBe('none')
    fireEvent.click(header('Note'))
    expect(header('Note').getAttribute('aria-sort')).toBe('descending')
    fireEvent.click(header('Note'))
    expect(header('Note').getAttribute('aria-sort')).toBe('ascending')
    expect(header('Name').getAttribute('aria-sort')).toBe('none')
  })

  it('reserves the room for an arrow before there is one', () => {
    // The glyph used to arrive with the first click and widen that header by its own width.
    const { container } = show()
    const slot = header('Name').querySelector('span[aria-hidden="true"]')!
    expect(slot.textContent).toBe('')
    expect(slot.className).toContain('w-3')
    const before = cols(container)
    fireEvent.click(header('Name'))
    expect(cols(container)).toEqual(before)
  })

  it('is a fixed layout whose total is the sum of its columns', () => {
    const { container } = show()
    const grid = container.querySelector('table')!
    expect(grid.className).toContain('table-fixed')
    // `w-full` would let CSS redistribute the surplus and override the colgroup.
    expect(grid.className).not.toContain('w-full')
    const sum = cols(container).reduce((n, w) => n + parseFloat(w), 0)
    expect(parseFloat(grid.style.width)).toBe(sum)
  })

  it('truncates every cell, because a fixed layout clips whatever it is told', () => {
    const { container } = show()
    for (const cell of container.querySelectorAll('tbody td')) {
      expect(cell.className).toContain('truncate')
    }
    // And the whole value is still one hover away.
    expect(screen.getByText('short').getAttribute('title')).toBe('short')
  })
})

describe('a column can be dragged wider', () => {
  it('moves the column it belongs to and no other', () => {
    const { container } = show()
    const [name, note] = cols(container).map(parseFloat)
    fireEvent.pointerDown(handleFor('Name'), { clientX: 100, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 140 })
    fireEvent.pointerUp(window, { clientX: 140 })
    expect(cols(container).map(parseFloat)).toEqual([name + 40, note])
  })

  it('does not sort the column it sits in', () => {
    show()
    fireEvent.pointerDown(handleFor('Name'), { clientX: 100, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 140 })
    fireEvent.pointerUp(window, { clientX: 140 })
    fireEvent.click(handleFor('Name'))
    expect(header('Name').getAttribute('aria-sort')).toBe('none')
  })

  it('does not start the drag that reorders columns', () => {
    const { container } = show()
    expect(handleFor('Name').getAttribute('draggable')).toBe('false')
    expect(fireEvent.dragStart(handleFor('Name'))).toBe(false)
    expect(labels(container)).toEqual(['Name', 'Note'])
  })

  it('stops at the bounds the column declares', () => {
    const { container } = show()
    fireEvent.pointerDown(handleFor('Name'), { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: -5000 })
    expect(parseFloat(cols(container)[0])).toBe(COLUMN.MIN)
    fireEvent.pointerMove(window, { clientX: 5000 })
    expect(parseFloat(cols(container)[0])).toBe(COLUMN.MAX)
  })

  it('keeps a width with its column when the columns are reordered', () => {
    const { container } = show()
    fireEvent.pointerDown(handleFor('Name'), { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 40 })
    fireEvent.pointerUp(window, { clientX: 40 })
    const [wideName, note] = cols(container).map(parseFloat)
    fireEvent.dragStart(header('Name'))
    fireEvent.drop(header('Note'))
    expect(labels(container)).toEqual(['Note', 'Name'])
    expect(cols(container).map(parseFloat)).toEqual([note, wideName])
  })

  it('leaves the other columns alone when one is hidden', () => {
    const { container } = show()
    const [, note] = cols(container).map(parseFloat)
    fireEvent.click(screen.getByText('Columns'))
    // "Name" is on screen twice with the menu open: the header and the menu's own entry.
    const menu = screen.getByRole('menu')
    fireEvent.click(within(menu).getByText('Name'))
    expect(cols(container).map(parseFloat)).toEqual([note])
  })
})

describe('the width is remembered', () => {
  it('comes back on a remount, keyed by the table rather than by where it was drawn', () => {
    const { container, unmount } = show()
    fireEvent.pointerDown(handleFor('Name'), { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 40 })
    fireEvent.pointerUp(window, { clientX: 40 })
    const after = cols(container)
    expect(localStorage.getItem('c4x.table.widths.tbl-notes|name,note')).toContain('"v":1')
    unmount()
    const again = show()
    expect(cols(again.container)).toEqual(after)
  })

  it('ignores a stored entry of the wrong version, an unknown column, or an absurd width', () => {
    const key = 'c4x.table.widths.tbl-notes|name,note'
    localStorage.setItem(key, JSON.stringify({ v: 0, cols: { name: 500 } }))
    const first = show()
    const derived = cols(first.container)
    first.unmount()
    localStorage.setItem(key, JSON.stringify({ v: 1, cols: { gone: 500 } }))
    const second = show()
    expect(cols(second.container)).toEqual(derived)
    second.unmount()
    localStorage.setItem(key, JSON.stringify({ v: 1, cols: { name: 1e9 } }))
    const third = show()
    expect(parseFloat(cols(third.container)[0])).toBe(COLUMN.MAX)
  })

  it('still renders and still drags when storage refuses to save', () => {
    const save = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage is full')
    })
    const { container } = show()
    const [name] = cols(container).map(parseFloat)
    fireEvent.pointerDown(handleFor('Name'), { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 40 })
    fireEvent.pointerUp(window, { clientX: 40 })
    expect(parseFloat(cols(container)[0])).toBe(name + 40)
    save.mockRestore()
  })

  it('offers to put the widths back only once something has been dragged', () => {
    const { container } = show()
    const before = cols(container)
    fireEvent.click(screen.getByText('Columns'))
    expect(screen.queryByText('Reset Widths')).toBeNull()
    fireEvent.click(screen.getByText('Columns'))
    fireEvent.pointerDown(handleFor('Name'), { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 40 })
    fireEvent.pointerUp(window, { clientX: 40 })
    fireEvent.click(screen.getByText('Columns'))
    fireEvent.click(screen.getByText('Reset Widths'))
    expect(cols(container)).toEqual(before)
  })

  it('does not hand one table the width somebody set on another (gate can fail)', () => {
    // THE LESSON `PaneTableIdentity.test.tsx` RECORDS, applied to widths. State seeded once per
    // component instance goes stale when a slot's table changes, so the remembered widths are
    // read on every change of table rather than in a `useState` initialiser.
    const slot = (tab: string, columns: string[]): TabPayload => ({
      tab, session: null, scope: 'main', cohort: null,
      tables: [{ id: `tbl-${tab}`, columns, rows: [Object.fromEntries(columns.map((c) => [c, c]))] }],
      figures: [], text: [],
      meta: [{
        id: `tbl-${tab}`, title: `${tab} table`, filterable: false, page_size: null,
        columns: columns.map((c) => ({
          id: c, label: c, numeric: false, specifier: null, align: 'left' as const,
          hidden: false, bands: [],
        })),
      }],
    })
    const { container, rerender } = render(<Pane payload={slot('tab-first', ['alpha', 'beta'])} />)
    fireEvent.pointerDown(screen.getByRole('separator', { name: 'Resize the alpha column' }),
                          { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 120 })
    fireEvent.pointerUp(window, { clientX: 120 })
    const dragged = parseFloat(cols(container)[0])
    rerender(<Pane payload={slot('tab-second', ['gamma', 'delta'])} />)
    expect(parseFloat(cols(container)[0])).not.toBe(dragged)
    expect(labels(container)).toEqual(['gamma', 'delta'])
  })
})
