import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
import {
  AccountSharing, COVER_HOVER, COVER_NOTE, MERGE_HOVER, QUIT_NOTE, REFUSED_HOVER,
  RESTART_NOTE,
} from './AccountSharing'
import { api, ApiError } from '@/api'
import type { AccountPair, AccountsState, MergeSurvey } from '@/api'

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

  it('gives the two sides one width, so the switch does not lean', async () => {
    // "All" is four characters and "Current" is seven. jsdom lays nothing out, so the rule is
    // what can be asserted, and the rule had to be measured first: `flex-1 basis-0` on the two
    // buttons leaves them at 34px and 61px in Chrome, because the box shrinks to fit and there
    // is no surplus to share. Two grid tracks take the wider content (measured: 61 and 61).
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state())
    draw()
    const all = await screen.findByRole('button', { name: 'All' })
    const current = screen.getByRole('button', { name: 'Current' })
    expect(all.parentElement).toBe(current.parentElement)
    expect(all.parentElement!.className).toContain('grid-cols-2')
    for (const option of [all, current]) expect(option.className).toContain('text-center')
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

describe('writes the app refused through the links', () => {
  /**
   * The failure that founded this row: the app lists records through a linked account directory
   * and refuses to save through one, so an archive is taken on screen and dropped. It appears
   * only in the app's own log, which the server reads; the row is the first place a reader can
   * see it. It shows whatever the mode says, because the links can still be on the disk while the
   * marker reads Current, and that is exactly the state that keeps losing writes.
   */
  const refused = {
    count: 2843,
    pairs: { 'ba7ccf25\\2108d9a2': 2521, 'ba7ccf25\\5f6eb959': 322 },
    last: '2026-10-10 12:47:50',
    file: 'C:\\Users\\Shake\\AppData\\Local\\Claude\\Logs\\main.log',
    said: '2843 refused record write(s) for 73 session(s)',
  }

  it('says nothing when the log holds none', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ intended: 'all', mode: 'all' }))
    draw()
    await screen.findByRole('button', { name: 'All' })
    expect(screen.queryByText(/refused/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Separate them' })).toBeNull()
  })

  it('names the count, carries the log line on hover, and offers the remedy', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', mode: 'all', linked: 8, refused_writes: refused }))
    draw()
    const line = await screen.findByText(/refused 2843 writes through these links/)
    const row = line.closest('p') as HTMLElement
    expect(row.getAttribute('data-refused')).toBe('2843')
    expect(row.getAttribute('title')).toContain(REFUSED_HOVER)
    expect(row.getAttribute('title')).toContain('73 session(s)')
    expect(row.getAttribute('title')).toContain('Logs\\main.log')
    expect((screen.getByRole('button', { name: 'Separate them' }) as HTMLButtonElement).disabled)
      .toBe(false)
  })

  it('shows it even while the marker reads Current, since the links are what lose the writes',
    async () => {
      vi.spyOn(api.accounts, 'state').mockResolvedValue(
        state({ intended: 'current', mode: 'mixed', linked: 1, refused_writes: refused }))
      draw()
      expect((await screen.findByText(/refused 2843 writes/)).textContent).toContain(
        'every change made under a linked account is lost')
    })

  it('the remedy asks first, and the question is the un-linking one', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', mode: 'all', refused_writes: refused }))
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Separate them' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toContain('un-links the account directories')
    expect(dialog.textContent).toContain('takes chats away from every other account')
  })

  it('cannot be acted on by a server started without writes', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(
      state({ intended: 'all', mode: 'all', refused_writes: refused }))
    draw({ writesEnabled: false })
    const button = await screen.findByRole('button', { name: 'Separate them' })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    expect(button.getAttribute('title')).toBe('This server was started without writes')
  })
})

describe('chats this account cannot see', () => {
  /**
   * The failure that founded this row: a chat was archived under one account, the app switched to
   * another, and the sidebar, the Sessions pane and the palette all stopped listing it, because
   * every one of them reads the records of the pair signed in and nothing else. The row is where a
   * reader learns that chats they cannot see exist at all.
   */
  const SIGNED_IN = { account: 'ba7ccf25-6e12-4287-b331-2d7eff00cdf8', org: '2108d9a2-a997-4f8e' }

  function pairRow(over: Partial<MergeSurvey['pairs'][number]> = {}) {
    return {
      pair: 'ba7ccf25\\2108d9a2', path: 'C:\\r\\ba7ccf25\\2108d9a2', link: false,
      records: 212, archived: 111, deleted_markers: 40, missing: 13, ...over,
    }
  }

  function survey(over: Partial<MergeSurvey> = {}): MergeSurvey {
    return { pairs: [pairRow()], ids: 223, to_copy: 112, divergent: [], ...over }
  }

  it('says nothing when this account already holds every chat', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ signed_in: SIGNED_IN }))
    vi.spyOn(api.accounts, 'mergeState').mockResolvedValue(
      survey({ pairs: [pairRow({ missing: 0 })], to_copy: 0 }))
    draw()
    await screen.findByRole('button', { name: 'All' })
    expect(screen.queryByRole('button', { name: 'Merge' })).toBeNull()
  })

  it('counts what the signed-in pair lacks, not the total across every pair', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ signed_in: SIGNED_IN }))
    vi.spyOn(api.accounts, 'mergeState').mockResolvedValue(
      survey({ pairs: [pairRow({ missing: 13 }), pairRow({ pair: 'e3506f07\\c851f706', missing: 99 })] }))
    draw()
    const line = await screen.findByText(/13 chats made under another account/)
    const row = line.closest('p') as HTMLElement
    expect(row.getAttribute('data-missing')).toBe('13')
    expect(row.getAttribute('title')).toContain(MERGE_HOVER)
    expect(row.textContent).toContain("not in this one's list")
  })

  it('reads as one chat in the singular', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ signed_in: SIGNED_IN }))
    vi.spyOn(api.accounts, 'mergeState').mockResolvedValue(
      survey({ pairs: [pairRow({ missing: 1 })] }))
    draw()
    expect((await screen.findByText(/1 chat made under another account/)).textContent).toContain(
      'is not in this one')
  })

  it('asks before it copies anything, and says what the merge does', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ signed_in: SIGNED_IN }))
    vi.spyOn(api.accounts, 'mergeState').mockResolvedValue(survey())
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Merge' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toContain("copies the chat records so every account's list holds")
  })

  it('reports what the merge itself said, including what it held back', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ signed_in: SIGNED_IN }))
    vi.spyOn(api.accounts, 'mergeState').mockResolvedValue(survey())
    const ran = vi.spyOn(api.accounts, 'merge').mockResolvedValue({
      dry_run: false, app_running: false, targets: ['ba7ccf25\\2108d9a2'],
      written: [{ id: 'local_a', pair: 'ba7ccf25\\2108d9a2', bytes: 10, done: true }],
      failed: [], idx_written: [], deferred_archived: [], restart_required: true,
      restart: { ok: true, why: null },
      said: '96 record(s) copied into 9 pair(s), 8 archived chat(s) held back until Claude closes',
    } as never)
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Merge' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }))
    await waitFor(() => expect(ran).toHaveBeenCalledWith(true))
    expect(await screen.findByText(/held back until Claude closes/)).toBeTruthy()
  })

  it('cannot be acted on by a server started without writes', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ signed_in: SIGNED_IN }))
    vi.spyOn(api.accounts, 'mergeState').mockResolvedValue(survey())
    draw({ writesEnabled: false })
    const button = await screen.findByRole('button', { name: 'Merge' })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    expect(button.getAttribute('title')).toBe('This server was started without writes')
  })

  it('says nothing when the survey cannot be read', async () => {
    vi.spyOn(api.accounts, 'state').mockResolvedValue(state({ signed_in: SIGNED_IN }))
    vi.spyOn(api.accounts, 'mergeState').mockRejectedValue(new Error('no server'))
    draw()
    await screen.findByRole('button', { name: 'All' })
    expect(screen.queryByRole('button', { name: 'Merge' })).toBeNull()
  })
})
