import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AccountSharing } from './AccountSharing'
import { api, ApiError } from '@/api'
import type { AccountsState } from '@/api'

/**
 * The toggle that decides whether every account signed into this machine reads one chat list.
 *
 * The cases that matter are the ones that stop it: one account has nothing to share, a server that
 * refuses writes cannot move directories, and Claude being open is a refusal the reader has to be
 * able to act on rather than a failure.
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
    ...over,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('AccountSharing', () => {
  it('offers nothing when the machine has one account directory', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ pairs: 1 }))
    const { container } = render(<AccountSharing writesEnabled />)
    await waitFor(() => expect(api.accounts.state).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })

  it('offers nothing where a junction is not a thing', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ supported: false, why_not: 'junctions are a Windows feature and this is Linux' }),
    )
    const { container } = render(<AccountSharing writesEnabled />)
    await waitFor(() => expect(api.accounts.state).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })

  it('shows which side is on, from what was asked for rather than what is on disk', async () => {
    // The two differ after an app update migrates the directories, and the control has to keep
    // describing the setting rather than the accident.
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ intended: 'all', mode: 'mixed' }))
    render(<AccountSharing writesEnabled />)
    const all = await screen.findByRole('button', { name: 'All' })
    expect(all.getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: 'Current' }).getAttribute('aria-pressed'))
      .toBe('false')
  })

  it('switches, then asks for a restart', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state())
    const share = vi.spyOn(api.accounts, 'share').mockResolvedValue({
      mode: 'all', dry_run: false, restart_required: true, backup: 'C:\\x\\1',
      state: state({ intended: 'all', mode: 'all', linked: 1 }),
    })
    const onChanged = vi.fn()
    render(<AccountSharing writesEnabled onChanged={onChanged} />)
    fireEvent.click(await screen.findByRole('button', { name: 'All' }))
    await waitFor(() => expect(share).toHaveBeenCalledWith('all'))
    expect(await screen.findByText(/Restart Claude/)).not.toBeNull()
    expect(onChanged).toHaveBeenCalled()
  })

  it('says what to do when the server refuses because Claude is open', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state())
    vi.spyOn(api.accounts, 'share').mockRejectedValue(
      new ApiError('409 from /api/accounts/sharing', 409, {
        error: 'Claude is running, and a directory it has open cannot be moved. Quit Claude and '
          + 'try again; nothing has been changed.',
      }),
    )
    render(<AccountSharing writesEnabled />)
    fireEvent.click(await screen.findByRole('button', { name: 'All' }))
    expect(await screen.findByText(/Quit Claude and try again/)).not.toBeNull()
    expect(screen.queryByText(/Restart Claude/)).toBeNull()
  })

  it('is disabled on a server that answers no writes', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state())
    const share = vi.spyOn(api.accounts, 'share')
    render(<AccountSharing writesEnabled={false} />)
    const button = (await screen.findByRole('button', { name: 'All' })) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
    expect(share).not.toHaveBeenCalled()
  })
})
