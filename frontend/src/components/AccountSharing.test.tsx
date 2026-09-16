import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
import { AccountSharing, COVER_HOVER, COVER_NOTE, QUIT_NOTE, RESTART_NOTE } from './AccountSharing'
import { api, ApiError } from '@/api'
import type { AccountPair, AccountsState } from '@/api'

/**
 * The toggle that decides whether every account signed into this machine reads one chat list.
 *
 * The cases that matter are the ones that stop it: one account has nothing to share, a server that
 * refuses writes cannot move directories, and Claude being open is a refusal the reader has to be
 * able to act on rather than a failure. The numbers live on hover, so what is asserted is the
 * title attributes and the ABSENCE of the sentence they replaced.
 *
 * PLAIN ASSERTIONS, no `jest-dom`. This suite does not register those matchers, and a test file
 * that assumes them fails on every line at once, which reads as a broken component.
 */
function state(over: Partial<AccountsState> = {}): AccountsState {
  return {
    supported: true,
    why_not: '',
    app_running: false,
    mode: 'current',
    intended: 'current',
    roots: [],
    pairs: 2,
    linked: 0,
    chats_visible: 187,
    current_chats: 12,
    intended_source: 'none',
    uncovered: [],
    ...over,
  }
}

function pair(over: Partial<AccountPair> = {}): AccountPair {
  return {
    root: 'C:\\r', account: 'ba7ccf25-0000-4000-8000-000000000000',
    org: 'ebefad6b-0000-4000-8000-000000000000',
    path: 'C:\\r\\ba7ccf25\\ebefad6b', link_to: null, records: 15, ...over,
  }
}

function draw(props: { writesEnabled?: boolean; onChanged?: () => void } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const view = render(
    <QueryClientProvider client={client}>
      <AccountSharing writesEnabled={props.writesEnabled ?? true} onChanged={props.onChanged} />
    </QueryClientProvider>,
  )
  return { ...view, client }
}

beforeEach(() => {
  vi.restoreAllMocks()
  focusManager.setFocused(undefined)
})

/** The confirm every switch opens: press Continue in it. */
async function goOn() {
  fireEvent.click(await screen.findByRole('button', { name: 'Continue' }))
}

/** What the confirm says once the write ran. */
const outcome = () => screen.findByTestId('confirm-outcome')

describe('AccountSharing', () => {
  it('offers nothing when the machine has one account directory', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ pairs: 1 }))
    const { container } = draw()
    await waitFor(() => expect(api.accounts.state).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })

  it('offers nothing where a junction is not a thing', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ supported: false, why_not: 'junctions are a Windows feature and this is Linux' }),
    )
    const { container } = draw()
    await waitFor(() => expect(api.accounts.state).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })

  it('shows which side is on, from what was asked for rather than what is on disk', async () => {
    // The two differ after an app update migrates the directories, and the control has to keep
    // describing the setting rather than the accident.
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ intended: 'all', mode: 'mixed' }))
    draw()
    const all = await screen.findByRole('button', { name: 'All' })
    expect(all.getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: 'Current' }).getAttribute('aria-pressed'))
      .toBe('false')
  })

  it('says on hover how many chats each side shows, and nothing in the row', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state())
    draw()
    const all = await screen.findByRole('button', { name: 'All' })
    const notes = '\n' + QUIT_NOTE + '\n' + RESTART_NOTE
    expect(all.getAttribute('title')).toBe(
      "Every account's chats: 187 across 2 account directories" + notes)
    expect(screen.getByRole('button', { name: 'Current' }).getAttribute('title')).toBe(
      "Only the signed-in account's own chats: 12" + notes)
    expect(screen.queryByText(/187/)).toBeNull()
    expect(screen.queryByText(/shared across/)).toBeNull()
    expect(screen.queryByText(/each with its own chats/)).toBeNull()
    expect(screen.queryByText(/Quit Claude before switching/)).toBeNull()
    expect(screen.queryByText('Account')).toBeNull()
    expect(screen.getByRole('group', { name: 'Account' })).not.toBeNull()
  })

  it('says the Current number is not known when the server cannot tell', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ current_chats: null }))
    draw()
    const current = await screen.findByRole('button', { name: 'Current' })
    expect(current.getAttribute('title')).toContain('not known while sharing is on')
  })

  it('carries the restart note on the buttons, and marks the switch after a change', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state())
    const share = vi.spyOn(api.accounts, 'share').mockResolvedValue({
      mode: 'all', dry_run: false, restart_required: true, backup: 'C:\\x\\1',
      state: state({ intended: 'all', mode: 'all', linked: 1 }),
    })
    const onChanged = vi.fn()
    draw({ onChanged })
    const all = await screen.findByRole('button', { name: 'All' })
    expect(all.getAttribute('title')).toContain(RESTART_NOTE)
    const group = screen.getByRole('group', { name: 'Account' })
    expect(group.getAttribute('data-restart')).toBe('false')
    fireEvent.click(all)
    // Nothing moves until the confirm's Continue, which sends the restart flag.
    expect(share).not.toHaveBeenCalled()
    await goOn()
    await waitFor(() => expect(share).toHaveBeenCalledWith('all', true))
    await waitFor(() => expect(group.getAttribute('data-restart')).toBe('true'))
    expect(group.className).toContain('border-warn')
    expect(screen.queryByText(/Restart Claude/)).toBeNull()
    expect(onChanged).toHaveBeenCalled()
    // The report's state replaces the one read on mount: All is now the side that is on.
    expect(screen.getByRole('button', { name: 'All' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('shows the refusal the server sends, in the confirm, and marks nothing', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state())
    vi.spyOn(api.accounts, 'share').mockRejectedValue(
      new ApiError('409 from /api/accounts/sharing', 409, {
        error: 'a restart is already under way; wait for it to finish',
      }),
    )
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'All' }))
    await goOn()
    expect((await outcome()).textContent).toContain('already under way')
    expect(screen.getByRole('group', { name: 'Account' }).getAttribute('data-restart')).toBe('false')
  })

  it('says whether Claude came back, from the report, once the switch ran', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ app_running: true }))
    vi.spyOn(api.accounts, 'share').mockResolvedValue({
      mode: 'all', dry_run: false, restart_required: false, backup: null,
      state: state({ intended: 'all', mode: 'all', linked: 1 }),
      restart: { was_running: true, quit: true, killed: 12, relaunched: true, how: 'task',
                 launch: ['explorer.exe'], why: 'relaunched through the task scheduler' },
    })
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'All' }))
    const dialog = screen.getByRole('dialog', { name: /every account/ })
    expect(dialog.textContent).toContain('closes every Claude window')
    await goOn()
    expect((await outcome()).textContent)
      .toBe('The directories are linked. Claude was closed and started again through the task scheduler.')
    // Started again: no amber mark, nothing left for the person to restart.
    expect(screen.getByRole('group', { name: 'Account' }).getAttribute('data-restart')).toBe('false')
  })

  it('says nothing is quit when Claude is not running', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ app_running: false }))
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'All' }))
    expect(screen.getByRole('dialog', { name: /every account/ }).textContent)
      .toContain('Claude is not running, so nothing is quit')
  })

  it('is disabled on a server that answers no writes', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state())
    const share = vi.spyOn(api.accounts, 'share')
    draw({ writesEnabled: false })
    const button = (await screen.findByRole('button', { name: 'All' })) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
    expect(share).not.toHaveBeenCalled()
  })

  it('reads the state again when a sibling says something changed, and when the page comes back', async () => {
    const read = vi.spyOn(api.accounts, 'state').mockResolvedValue(state())
    const { client } = draw()
    await screen.findByRole('button', { name: 'All' })
    expect(read).toHaveBeenCalledTimes(1)
    await act(() => client.invalidateQueries())
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    act(() => {
      focusManager.setFocused(false)
      focusManager.setFocused(true)
    })
    await waitFor(() => expect(read).toHaveBeenCalledTimes(3))
  })
})

describe('the pairs sharing does not cover yet', () => {
  it('says nothing under Current, and nothing under All with every pair covered', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'current', uncovered: [pair()] }))
    draw()
    await screen.findByRole('button', { name: 'All' })
    expect(screen.queryByText(/not yet covered/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cover now' })).toBeNull()
  })

  it('names the count and when it is covered, with the paths on hover', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', mode: 'mixed', uncovered: [
        pair({ why: 'not linked' }), pair({ path: 'C:\\r\\x\\y', why: 'points elsewhere' })] }))
    draw()
    const line = await screen.findByText(/2 account pairs not yet covered/)
    expect(line.textContent).toContain(COVER_NOTE)
    const row = line.closest('p') as HTMLElement
    expect(row.getAttribute('data-uncovered')).toBe('2')
    expect(row.getAttribute('title')).toContain(COVER_HOVER)
    expect(row.getAttribute('title')).toContain('C:\\r\\x\\y: points elsewhere')
    expect(row.getAttribute('title')).toContain(': not linked')
    const button = screen.getByRole('button', { name: 'Cover now' }) as HTMLButtonElement
    expect(button.disabled).toBe(false)
  })

  it('asks before covering while Claude is open, naming the close, and Cancel covers nothing', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', app_running: true, uncovered: [pair()] }))
    const reconcile = vi.spyOn(api.accounts, 'reconcile')
    draw()
    expect(await screen.findByText(/1 account pair not yet covered/)).not.toBeNull()
    const button = screen.getByRole('button', { name: 'Cover now' }) as HTMLButtonElement
    expect(button.disabled).toBe(false)
    expect(button.getAttribute('title')).toContain('after asking')
    fireEvent.click(button)
    const dialog = screen.getByRole('dialog', { name: 'Cover now' })
    expect(dialog.textContent).toContain('closes every Claude window')
    expect(dialog.textContent).toContain('moves the uncovered account directories')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(reconcile).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('is disabled on a server that answers no writes', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', uncovered: [pair()] }))
    draw({ writesEnabled: false })
    const button = (await screen.findByRole('button', { name: 'Cover now' })) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(button.getAttribute('title')).toContain('without writes')
  })

  it('covers on a click, replaces the state and marks the restart', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', uncovered: [pair()] }))
    const after = state({ intended: 'all', mode: 'all', linked: 2, uncovered: [],
      intended_source: 'marker' })
    const reconcile = vi.spyOn(api.accounts, 'reconcile').mockResolvedValue({
      ran: true, why: 'covered 1 pair(s)', app_running: false, pending: [], backup: 'C:\\b\\1',
      marker_written: true, restart_required: true, state: after,
    })
    const onChanged = vi.fn()
    draw({ onChanged })
    fireEvent.click(await screen.findByRole('button', { name: 'Cover now' }))
    await goOn()
    await waitFor(() => expect(reconcile).toHaveBeenCalledWith(true))
    await waitFor(() => expect(screen.queryByText(/not yet covered/)).toBeNull())
    const group = screen.getByRole('group', { name: 'Account' })
    expect(group.getAttribute('data-restart')).toBe('true')
    expect(onChanged).toHaveBeenCalled()
  })

  it('shows the sentence a 409 carries', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', uncovered: [pair()] }))
    vi.spyOn(api.accounts, 'reconcile').mockRejectedValue(
      new ApiError('409 from /api/accounts/reconcile', 409, {
        error: 'Claude could not be closed; nothing has been changed. Quit it yourself and try again.',
        pending: [pair()],
      }),
    )
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Cover now' }))
    await goOn()
    expect((await outcome()).textContent).toContain('Quit it yourself and try again')
    expect(screen.getByText(/not yet covered/)).not.toBeNull()
  })
})


describe('every switch asks first, and says where the Current number came from', () => {
  it('opens a confirm on Current and calls nothing until Continue', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ intended: 'all', mode: 'all' }))
    const share = vi.spyOn(api.accounts, 'share').mockResolvedValue({
      mode: 'current', dry_run: false, restart_required: true, backup: null,
      state: state({ intended: 'current', mode: 'current' }),
    })
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Current' }))
    expect(share).not.toHaveBeenCalled()
    const ask = screen.getByRole('dialog', { name: /signed-in account/ })
    expect(ask.textContent).toContain('un-links the account directories')
    expect(ask.textContent).toContain('takes chats away from every other account')
    await goOn()
    await waitFor(() => expect(share).toHaveBeenCalledTimes(1))
    expect(share).toHaveBeenCalledWith('current', true)
    fireEvent.click(await screen.findByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('keeps sharing when told to', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ intended: 'all', mode: 'all' }))
    const share = vi.spyOn(api.accounts, 'share')
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Current' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(share).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'All' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('asks before All as well, the user\'s example, and only Continue switches', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state())
    const share = vi.spyOn(api.accounts, 'share').mockResolvedValue({
      mode: 'all', dry_run: false, restart_required: true, backup: null,
      state: state({ intended: 'all', mode: 'all' }),
    })
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'All' }))
    expect(share).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog', { name: /every account/ })).not.toBeNull()
    await goOn()
    await waitFor(() => expect(share).toHaveBeenCalledWith('all', true))
  })

  it('says the Current number is by the account each chat was made under, with the untagged', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', mode: 'all', current_chats: 12, current_source: 'tags', untagged: 3 }))
    draw()
    const current = await screen.findByRole('button', { name: 'Current' })
    expect(current.getAttribute('title')).toContain(
      "Only the signed-in account's own chats: 12, by the account each chat was made under; 3 of unknown account")
    expect(current.getAttribute('title')).toContain(RESTART_NOTE)
  })

  it('keeps the manifest wording when the number came from the manifest', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', mode: 'all', current_chats: 12, current_source: 'manifest' }))
    draw()
    const current = await screen.findByRole('button', { name: 'Current' })
    expect(current.getAttribute('title')).toContain("Only the signed-in account's own chats: 12\n")
    expect(current.getAttribute('title')).not.toContain('made under')
  })
})


describe('one number for the signed-in account', () => {
  it('leads with what the page lists and follows with what the app lists', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', mode: 'all', chats_visible: 191, pairs: 9, current_chats: 175,
        current_source: 'tags', untagged: 1, listed: 489, current_listed: 107 }))
    draw()
    const current = await screen.findByRole('button', { name: 'Current' })
    expect(current.getAttribute('title')).toContain(
      "Only the signed-in account's own chats: 107 listed here; 175 in the app; 1 of unknown account")
    expect(screen.getByRole('button', { name: 'All' }).getAttribute('title')).toContain(
      "Every account's chats: 489 listed here; 191 in the app across 9 account directories")
  })

  it('says the app count is not known when it is not, and still leads with the page count', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', mode: 'all', current_chats: null, current_source: null,
        listed: 489, current_listed: 107 }))
    draw()
    const current = await screen.findByRole('button', { name: 'Current' })
    expect(current.getAttribute('title')).toContain(
      "Only the signed-in account's own chats: 107 listed here; not known while sharing is on in the app")
  })

  it('keeps the older sentences when the server has no page count', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ current_chats: 12, listed: null, current_listed: null }))
    draw()
    const current = await screen.findByRole('button', { name: 'Current' })
    expect(current.getAttribute('title')).toContain("Only the signed-in account's own chats: 12\n")
    expect(screen.getByRole('button', { name: 'All' }).getAttribute('title')).toContain(
      "Every account's chats: 187 across 2 account directories")
  })
})
