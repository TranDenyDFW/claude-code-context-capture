import type { Selection } from '@/api'

/**
 * What the page is showing, in the URL as well as in React state.
 *
 * The app kept its state in memory only, so a page could not be shared, bookmarked, reopened after
 * a reload, or opened twice in two windows showing two different things. Everything a reader can
 * change from the shell is here: the tab, the selection (session, scope, cohort, the compare arm),
 * and the two fields that turn the shell into a single-table page. The URL is written with
 * replaceState on every change, so the address bar always describes the screen and the history
 * does not fill up with every click.
 *
 * Nothing transient goes here. The inspector drawer, the palette and the live toggle are moments,
 * not places, and a link that reopened a drawer would surprise the person it was sent to.
 */
export interface ViewState {
  tab: string | null
  selection: Selection
  /** `table` and `figure` each render exactly one thing full width, for a window of its own. */
  view: 'dashboard' | 'table' | 'figure' | 'compaction'
  /** Which table of the tab, by index into `payload.tables`, when `view` is `table`. */
  table: number | null
  /**
   * Which chart of the tab, by index into `payload.plotly`, when `view` is `figure`.
   *
   * Indexed the same way the pane pairs them, so a link to a chart survives being sent to someone
   * else exactly as a link to a table does.
   */
  figure: number | null
  /** Which compaction, by uuid: a document, not an index into this tab's payload. */
  compaction: string | null
  /** A filter to seed the table's search box with, so a link can point at the rows behind a click. */
  query: string
  /** An exact row filter: a column and its value, so a link can name a row by a hidden key. */
  filter: { key: string; value: string } | null
}

export const EMPTY: ViewState = {
  tab: null, selection: { scope: 'main' }, view: 'dashboard', table: null, figure: null,
  compaction: null,
  query: '', filter: null,
}

/** A table index the app could actually hold: a whole number, not a float, not beyond a payload. */
const MAX_TABLE = 999

/** The state a query string describes. Anything absent or unreadable falls back to the default. */
export function fromSearch(search: string): ViewState {
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search)
  const text = (key: string) => {
    const value = params.get(key)
    return value && value.trim() ? value.trim() : null
  }
  const scope = params.get('scope') === 'all' ? 'all' : 'main'
  const compareKind = params.get('compareKind') === 'cohort' ? 'cohort' : 'session'
  // BOUNDED. A digits-only test alone accepted a twenty-digit index, which is not a safe integer,
  // so the round trip was not a fixed point and the value was neither a table nor an error.
  const whole = (key: string) => {
    const raw = params.get(key)
    const parsed = raw !== null && /^\d+$/.test(raw) ? Number(raw) : null
    return parsed !== null && Number.isSafeInteger(parsed) && parsed <= MAX_TABLE ? parsed : null
  }
  const table = whole('table')
  const selection: Selection = { scope }
  const session = text('session')
  const cohort = text('cohort')
  const compare = text('compare')
  if (session) selection.session = session
  if (cohort) selection.cohort = cohort
  if (compare) {
    selection.compareWith = compare
    selection.compareKind = compareKind
  }
  const figure = whole('figure')
  const asked = params.get('view')
  const compaction = text('compaction')
  const view = asked === 'table' && table !== null ? 'table'
    : asked === 'figure' && figure !== null ? 'figure'
    : asked === 'compaction' && compaction ? 'compaction'
    : 'dashboard'
  const filterKey = text('key')
  const filterValue = params.get('val')
  return {
    tab: text('tab'),
    selection,
    view,
    // Every single-thing field is dropped outside its own view, so one state has one address.
    table: view === 'table' ? table : null,
    figure: view === 'figure' ? figure : null,
    compaction: view === 'compaction' ? compaction : null,
    query: view === 'table' ? (params.get('q') ?? '') : '',
    filter: view === 'table' && filterKey && filterValue !== null
      ? { key: filterKey, value: filterValue }
      : null,
  }
}

/** The query string for a state. Defaults are omitted, so a fresh page has an empty address. */
export function toSearch(state: ViewState): string {
  const params = new URLSearchParams()
  if (state.tab) params.set('tab', state.tab)
  const { selection } = state
  if (selection.session) params.set('session', selection.session)
  if (selection.scope === 'all') params.set('scope', 'all')
  if (selection.cohort) params.set('cohort', selection.cohort)
  if (selection.compareWith) {
    params.set('compare', selection.compareWith)
    if (selection.compareKind === 'cohort') params.set('compareKind', 'cohort')
  }
  if (state.view === 'compaction' && state.compaction) {
    params.set('view', 'compaction')
    params.set('compaction', state.compaction)
  }
  if (state.view === 'figure' && state.figure !== null) {
    params.set('view', 'figure')
    params.set('figure', String(state.figure))
  }
  if (state.view === 'table' && state.table !== null) {
    params.set('view', 'table')
    params.set('table', String(state.table))
    if (state.query) params.set('q', state.query)
    if (state.filter) {
      params.set('key', state.filter.key)
      params.set('val', state.filter.value)
    }
  }
  const text = params.toString()
  return text ? `?${text}` : ''
}

export function readState(): ViewState {
  try {
    return fromSearch(window.location.search)
  } catch {
    return EMPTY
  }
}

/** Write the state to the address bar without adding a history entry. Idempotent. */
export function writeState(state: ViewState): void {
  try {
    const next = toSearch(state)
    if (next === window.location.search) return
    window.history.replaceState(null, '', `${window.location.pathname}${next}${window.location.hash}`)
  } catch {
    // A page served from a context without history access still works; it just cannot be shared.
  }
}

/**
 * An absolute URL that opens one table of the current view on its own.
 *
 * Two kinds of narrowing, because they are two different things: `query` is the text the search box
 * would hold, which only ever finds what the page DRAWS, and `filter` names a column and a value
 * exactly, which is the only way to point at a row by a hidden key such as a session id.
 */
/** An address for one chart of the current tab, so a chart can be opened in a window of its own. */
export function figureUrl(state: ViewState, figure: number): string {
  const search = toSearch({
    ...state, view: 'figure', figure, table: null, compaction: null, query: '', filter: null,
  })
  return `${window.location.origin}${window.location.pathname}${search}`
}

/** An address for one compaction, so a boundary opens in a window of its own. */
export function compactionUrl(state: ViewState, compaction: string): string {
  const search = toSearch({
    ...state, view: 'compaction', compaction, table: null, figure: null, query: '', filter: null,
  })
  return `${window.location.origin}${window.location.pathname}${search}`
}

export function tableUrl(
  state: ViewState,
  table: number,
  focus: { query?: string; filter?: { key: string; value: string } | null } = {},
): string {
  const search = toSearch({
    ...state, view: 'table', table, figure: null, compaction: null,
    query: focus.query ?? '', filter: focus.filter ?? null,
  })
  return `${window.location.origin}${window.location.pathname}${search}`
}
