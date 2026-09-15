import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
import { AccountSharing, QUIT_NOTE, RESTART_NOTE } from './AccountSharing'
import { api, ApiError } from '@/api'
import type { AccountsState } from '@/api'

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
    ...over,
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
    await waitFor(() => expect(share).toHaveBeenCalledWith('all'))
    await waitFor(() => expect(group.getAttribute('data-restart')).toBe('true'))
    expect(group.className).toContain('border-warn')
    expect(screen.queryByText(/Restart Claude/)).toBeNull()
    expect(onChanged).toHaveBeenCalled()
    // The report's state replaces the one read on mount: All is now the side that is on.
    expect(screen.getByRole('button', { name: 'All' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('says what to do when the server refuses because Claude is open', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state())
    vi.spyOn(api.accounts, 'share').mockRejectedValue(
      new ApiError('409 from /api/accounts/sharing', 409, {
        error: 'Claude is running, and a directory it has open cannot be moved. Quit Claude and '
          + 'try again; nothing has been changed.',
      }),
    )
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'All' }))
    expect(await screen.findByText(/Quit Claude and try again/)).not.toBeNull()
    expect(screen.getByRole('group', { name: 'Account' }).getAttribute('data-restart')).toBe('false')
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
