import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AdoptSessions } from './AdoptSessions'
import { api, ApiError } from '@/api'
import type { AdoptReport, AdoptState } from '@/api'

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

beforeEach(() => {
  vi.restoreAllMocks()
})

async function opened() {
  fireEvent.click(await screen.findByRole('button', { name: /no record of/ }))
}

describe('AdoptSessions', () => {
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
    expect((await screen.findByRole('button', { name: /no record of/ })).textContent)
      .toContain('3 chats on this machine')
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
