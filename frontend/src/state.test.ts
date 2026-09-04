/**
 * The address bar describes the screen. A state written to a query string and read back is the
 * same state; a string nobody wrote is read as the defaults, never as a crash or a half-state.
 */
import { describe, expect, it } from 'vitest'
import { EMPTY, fromSearch, tableUrl, toSearch, type ViewState } from './state'

const full: ViewState = {
  tab: 'tab-cost',
  selection: { session: 'abc-123', scope: 'all', cohort: 'project::P:\\x', compareWith: 'def', compareKind: 'cohort' },
  view: 'table',
  table: 3,
  figure: null,
  compaction: null,
  query: 'categories.json',
  filter: { key: 'session_id', value: 's-1' },
}

describe('the view state round-trips through the query string', () => {
  it('reads back exactly what was written, every field', () => {
    expect(fromSearch(toSearch(full))).toEqual(full)
  })

  it('writes nothing for the defaults, so a fresh page has an empty address', () => {
    expect(toSearch(EMPTY)).toBe('')
    expect(fromSearch('')).toEqual(EMPTY)
    expect(fromSearch('?')).toEqual(EMPTY)
  })

  it('reads a scope it does not know as main, and a compare kind it does not know as session', () => {
    expect(fromSearch('?scope=everything').selection.scope).toBe('main')
    expect(fromSearch('?compare=x&compareKind=galaxy').selection.compareKind).toBe('session')
  })

  it('needs a table index for the table view, and the index must be a whole number', () => {
    expect(fromSearch('?view=table').view).toBe('dashboard')
    expect(fromSearch('?view=table&table=two').view).toBe('dashboard')
    expect(fromSearch('?view=table&table=2')).toMatchObject({ view: 'table', table: 2 })
  })

  it('keeps the filter only with the table view, where it means something', () => {
    expect(toSearch({ ...EMPTY, query: 'stray' })).toBe('')
    expect(fromSearch(toSearch({ ...EMPTY, view: 'table', table: 0, query: 'x y' })).query).toBe('x y')
  })

  it('ignores keys it does not know rather than failing on them (gate can fail)', () => {
    expect(fromSearch('?utm_source=mail&tab=tab-window').tab).toBe('tab-window')
  })

  it('builds an absolute link that opens one table on its own, pre-filtered', () => {
    const base = { ...EMPTY, tab: 'tab-cost', selection: { scope: 'main' as const, session: 's1' } }
    const url = tableUrl(base, 4, { query: 'needle' })
    expect(url.startsWith(window.location.origin)).toBe(true)
    expect(fromSearch(new URL(url).search)).toMatchObject({
      tab: 'tab-cost', view: 'table', table: 4, query: 'needle', selection: { session: 's1', scope: 'main' },
    })
  })

  it('carries an exact filter, which is the only way to point at a row by a hidden key', () => {
    const url = tableUrl({ ...EMPTY, tab: 'tab-sessions' }, 0, { filter: { key: 'session_id', value: 's-2' } })
    expect(fromSearch(new URL(url).search)).toMatchObject({
      view: 'table', table: 0, filter: { key: 'session_id', value: 's-2' }, query: '',
    })
  })

  it('refuses a table index that is not a safe integer, and one beyond any payload', () => {
    expect(fromSearch('?view=table&table=99999999999999999999').view).toBe('dashboard')
    expect(fromSearch('?view=table&table=1000').view).toBe('dashboard')
    expect(fromSearch('?view=table&table=999')).toMatchObject({ view: 'table', table: 999 })
  })

  it('drops every table-view field outside that view, so one state has one address', () => {
    const state = fromSearch('?tab=tab-cost&q=x&key=k&val=v')
    expect(state).toMatchObject({ view: 'dashboard', table: null, query: '', filter: null })
    expect(toSearch(state)).toBe('?tab=tab-cost')
  })
})
