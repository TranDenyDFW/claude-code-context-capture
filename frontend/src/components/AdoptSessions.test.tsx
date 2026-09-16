import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AdoptSessions } from './AdoptSessions'
import { api, ApiError } from '@/api'
import type { AdoptReport, AdoptState, RetitleReport } from '@/api'

/**
 * The control that gives Claude a record for the chats it has no record of.
 *
 * What matters is what it refuses to do on its own: nothing is preselected, a machine with
 * nothing to adopt shows nothing, and the count of chats the app deleted on purpose is said
 * before any folder can be ticked.
 *
 * PLAIN ASSERTIONS, no `jest-dom`, as in the sibling suites.
 */
function state(over: Partial<AdoptState> = {}): AdoptState {
  return {
    supported: true,
    why_not: '',
    pair: { account: 'a', org: 'o', root: 'R', source: 'config.json' },
    physical: 'R/a/o',
    groups: [
      { cwd: 'P:\\Gamma', project: 'Gamma', count: 2, newest: '2026-08-03T12:01:00Z', sessions: [] },
      { cwd: 'P:\\Alpha', project: 'Alpha', count: 1, newest: '2026-08-01T00:04:00Z', sessions: [] },
    ],
    candidates: 3,
    cli_candidates: 4,
    other_account: 1,
    deleted_markers: 0,
    untitled_adopted: 0,
    review_runs: 0,
    review_records: 0,
    app_running: false,
    sharing: 'current',
    ...over,
  }
}

function report(over: Partial<AdoptReport> = {}): AdoptReport {
  return {
    supported: true,
    why_not: '',
    pair: { account: 'a', org: 'o', root: 'R', source: 'config.json' },
    physical: 'R/a/o',
    note: null,
    written: [{ session_id: 's1', path: 'R/a/o/local_1.json', title: null }],
    skipped: [],
    selected: 1,
    restart_required: true,
    dry_run: false,
    ...over,
  }
}

function named(over: Partial<RetitleReport> = {}): RetitleReport {
  return { renamed: [{ session_id: 's1', path: 'p1', title: 'raw prompt' }], kept: 0, missing: 0,
           restart_required: true, ...over }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

const ADOPT = /^Adopt \(\d+\)$/

async function opened() {
  fireEvent.click(await screen.findByRole('button', { name: ADOPT }))
}

describe('AdoptSessions', () => {
  it('names the nameless records a first build wrote, then asks for a restart', async () => {
    const stateCall = vi.spyOn(api.adopt, 'state')
      .mockResolvedValueOnce(state({ groups: [], cli_candidates: 0, untitled_adopted: 64 }))
      .mockResolvedValueOnce(state({ groups: [], cli_candidates: 0, untitled_adopted: 0 }))
    const retitle = vi.spyOn(api.adopt, 'retitle').mockResolvedValue(named({
      renamed: Array.from({ length: 64 }, (_, i) => ({ session_id: `s${i}`, path: `p${i}`, title: `t${i}` })),
    }))
    const onChanged = vi.fn()
    render(<AdoptSessions writesEnabled onChanged={onChanged} />)
    const button = await screen.findByRole('button', { name: ADOPT })
    expect(button.textContent).toBe('Adopt (0)')
    expect(button.getAttribute('title')).toBe('64 adopted chats without a name')
    await opened()
    expect(screen.getByText(/64 adopted chats have no name yet/).textContent)
      .toContain('General coding session')
    fireEvent.click(screen.getByRole('button', { name: 'Name them' }))
    await waitFor(() => expect(retitle).toHaveBeenCalled())
    expect(await screen.findByText(/Restart Claude to see the 64 names/)).not.toBeNull()
    expect(onChanged).toHaveBeenCalled()
    expect(stateCall).toHaveBeenCalledTimes(2)
  })

  it('opens as a window over the page, with focus in the search box and back on the button', async () => {
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state())
    const { container } = render(<AdoptSessions writesEnabled />)
    const trigger = await screen.findByRole('button', { name: ADOPT })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog', { name: 'Adopted Chats' })
    // Not inside the header's subtree: a fixed element there is clipped to the header's box.
    expect(container.contains(dialog)).toBe(false)
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    // The page is dimmed behind it: a backdrop at the end of the document, the project
    // dialog's way, the user's choice over the side drawer this used to be.
    const backdrop = dialog.parentElement as HTMLElement
    expect(backdrop.getAttribute('role')).toBe('presentation')
    expect(backdrop.parentElement).toBe(document.body)
    const search = screen.getByRole('searchbox', { name: 'Search folders and chats' })
    expect(document.activeElement).toBe(search)
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    expect(trigger.getAttribute('aria-controls')).toBe(dialog.id)
    fireEvent.keyDown(search, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('lays the folders out as a table, and narrows it as you type by folder, path and chat title', async () => {
    const chat = (id: string, title: string) => ({
      session_id: id, title, title_source: 'auto' as const, first_ts: '2026-08-01T00:00:00Z',
      last_ts: '2026-08-03T12:01:00Z', turns: 7, model: 'claude-opus-5', cli: false, cwd: 'x',
    })
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state({ groups: [
      { cwd: 'P:\\Gamma', project: 'Gamma', count: 2, newest: '2026-08-03T12:01:00Z',
        sessions: [chat('g1', 'Wire the hooks'), chat('g2', 'Second thoughts')] },
      { cwd: 'P:\\Alpha', project: 'Alpha', count: 1, newest: '2026-08-01T00:04:00Z',
        sessions: [chat('a1', 'Fix the exporter')] },
      { cwd: 'D:\\Work\\Beta', project: 'Beta', count: 1, newest: '2026-07-01T00:04:00Z', sessions: [] },
    ] }))
    render(<AdoptSessions writesEnabled />)
    await opened()
    const offered = () =>
      screen.getAllByRole('checkbox', { name: /^Adopt / }).map((b) => b.getAttribute('aria-label'))
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent))
      .toEqual(['Adopt', 'Folder', 'Chats', 'Newest', 'Path'])
    expect(offered()).toEqual(['Adopt Gamma', 'Adopt Alpha', 'Adopt Beta'])
    // A folder holding one chat carries that chat's title, the way the population list names it.
    expect(screen.getByText('Fix the exporter')).not.toBeNull()
    expect(screen.queryByText('Second thoughts')).toBeNull()
    const search = screen.getByRole('searchbox', { name: 'Search folders and chats' })
    fireEvent.change(search, { target: { value: 'gam' } })
    expect(offered()).toEqual(['Adopt Gamma'])
    expect(screen.getByText('1 of 3 folders match')).not.toBeNull()
    fireEvent.change(search, { target: { value: 'd:\\work' } })
    expect(offered()).toEqual(['Adopt Beta'])
    // A chat title matched: the folder opens so the row shows why it is there.
    fireEvent.change(search, { target: { value: 'second' } })
    expect(offered()).toEqual(['Adopt Gamma'])
    expect(screen.getByText('Second thoughts')).not.toBeNull()
    fireEvent.change(search, { target: { value: 'zzz' } })
    expect(screen.queryAllByRole('checkbox', { name: /^Adopt / })).toEqual([])
    expect(screen.getByText('No folder matches that.')).not.toBeNull()
    expect(screen.getByText('0 of 3 folders match')).not.toBeNull()
    // Escape clears the search first; only an empty search lets it close the window.
    fireEvent.keyDown(search, { key: 'Escape' })
    expect((search as HTMLInputElement).value).toBe('')
    expect(screen.getByRole('dialog', { name: 'Adopted Chats' })).not.toBeNull()
    expect(offered()).toEqual(['Adopt Gamma', 'Adopt Alpha', 'Adopt Beta'])
    fireEvent.keyDown(search, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('opens a folder to its chats on request, and Select all takes the folders shown', async () => {
    const chat = (id: string, title: string) => ({
      session_id: id, title, title_source: 'auto' as const, first_ts: '2026-08-01T00:00:00Z',
      last_ts: '2026-08-03T12:01:00Z', turns: 7, model: 'claude-opus-5', cli: true, cwd: 'x',
    })
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state({ groups: [
      { cwd: 'P:\\Gamma', project: 'Gamma', count: 2, newest: '2026-08-03T12:01:00Z',
        sessions: [chat('g1', 'Wire the hooks'), chat('g2', 'Second thoughts')] },
      { cwd: 'P:\\Alpha', project: 'Alpha', count: 1, newest: '2026-08-01T00:04:00Z', sessions: [] },
    ] }))
    const run = vi.spyOn(api.adopt, 'run').mockResolvedValue(report())
    render(<AdoptSessions writesEnabled />)
    await opened()
    fireEvent.click(screen.getByRole('button', { name: 'Show the chats in Gamma' }))
    expect(screen.getByText('Wire the hooks')).not.toBeNull()
    expect(screen.getAllByText('7 turns, 2026-08-03, CLI')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: 'Hide the chats in Gamma' }))
    expect(screen.queryByText('Wire the hooks')).toBeNull()
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search folders and chats' }),
                     { target: { value: 'alpha' } })
    fireEvent.click(screen.getByRole('button', { name: 'Select all' }))
    fireEvent.click(screen.getByRole('button', { name: /^Adopt 1 chat$/ }))
    await waitFor(() =>
      expect(run).toHaveBeenCalledWith({ cwds: ['P:\\Alpha'], include_cli: false, dry_run: false }))
  })

  it('shows a Runs column only when the server sends one', async () => {
    const stateCall = vi.spyOn(api.adopt, 'state').mockResolvedValue(state({ groups: [
      { cwd: 'P:\\Gamma', project: 'Gamma', count: 1, newest: '2026-08-03T12:01:00Z', sessions: [], runs: 870 },
      { cwd: 'P:\\Alpha', project: 'Alpha', count: 1, newest: '2026-08-01T00:04:00Z', sessions: [] },
    ] }))
    render(<AdoptSessions writesEnabled />)
    await opened()
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent))
      .toEqual(['Adopt', 'Folder', 'Chats', 'Newest', 'Runs', 'Path'])
    expect(screen.getByText('870')).not.toBeNull()
    expect(stateCall).toHaveBeenCalled()
  })

  it('offers to take back the records a first build wrote for review runs, then asks for a restart', async () => {
    vi.spyOn(api.adopt, 'state')
      .mockResolvedValueOnce(state({ groups: [], cli_candidates: 0, review_records: 58,
                                     review_runs: 3, deleted_in_app: 2 }))
      .mockResolvedValueOnce(state({ groups: [], cli_candidates: 0, review_runs: 61 }))
    const unadopt = vi.spyOn(api.adopt, 'unadoptReviews').mockResolvedValue({
      removed: Array.from({ length: 58 }, (_, i) => ({ session_id: `r${i}`, path: `p${i}`, reviewed: 'c' })),
      missing: 0, kept: 24, restart_required: true,
    })
    vi.spyOn(api.adopt, 'sweep').mockResolvedValue({
      enabled: true,
      last: { at: '2026-09-14T22:24:05Z', epoch: 1, removed: 4, missing: 0, failed: 0, restarted: true,
              restarted_epoch: 1, why: 'removed 4 review-run record(s); Claude restarted',
              restart: { restarted: true, killed: 6, launch: ['explorer.exe'], why: 'relaunched' } },
    })
    render(<AdoptSessions writesEnabled />)
    expect((await screen.findByRole('button', { name: ADOPT })).getAttribute('title'))
      .toBe('58 review runs still in Claude')
    await opened()
    expect(screen.getByText(/58 review runs are listed in Claude as a chat/)).not.toBeNull()
    expect(screen.getByText(/3 review runs on this machine are folded/)).not.toBeNull()
    expect(screen.getByText(/2 chats you deleted in Claude are left out/).textContent)
      .toContain('their transcripts stay on disk')
    expect((await screen.findByTestId('startup-sweep')).textContent)
      .toBe('Startup sweep at 2026-09-14 22:24:05 UTC: removed 4 review-run record(s); Claude restarted.')
    fireEvent.click(screen.getByRole('button', { name: 'Remove them from Claude' }))
    await waitFor(() => expect(unadopt).toHaveBeenCalled())
    expect(await screen.findByText(/Restart Claude to drop the 58 review runs/)).not.toBeNull()
    expect(screen.queryByRole('button', { name: /Remove them/ })).toBeNull()
  })

  it('shows nothing when every chat already has a record', async () => {
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state({ groups: [], cli_candidates: 0 }))
    const { container } = render(<AdoptSessions writesEnabled />)
    await waitFor(() => expect(api.adopt.state).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })

  it('shows nothing where there is no account to file a record under', async () => {
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state({ supported: false, why_not: 'no pair' }))
    const { container } = render(<AdoptSessions writesEnabled />)
    await waitFor(() => expect(api.adopt.state).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })

  it('offers the folders with NOTHING preselected and the button off until one is ticked', async () => {
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state())
    render(<AdoptSessions writesEnabled />)
    const button = await screen.findByRole('button', { name: ADOPT })
    expect(button.textContent).toBe('Adopt (3)')
    expect(button.getAttribute('title')).toBe('3 chats on this machine that Claude has no record of')
    await opened()
    const boxes = screen.getAllByRole('checkbox', { name: /Gamma|Alpha/ }) as HTMLInputElement[]
    expect(boxes.map((b) => b.checked)).toEqual([false, false])
    const adopt = screen.getByRole('button', { name: /^Adopt 0 chats$/ }) as HTMLButtonElement
    expect(adopt.disabled).toBe(true)
    fireEvent.click(boxes[0])
    expect((screen.getByRole('button', { name: /^Adopt 2 chats$/ }) as HTMLButtonElement).disabled)
      .toBe(false)
  })

  it('says how many chats the app deleted on purpose before anything can be ticked', async () => {
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state({ deleted_markers: 17 }))
    render(<AdoptSessions writesEnabled />)
    await opened()
    expect(screen.getByText(/Claude has deleted 17 chats/).textContent)
      .toContain('adopt only folders you recognise')
    expect(screen.getByText(/1 chat belong to another account/)).not.toBeNull()
  })

  it('adopts the ticked folders, then asks for a restart and refreshes', async () => {
    const stateCall = vi.spyOn(api.adopt, 'state')
      .mockResolvedValueOnce(state())
      .mockResolvedValueOnce(state({ groups: [], cli_candidates: 4 }))
    const run = vi.spyOn(api.adopt, 'run').mockResolvedValue(report({ written: [
      { session_id: 's1', path: 'p1', title: null },
      { session_id: 's2', path: 'p2', title: 't' },
      { session_id: 's3', path: 'p3', title: null },
    ], selected: 3 }))
    const onChanged = vi.fn()
    render(<AdoptSessions writesEnabled onChanged={onChanged} />)
    await opened()
    fireEvent.click(screen.getByRole('button', { name: 'Select all' }))
    fireEvent.click(screen.getByRole('button', { name: /^Adopt 3 chats$/ }))
    await waitFor(() =>
      expect(run).toHaveBeenCalledWith({ cwds: ['P:\\Gamma', 'P:\\Alpha'], include_cli: false, dry_run: false }))
    expect((await screen.findByText(/Restart Claude to see the 3 chats adopted/)).textContent)
      .toContain('reads these records when it starts')
    expect(onChanged).toHaveBeenCalled()
    expect(stateCall).toHaveBeenCalledTimes(2)
  })

  it('carries the sharing note when the bytes live in the shared directory', async () => {
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state())
    vi.spyOn(api.adopt, 'run').mockResolvedValue(report({
      note: 'sharing is on: these records live under the shared directory and stay there if sharing is turned off',
    }))
    render(<AdoptSessions writesEnabled />)
    await opened()
    fireEvent.click(screen.getByRole('checkbox', { name: /Alpha/ }))
    fireEvent.click(screen.getByRole('button', { name: /^Adopt 1 chat$/ }))
    expect(await screen.findByText(/sharing is on: these records live/)).not.toBeNull()
  })

  it('asks again with CLI sessions when the box is ticked', async () => {
    const stateCall = vi.spyOn(api.adopt, 'state').mockResolvedValue(state())
    render(<AdoptSessions writesEnabled />)
    await opened()
    fireEvent.click(screen.getByRole('checkbox', { name: /Include CLI and SDK sessions/ }))
    await waitFor(() => expect(stateCall).toHaveBeenLastCalledWith(true))
  })

  it('shows the refusal when sharing does not cover the signed-in pair', async () => {
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state())
    vi.spyOn(api.adopt, 'run').mockRejectedValue(
      new ApiError('409 from /api/adopt', 409, {
        error: 'sharing is on but the signed-in pair is not part of it; turn sharing off and on again, then adopt',
      }),
    )
    render(<AdoptSessions writesEnabled />)
    await opened()
    fireEvent.click(screen.getByRole('checkbox', { name: /Alpha/ }))
    fireEvent.click(screen.getByRole('button', { name: /^Adopt 1 chat$/ }))
    expect(await screen.findByText(/turn sharing off and on again/)).not.toBeNull()
    expect(screen.queryByText(/Restart Claude/)).toBeNull()
  })

  it('is disabled on a server that answers no writes', async () => {
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state())
    const run = vi.spyOn(api.adopt, 'run')
    render(<AdoptSessions writesEnabled={false} />)
    await opened()
    fireEvent.click(screen.getByRole('checkbox', { name: /Alpha/ }))
    const button = screen.getByRole('button', { name: /^Adopt 1 chat$/ }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
    expect(run).not.toHaveBeenCalled()
  })
})

describe('the startup sweep line', () => {
  it('says the sweep is off on a server started with --no-review-sweep', async () => {
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state({ groups: [], cli_candidates: 0, review_records: 1 }))
    vi.spyOn(api.adopt, 'sweep').mockResolvedValue({ enabled: false, last: null })
    render(<AdoptSessions writesEnabled />)
    await opened()
    expect((await screen.findByTestId('startup-sweep')).textContent).toContain('off on this server')
  })

  it('shows no line when the server cannot say', async () => {
    vi.spyOn(api.adopt, 'state').mockResolvedValue(state({ groups: [], cli_candidates: 0, review_records: 1 }))
    vi.spyOn(api.adopt, 'sweep').mockRejectedValue(new Error('404'))
    render(<AdoptSessions writesEnabled />)
    await opened()
    expect(screen.getByText(/1 review run is listed in Claude/)).not.toBeNull()
    expect(screen.queryByTestId('startup-sweep')).toBeNull()
  })
})
