import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
import { UpdateData } from './UpdateData'
import { api, ApiError } from '@/api'
import type { HarvestOutcome, HarvestStatus } from '@/api'

/**
 * The header's Update data button.
 *
 * What matters: one click starts one update and opens nothing; it cannot be clicked twice; the
 * page reloads its data exactly once when a run ends, a failed run included; a run the server
 * does not name back is never called a success; a 409 for a job already running is followed, not
 * shown as an error; and a server that will not harvest says why on hover and posts nothing.
 *
 * REAL TIMERS WITH TINY INTERVALS, the way ServerControls' tests poll: `findBy` hangs under fake
 * timers (Transient.test.tsx says why). PLAIN ASSERTIONS, no jest-dom.
 */

function status(over: Partial<HarvestStatus> = {}): HarvestStatus {
  return {
    enabled: true, reason: null, why_not: null, fix: null, running: false, job: null, last: null,
    last_harvest: { ts: new Date(Date.now() - 180_000).toISOString(), mode: 'incremental', files_seen: 9599, files_read: 2, ms: 1412 },
    ...over,
  }
}

function outcome(over: Partial<HarvestOutcome> = {}): HarvestOutcome {
  return {
    id: 'b-1', kind: 'incremental', dry_run: false, ok: true, partial: false,
    sentence: 'Read 3 of 9,599 transcripts.', short: 'Updated: 3 transcripts read.', seconds: 1.2,
    error: null, started_at: '2026-09-20T17:59:58.000Z', finished_at: '2026-09-20T17:59:59.200Z',
    ...over,
  }
}

const running = (id = 'b-1') => status({
  running: true, job: { id, kind: 'incremental', dry_run: false, started_at: '', elapsed_s: 2 },
})

function draw(props: { onChanged?: () => void; noteMs?: number } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={client}>
      <UpdateData onChanged={props.onChanged} pollMs={5} idleMs={60_000} noteMs={props.noteMs ?? 60_000} />
    </QueryClientProvider>,
  )
}

const button = () => screen.findByRole('button', { name: /Update data|Updating/ })
const note = () => screen.getByTestId('update-note').textContent

beforeEach(() => {
  vi.restoreAllMocks()
  focusManager.setFocused(undefined)
})

describe('UpdateData', () => {
  it('shows nothing until the server has said whether it can update, and nothing when it cannot say', async () => {
    vi.spyOn(api.harvest, 'state').mockRejectedValue(new ApiError('500 from /api/store/harvest', 500))
    const { container } = draw()
    await waitFor(() => expect(api.harvest.state).toHaveBeenCalled())
    expect(container.querySelector('button')).toBeNull()
  })

  it('runs on one click without asking: no dialog opens and one incremental update is posted', async () => {
    vi.spyOn(api.harvest, 'state')
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({ last: outcome() }))
    const run = vi.spyOn(api.harvest, 'run').mockResolvedValue({ ...status(), accepted: true, id: 'b-1' })
    draw()
    fireEvent.click(await button())
    await waitFor(() => expect(run).toHaveBeenCalledTimes(1))
    expect(run).toHaveBeenCalledWith('incremental', false)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('is busy and unclickable while a run is under way, and a second click posts nothing', async () => {
    vi.spyOn(api.harvest, 'state').mockResolvedValueOnce(status()).mockResolvedValue(running())
    const run = vi.spyOn(api.harvest, 'run').mockResolvedValue({ ...running(), accepted: true, id: 'b-1' })
    draw()
    fireEvent.click(await button())
    const busy = await screen.findByRole('button', { name: 'Updating…' })
    expect(busy.getAttribute('aria-disabled')).toBe('true')
    expect(busy.getAttribute('aria-busy')).toBe('true')
    expect((busy as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(busy)
    fireEvent.click(busy)
    expect(run).toHaveBeenCalledTimes(1)
  })

  it('calls onChanged once when the run finishes, and says what the server said', async () => {
    const state = vi.spyOn(api.harvest, 'state')
      .mockResolvedValueOnce(status())
      .mockResolvedValueOnce(running())
      .mockResolvedValue(status({ last: outcome() }))
    vi.spyOn(api.harvest, 'run').mockResolvedValue({ ...running(), accepted: true, id: 'b-1' })
    const onChanged = vi.fn()
    draw({ onChanged })
    fireEvent.click(await button())
    await waitFor(() => expect(note()).toBe('Updated: 3 transcripts read.'))
    expect(onChanged).toHaveBeenCalledTimes(1)
    expect((await button()).getAttribute('aria-disabled')).toBe('false')
    expect(state.mock.calls.length).toBeGreaterThanOrEqual(3)
  })

  it('a run that finished before the first poll is still a finished run', async () => {
    vi.spyOn(api.harvest, 'state')
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({ last: outcome() }))
    vi.spyOn(api.harvest, 'run').mockResolvedValue({ ...status(), accepted: true, id: 'b-1' })
    const onChanged = vi.fn()
    draw({ onChanged })
    fireEvent.click(await button())
    await waitFor(() => expect(note()).toBe('Updated: 3 transcripts read.'))
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('comes up already running when the server says a job is under way, and reloads when it ends', async () => {
    vi.spyOn(api.harvest, 'state')
      .mockResolvedValueOnce(running('b-4'))
      .mockResolvedValueOnce(running('b-4'))
      .mockResolvedValue(status({ last: outcome({ id: 'b-4' }) }))
    const run = vi.spyOn(api.harvest, 'run')
    const onChanged = vi.fn()
    draw({ onChanged })
    expect((await screen.findByRole('button', { name: 'Updating…' })).getAttribute('aria-busy')).toBe('true')
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(run).not.toHaveBeenCalled()
    expect(note()).toBe('Updated: 3 transcripts read.')
  })

  it('adopts a run that is already under way instead of calling a 409 an error', async () => {
    // HELD RUNNING until the test lets go, because the point is what the page says WHILE it
    // waits. The job is followed either way (any running job is), so the final state alone
    // cannot tell "adopted" from "shown as a failure and then overwritten".
    let calls = 0
    let finished = false
    vi.spyOn(api.harvest, 'state').mockImplementation(async () => {
      calls += 1
      if (calls === 1) return status()
      return finished ? status({ last: outcome({ id: 'b-9' }) }) : running('b-9')
    })
    vi.spyOn(api.harvest, 'run').mockRejectedValue(
      new ApiError('409 from /api/store/harvest', 409, { error: 'An update is already running.', reason: 'busy' }))
    const onChanged = vi.fn()
    draw({ onChanged })
    fireEvent.click(await button())
    await waitFor(() => expect(note()).toBe('An update was already running; waiting for it.'))
    expect((await button()).getAttribute('aria-busy')).toBe('true')
    expect(screen.getByTestId('update-note').className).not.toContain('text-bad')
    expect(onChanged).not.toHaveBeenCalled()
    finished = true
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(note()).toBe('Updated: 3 transcripts read.')
  })

  it('a refusal with nothing running is shown as the server worded it, and reloads nothing', async () => {
    vi.spyOn(api.harvest, 'state').mockResolvedValue(status())
    vi.spyOn(api.harvest, 'run').mockRejectedValue(
      new ApiError('403 from /api/store/harvest', 403, { error: 'This server is serving a copy.', reason: 'not-own-store' }))
    const onChanged = vi.fn()
    draw({ onChanged })
    fireEvent.click(await button())
    await waitFor(() => expect(note()).toBe('Update failed: This server is serving a copy.'))
    expect(onChanged).not.toHaveBeenCalled()
    expect((await button()).getAttribute('aria-disabled')).toBe('false')
  })

  it('a run that fails says so in words, keeps saying it, and still reloads what it may have written', async () => {
    vi.spyOn(api.harvest, 'state')
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({ last: outcome({ ok: false, short: 'Update failed: the harvester exited 1.', error: 'x' }) }))
    vi.spyOn(api.harvest, 'run').mockResolvedValue({ ...status(), accepted: true, id: 'b-1' })
    const onChanged = vi.fn()
    draw({ onChanged, noteMs: 10 })
    fireEvent.click(await button())
    await waitFor(() => expect(note()).toBe('Update failed: the harvester exited 1.'))
    await new Promise((resolve) => setTimeout(resolve, 60))
    expect(note()).toBe('Update failed: the harvester exited 1.')
    expect(screen.getByTestId('update-note').className).toContain('text-bad')
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('a run the server does not name back is interrupted, never a success', async () => {
    vi.spyOn(api.harvest, 'state')
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({ last: null }))
    vi.spyOn(api.harvest, 'run').mockResolvedValue({ ...status(), accepted: true, id: 'b-5' })
    draw()
    fireEvent.click(await button())
    await waitFor(() => expect(note()).toContain('did not report back'))
    expect(note()).not.toContain('Updated')
  })

  it('a finished job with another id is not the run this page started, so it is not called a success', async () => {
    // A server restarted mid-run can come back naming an OLDER job as its last one. "Not running
    // any more, and there is a last job that went fine" is exactly what a success looks like
    // without the id.
    vi.spyOn(api.harvest, 'state')
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({ last: outcome({ id: 'b-3' }) }))
    vi.spyOn(api.harvest, 'run').mockResolvedValue({ ...status(), accepted: true, id: 'b-5' })
    draw()
    fireEvent.click(await button())
    await waitFor(() => expect(note()).toContain('did not report back'))
    expect(note()).not.toContain('Updated')
    expect(screen.getByTestId('update-note').className).toContain('text-warn')
  })

  it('stops saying a success after a moment, so the note does not read as a state', async () => {
    vi.spyOn(api.harvest, 'state')
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({ last: outcome() }))
    vi.spyOn(api.harvest, 'run').mockResolvedValue({ ...status(), accepted: true, id: 'b-1' })
    draw({ noteMs: 15 })
    fireEvent.click(await button())
    await waitFor(() => expect(note()).toBe('Updated: 3 transcripts read.'))
    await waitFor(() => expect(note()).toBe(''))
    expect(screen.getByTestId('update-note').className).toBe('sr-only')
  })

  it('is off, with the server’s reason on hover, when the server will not harvest, and a click posts nothing', async () => {
    vi.spyOn(api.harvest, 'state').mockResolvedValue(status({
      enabled: false, reason: 'not-own-store', why_not: 'This server is serving a copy.', fix: 'Start it without --db.',
    }))
    const run = vi.spyOn(api.harvest, 'run')
    draw()
    const off = await button()
    expect(off.getAttribute('aria-disabled')).toBe('true')
    expect(off.getAttribute('title')).toContain('Update data is off on this server: This server is serving a copy.')
    expect(off.getAttribute('title')).toContain('Start it without --db.')
    fireEvent.click(off)
    expect(run).not.toHaveBeenCalled()
  })

  it('says on hover how fresh the store is and that it does not ask, and nothing about it in the row', async () => {
    vi.spyOn(api.harvest, 'state').mockResolvedValue(status())
    draw()
    const idle = await button()
    expect(idle.getAttribute('title')).toContain('Store last updated 3 min ago: 2 transcripts read in 1.4 s.')
    expect(idle.getAttribute('title')).toContain('does not ask first')
    expect(note()).toBe('')
    expect(screen.queryByText(/last updated/i)).toBeNull()
  })

  it('says the outcome is not known when the server stops answering mid-run, and does not stay busy', async () => {
    vi.spyOn(api.harvest, 'state')
      .mockResolvedValueOnce(status())
      .mockResolvedValueOnce(running())
      .mockRejectedValue(new ApiError('Failed to fetch', 0))
    vi.spyOn(api.harvest, 'run').mockResolvedValue({ ...running(), accepted: true, id: 'b-1' })
    draw()
    fireEvent.click(await button())
    await waitFor(() => expect(note()).toBe("C4X stopped answering; the update's outcome is not known yet."))
    expect((await button()).getAttribute('aria-busy')).toBe('false')
  })

  it('one failed poll does not lose the run: it is picked up again and reported, and the page reloads', async () => {
    // The common case behind "stopped answering" is a laptop lid or a network change while the
    // server and the harvester carry on. Dropping the run there left a red note for ever and
    // never reloaded data the run did write.
    let calls = 0
    vi.spyOn(api.harvest, 'state').mockImplementation(async () => {
      calls += 1
      if (calls === 1) return status()
      if (calls === 2) return running()
      if (calls === 3) throw new ApiError('Failed to fetch', 0)
      return status({ last: outcome() })
    })
    vi.spyOn(api.harvest, 'run').mockResolvedValue({ ...running(), accepted: true, id: 'b-1' })
    const onChanged = vi.fn()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    render(
      <QueryClientProvider client={client}>
        <UpdateData onChanged={onChanged} pollMs={5} idleMs={60_000} noteMs={60_000} />
      </QueryClientProvider>,
    )
    fireEvent.click(await button())
    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(3))
    // Poked rather than waited for: while lost the hook asks again every two seconds at least.
    await waitFor(async () => {
      await client.refetchQueries({ queryKey: ['harvest'] })
      expect(note()).toBe('Updated: 3 transcripts read.')
    })
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('a 409 whose job ended before the page could look is a finished run, not a failed click', async () => {
    vi.spyOn(api.harvest, 'state')
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({ last: outcome({ id: 'b-9' }) }))
    vi.spyOn(api.harvest, 'run').mockRejectedValue(new ApiError('409 from /api/store/harvest', 409, {
      error: 'An update is already running.', reason: 'busy', job: { id: 'b-9' },
    }))
    const onChanged = vi.fn()
    draw({ onChanged })
    fireEvent.click(await button())
    await waitFor(() => expect(note()).toBe('Updated: 3 transcripts read.'))
    expect(note()).not.toContain('Update failed')
    expect(onChanged).toHaveBeenCalledTimes(1)
    expect((await button()).getAttribute('aria-busy')).toBe('false')
  })

  it('a run it found under way is held to its id too: a later run of a restarted server is not its result', async () => {
    // Reloaded mid-run, following job b-7; the server is restarted (which kills b-7) and some
    // other tab runs c-1 on the new server. "Not running, and the last job went fine" is true,
    // and it is not this run.
    let finished = false
    vi.spyOn(api.harvest, 'state').mockImplementation(async () =>
      finished ? status({ last: outcome({ id: 'c-1' }) }) : running('b-7'))
    draw()
    await screen.findByRole('button', { name: 'Updating…' })
    finished = true
    await waitFor(() => expect(note()).toContain('did not report back'))
    expect(note()).not.toContain('Updated')
  })

  it('an idle page shown again asks at once, so the freshness line is not thirty seconds stale', async () => {
    const state = vi.spyOn(api.harvest, 'state').mockResolvedValue(status())
    draw()
    await button()
    const before = state.mock.calls.length
    focusManager.setFocused(false)
    focusManager.setFocused(true)
    await waitFor(() => expect(state.mock.calls.length).toBeGreaterThan(before))
  })

  it('catches up when the page is shown again: a hidden tab does not poll, and the return refetches', async () => {
    let calls = 0
    let finished = false
    vi.spyOn(api.harvest, 'state').mockImplementation(async () => {
      calls += 1
      if (calls === 1) return status()
      return finished ? status({ last: outcome() }) : running()
    })
    vi.spyOn(api.harvest, 'run').mockResolvedValue({ ...running(), accepted: true, id: 'b-1' })
    const onChanged = vi.fn()
    draw({ onChanged })
    fireEvent.click(await button())
    await screen.findByRole('button', { name: 'Updating…' })
    focusManager.setFocused(false)
    finished = true
    const whileHidden = calls
    await new Promise((resolve) => setTimeout(resolve, 80))
    expect(calls).toBe(whileHidden)
    expect(onChanged).not.toHaveBeenCalled()
    focusManager.setFocused(true)
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(note()).toBe('Updated: 3 transcripts read.')
  })

  it('is a group of its own named Store data, with no pressed state, so it cannot be read as the Live toggle', async () => {
    vi.spyOn(api.harvest, 'state').mockResolvedValue(status())
    draw()
    const idle = await button()
    const group = screen.getByRole('group', { name: 'Store data' })
    expect(group.contains(idle)).toBe(true)
    expect(group.className).toContain('border-l')
    expect(idle.hasAttribute('aria-pressed')).toBe(false)
    expect(idle.className).not.toContain('bg-good')
    expect(idle.getAttribute('aria-label')).toBeNull()
  })
})
