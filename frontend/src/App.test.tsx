/**
 * The shell, which had no test file at all while four merged fixes lived only in it.
 *
 * Everything here was demonstrated by an independent review as a working reproduction and then
 * shipped without a regression test: the way back from a table address whose tab did not build,
 * the palette shortcut that must not fire behind the single-table page, the six fields popstate
 * has to restore, and the filter that must reach the address without wiping what the reader typed.
 *
 * jsdom implements history and popstate, so no shim is needed; what IS needed is resetting the
 * address between cases, because `readState()` runs once in a mount-time useMemo and one window is
 * shared by the whole file.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/api', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/api')>()
  return {
    ...real,
    // ApiError stays REAL: App branches on `error instanceof ApiError`, and a stubbed class would
    // make the failure pane print the wrong half of itself while the test still passed.
    api: {
      tabs: vi.fn(), health: vi.fn(), cohorts: vi.fn(), selector: vi.fn(), tab: vi.fn(),
      sessions: vi.fn(),
      // EVERY CALL THE HEADER MAKES, including the ones a control added later. A partial mock is
      // not a smaller mock: `AccountSharing` asks on mount, and with `accounts` missing the whole
      // header threw and four tests about the population chip failed for a reason none of them
      // named.
      accounts: { state: vi.fn(), share: vi.fn(), verify: vi.fn(), reconcile: vi.fn() },
      adopt: { state: vi.fn(), run: vi.fn(), retitle: vi.fn(), sweep: vi.fn() },
      server: { stop: vi.fn(), restart: vi.fn() },
    },
  }
})
vi.mock('./components/Plot', () => ({ Plot: () => <div data-testid="chart" /> }))

import App from './App'
import { api, ApiError } from '@/api'

const TABS = [
  { id: 'tab-cost', label: 'Cost', help: 'What was paid for twice.', scoped: true },
  { id: 'tab-summary', label: 'Summary', help: 'Findings.', scoped: false },
]

function payload(over: Record<string, unknown> = {}) {
  return {
    tab: 'tab-cost', session: null, scope: 'main', cohort: null,
    tables: [
      { id: 'first', columns: ['target'], rows: [{ target: 'a.json' }, { target: 'b.md' }] },
      { id: 'second', columns: ['ts'], rows: [
        { session_id: 's2', ts: '10:00', preview: 'needle in here' },
        { session_id: 's9', ts: '11:00', preview: 'other' },
      ] },
    ],
    figures: [], text: [], plotly: [],
    meta: [
      { id: 'first', title: 'First', columns: [], filterable: true, page_size: null },
      { id: 'second', title: 'Second', columns: [], filterable: true, page_size: null },
    ],
    ...over,
  }
}

function show(search: string) {
  window.history.replaceState(null, '', search)
  // retry: false, because main.tsx retries once and every error case would otherwise wait for it.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(<QueryClientProvider client={client}><App /></QueryClientProvider>)
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  vi.mocked(api.tabs).mockResolvedValue(TABS)
  vi.mocked(api.health).mockResolvedValue({
    ok: true, db: 'data/context.db', pid: 1, read_only: true, writes_enabled: false, cache: {},
  })
  vi.mocked(api.cohorts).mockResolvedValue([])
  vi.mocked(api.selector).mockResolvedValue([])
  // One account directory, so the Account switch renders nothing and these tests keep describing
  // the header they were written for.
  vi.mocked(api.accounts.state).mockResolvedValue({
    supported: true, why_not: '', app_running: false, mode: 'current', intended: 'current',
    roots: [], pairs: 1, linked: 0, chats_visible: 0, current_chats: 0,
    intended_source: 'none', uncovered: [],
  })
  // Nothing to adopt, so the header shows no adopt control in these tests.
  vi.mocked(api.adopt.state).mockResolvedValue({
    supported: true, why_not: '', pair: null, physical: null, groups: [], candidates: 0,
    cli_candidates: 0, other_account: 0, deleted_markers: 0, untitled_adopted: 0,
    review_runs: 0, review_records: 0, app_running: false, sharing: null,
  })
  vi.mocked(api.adopt.sweep).mockResolvedValue({ enabled: true, last: null })
  vi.mocked(api.tab).mockResolvedValue(payload())
})

describe('an address that names a table of a tab that will not build', () => {
  it('still offers the way back, and the way back works', async () => {
    vi.mocked(api.tab).mockRejectedValue(new ApiError('500 from /api/tab/tab-cost/render', 500))
    show('/?tab=tab-cost&view=table&table=0')
    expect(await screen.findByText('This tab did not load')).toBeTruthy()
    // One control, and it is not TablePage's own: that renders only once a payload arrives.
    fireEvent.click(screen.getByText('Back to the dashboard'))
    expect(await screen.findByRole('button', { name: 'Search tabs, populations and sessions' }))
      .toBeTruthy()
  })
})

describe('the palette shortcut', () => {
  it('opens on the dashboard, and does not arm itself behind the single-table page', async () => {
    const first = show('/?tab=tab-cost')
    await screen.findByRole('button', { name: 'Search tabs, populations and sessions' })
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    expect(screen.getByRole('dialog')).toBeTruthy()
    // UNMOUNTED before the second case: two Apps in one document share the window's keydown
    // listener and the first one's palette would answer for the second's.
    first.unmount()

    show('/?tab=tab-cost&view=table&table=0')
    await screen.findByRole('heading', { level: 1 })
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    // The palette is not drawn on this page at all, so "no dialog here" proves nothing. What the
    // guard is for is the RETURN: without it the dashboard opens with a palette nobody asked for.
    fireEvent.click(screen.getByText('Back to the dashboard'))
    await screen.findByRole('button', { name: 'Search tabs, populations and sessions' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('the address the reader edits', () => {
  it('restores the tab, the selection, the view, the table, the filter box and the exact filter', async () => {
    show('/?tab=tab-summary')
    await waitFor(() => expect(api.tab).toHaveBeenCalled())
    window.history.replaceState(null, '',
      '/?tab=tab-cost&session=s2&scope=all&cohort=project::x&view=table&table=1&q=needle&key=session_id&val=s2')
    fireEvent(window, new PopStateEvent('popstate'))

    await waitFor(() => expect(vi.mocked(api.tab).mock.lastCall?.[0]).toBe('tab-cost'))
    expect(vi.mocked(api.tab).mock.lastCall?.[1]).toMatchObject({
      session: 's2', scope: 'all', cohort: 'project::x',
    })
    const heading = await screen.findByRole('heading', { level: 1 })
    expect(heading.textContent).toContain('Second')
    expect((screen.getByLabelText('Filter Second') as HTMLInputElement).value).toBe('needle')
    expect(screen.getByText(/Filtered to session_id = s2, 1 of 2 rows/)).toBeTruthy()
  })
})

describe('the filter typed on the single-table page', () => {
  it('reaches the address, and is not wiped by the round trip back into the box', async () => {
    show('/?tab=tab-cost&view=table&table=0')
    const box = await screen.findByLabelText('Filter First')
    fireEvent.change(box, { target: { value: 'b.m' } })
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get('q')).toBe('b.m'))
    // The review warned that syncing the prop would wipe what was typed the moment a writer
    // appeared. A writer did appear, in the same commit. This is the check it asked for.
    expect((box as HTMLInputElement).value).toBe('b.m')
    expect(screen.getByText('b.md')).toBeTruthy()
    expect(screen.queryByText('a.json')).toBeNull()
  })
})

describe('the population chip', () => {
  it('carries the population, and does NOT carry the whole view description', async () => {
    vi.mocked(api.tab).mockResolvedValue(payload({
      population: 'Store-wide. Not affected by the header selection.',
      population_scope: 'store',
      about: ['This tab describes the capture machinery and the window math.'],
      text: ['This tab describes the capture machinery and the window math.'],
    }))
    show('/?tab=tab-cost')
    const chip = await screen.findByText(/Store-Wide/)
    // The chip briefly carried both. On the Diagnostics tab that was 826 characters across six
    // paragraphs as the tooltip of an element 61 pixels wide, which is a wall with no scrollbar.
    expect(chip.getAttribute('title')).toBe('Store-wide. Not affected by the header selection.')
    expect(chip.getAttribute('title')).not.toContain('capture machinery')
    expect(chip.textContent).not.toContain('?')
  })

  it('shows no glyph when the view has nothing to add to its population', async () => {
    // A population and no `about`: the chip still states what the numbers cover, and there is
    // nothing further to hover for, so nothing should say there is.
    vi.mocked(api.tab).mockResolvedValue(payload({
      population: 'Store-wide. Not affected by the header selection.',
      population_scope: 'store',
    }))
    show('/?tab=tab-cost')
    const chip = await screen.findByText(/Store-Wide/)
    expect(chip.textContent).not.toContain('?')
    expect(chip.getAttribute('title')).toBe('Store-wide. Not affected by the header selection.')
  })

  it('says nothing at all when the tab is describing the selection', async () => {
    // THE CASE THE CHIP WAS REMOVED FOR. It used to read "This Selection" here, on screen
    // whatever was picked, which is decoration rather than a fact anybody can act on. What
    // survives is the warning above: a tab describing the whole store while a selection is set.
    vi.mocked(api.tab).mockResolvedValue(payload({
      population: 'Describing session s-1.',
      population_scope: 'selection',
    }))
    show('/?tab=tab-session')
    await screen.findByRole('button', { name: 'Search tabs, populations and sessions' })
    expect(screen.queryByText('This Selection')).toBeNull()
    expect(screen.queryByText('Store-Wide')).toBeNull()
  })
})

describe('the page', () => {
  it('has no footer, so no tab ends in this process cache counters', async () => {
    show('/?tab=tab-cost')
    await screen.findByRole('button', { name: 'Search tabs, populations and sessions' })
    expect(document.querySelector('footer')).toBeNull()
    expect(screen.queryByText(/cache .* hit/)).toBeNull()
    expect(screen.queryByText(/this server never writes to the store/)).toBeNull()
  })
})

describe('what a view says about itself', () => {
  it('reaches the OPEN tab and no other, which is the only reason the disclosure was removed', async () => {
    // An independent review deleted the `about` wiring from Sidebar.tsx entirely and the whole
    // frontend suite stayed green. The justification for taking the About disclosure out of the
    // body was that the tab carries it; nothing asserted that it does.
    vi.mocked(api.tab).mockResolvedValue(payload({
      about: ['This tab describes the capture machinery and the window math.'],
      text: ['This tab describes the capture machinery and the window math.'],
    }))
    show('/?tab=tab-cost')
    const open = await screen.findByRole('tab', { name: 'Cost' })
      .catch(() => screen.getByText('Cost').closest('button')!)
    const other = screen.getByText('Summary').closest('button')!
    await waitFor(() =>
      expect(open.getAttribute('title')).toContain('capture machinery'))
    // Only the open one: the others have not been built, so there is nothing honest to put on them.
    expect(other.getAttribute('title') ?? '').not.toContain('capture machinery')
    // And it is not ALSO printed in the body.
    expect(screen.queryByText(/capture machinery/)).toBeNull()
  })
})

describe('the header, as the user asked for it', () => {
  it('puts Adopted Chats after the server controls, set apart by a divider', async () => {
    vi.mocked(api.adopt.state).mockResolvedValue({
      supported: true, why_not: '', pair: { account: 'a', org: 'o', root: 'R', source: 's' },
      physical: 'R/a/o', candidates: 1, cli_candidates: 0, other_account: 0, deleted_markers: 0,
      untitled_adopted: 0, review_runs: 0, review_records: 0, app_running: false, sharing: null,
      groups: [{ cwd: 'P:/Alpha', project: 'Alpha', count: 1, newest: '2026-08-01T00:04:00Z', sessions: [] }],
    })
    show('/')
    const adopt = await screen.findByRole('button', { name: /^Adopt \(\d+\)$/ })
    expect(adopt.getAttribute('title')).toContain('no record of')
    const server = screen.getByRole('group', { name: 'C4X server' })
    expect(server.compareDocumentPosition(adopt) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(server.className).toContain('border-l')
    expect(adopt.parentElement?.className).toContain('border-l')
  })

  it('offers the populations as a combobox whose rows name their full path on hover', async () => {
    vi.mocked(api.cohorts).mockResolvedValue([
      { value: '__all__', label: 'All sessions (2 listed)', path: 'Every chat the store lists' },
      { value: 'project::P:/ClaudeExt/ccxe/c4x', label: 'c4x (1 listed)', path: 'P:/ClaudeExt/ccxe/c4x' },
    ])
    show('/')
    const control = await screen.findByRole('combobox', { name: 'Population' })
    expect(screen.queryByRole('option')).toBeNull()
    fireEvent.click(control)
    // The cohorts arrive after the first paint; the open list picks them up as they do.
    await screen.findByRole('option', { name: /c4x/ })
    const rows = screen.getAllByRole('option')
    expect(rows.map((r) => r.getAttribute('title'))).toEqual(
      ['Every chat the store lists', 'Every chat the store lists', 'P:/ClaudeExt/ccxe/c4x'])
    fireEvent.click(rows[2])
    expect(window.location.search).toContain('cohort=')
  })
})
