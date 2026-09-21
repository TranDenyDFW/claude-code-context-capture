import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
import { StoreMaintenance } from './StoreMaintenance'
import { UpdateData } from './UpdateData'
import { api, ApiError } from '@/api'
import type { HarvestKind, HarvestOutcome, HarvestStatus } from '@/api'

/**
 * The two one-off passes, on the Diagnostics tab.
 *
 * What matters: BOTH ask first and Cancel posts nothing; the fold runs its dry run BEFORE it
 * asks, so the question quotes the harvester's own numbers, including the runs it can place
 * nowhere; a dry run reloads nothing and the real run reloads the page once, through the
 * header's control, which owns the reload; and a server that will not harvest says why in a
 * visible sentence and posts nothing.
 *
 * A LITTLE FAKE SERVER, because these flows post twice (the dry run, then the run) and the
 * status has to answer for whichever job was last. REAL TIMERS, tiny intervals, no jest-dom.
 */

function status(over: Partial<HarvestStatus> = {}): HarvestStatus {
  return {
    enabled: true, reason: null, why_not: null, fix: null, running: false, job: null, last: null,
    last_harvest: null, ...over,
  }
}

const RUNS = { linked: 4, heads: 1, batched: 9, projects: 2, unlinked: 0, misses: 185, calls_without_result_ts: 0 }

function server(over: { runs?: Record<string, unknown>; fail?: HarvestKind } = {}) {
  let last: HarvestOutcome | null = null
  let n = 0
  vi.spyOn(api.harvest, 'state').mockImplementation(async () => status({ last }))
  const run = vi.spyOn(api.harvest, 'run').mockImplementation(async (kind: HarvestKind = 'incremental', dryRun = false) => {
    n += 1
    const failed = over.fail === kind
    last = {
      id: `b-${n}`, kind, dry_run: dryRun, ok: !failed, partial: false,
      sentence: failed ? 'The update failed: the harvester exited 1.'
        : kind === 'runs' ? (dryRun ? 'Would fold 13 runs.' : 'Folded 4 runs under their chats and 9 under their projects.')
          : 'Recorded the outcome of 322 tool calls.',
      short: 'short', seconds: 1, error: failed ? 'exited 1' : null,
      started_at: '', finished_at: '', summary: kind === 'runs' ? { ...RUNS, ...over.runs } : null,
    }
    return { ...status(), accepted: true, id: `b-${n}` }
  })
  return run
}

function draw(withHeader: { onChanged?: () => void } | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={client}>
      {withHeader && <UpdateData onChanged={withHeader.onChanged} pollMs={5} idleMs={60_000} noteMs={60_000} />}
      <StoreMaintenance pollMs={5} />
    </QueryClientProvider>,
  )
}

const dialog = () => screen.findByRole('dialog')
const note = () => screen.getByTestId('maintenance-note').textContent

beforeEach(() => {
  vi.restoreAllMocks()
  focusManager.setFocused(undefined)
})

describe('StoreMaintenance', () => {
  it('shows nothing until the server has said whether it can run these', async () => {
    vi.spyOn(api.harvest, 'state').mockRejectedValue(new ApiError('500', 500))
    const { container } = draw()
    await waitFor(() => expect(api.harvest.state).toHaveBeenCalled())
    expect(container.querySelector('section')).toBeNull()
  })

  it('Record tool outcomes asks first, and Cancel posts nothing', async () => {
    const run = server()
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Record tool outcomes…' }))
    const asked = await dialog()
    expect(asked.textContent).toContain('re-reads every transcript')
    expect(asked.textContent).toContain('nothing is closed or restarted')
    fireEvent.click(within(asked).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(run).not.toHaveBeenCalled()
  })

  it('Record runs the pass once on Record, never as a dry run, and says what came of it', async () => {
    const run = server()
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Record tool outcomes…' }))
    fireEvent.click(within(await dialog()).getByRole('button', { name: 'Record' }))
    expect((await screen.findByTestId('confirm-outcome')).textContent).toBe('Recorded the outcome of 322 tool calls.')
    expect(run).toHaveBeenCalledTimes(1)
    expect(run).toHaveBeenCalledWith('tool-outcomes', false)
    fireEvent.click(within(await dialog()).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(note()).toBe('Recorded the outcome of 322 tool calls.'))
  })

  it('Fold runs its dry run first and puts the numbers in the question, the unplaced ones included', async () => {
    const run = server()
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Fold headless runs…' }))
    const asked = await dialog()
    expect(run).toHaveBeenCalledTimes(1)
    expect(run).toHaveBeenCalledWith('runs', true)
    expect(asked.textContent).toContain('fold 4 runs under the 1 chat that spawned them and 9 under 2 projects')
    expect(asked.textContent).toContain('leave 185 it can place nowhere')
    expect(asked.textContent).toContain('stop being listed as chats of their own')
    fireEvent.click(within(asked).getByRole('button', { name: 'Fold' }))
    expect((await screen.findByTestId('confirm-outcome')).textContent).toContain('Folded 4 runs')
    expect(run).toHaveBeenCalledTimes(2)
    expect(run).toHaveBeenLastCalledWith('runs', false)
  })

  it('Cancel after the dry run folds nothing', async () => {
    const run = server()
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Fold headless runs…' }))
    fireEvent.click(within(await dialog()).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(run).toHaveBeenCalledTimes(1)
    expect(run).not.toHaveBeenCalledWith('runs', false)
  })

  it('says to record tool outcomes first when calls have no result time', async () => {
    server({ runs: { calls_without_result_ts: 41 } })
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Fold headless runs…' }))
    const asked = await dialog()
    expect(asked.textContent).toContain('41 shell calls have no result time yet')
    expect(asked.textContent).toContain('Cancel and run Record tool outcomes first')
  })

  it('a dry run that fails says so beside the buttons and asks nothing', async () => {
    const run = server({ fail: 'runs' })
    draw()
    fireEvent.click(await screen.findByRole('button', { name: 'Fold headless runs…' }))
    await waitFor(() => expect(note()).toBe('Failed: The update failed: the harvester exited 1.'))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(run).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('maintenance-note').className).toContain('text-bad')
  })

  it('a dry run reloads nothing and the fold reloads the page once, through the header', async () => {
    server()
    const onChanged = vi.fn()
    draw({ onChanged })
    fireEvent.click(await screen.findByRole('button', { name: 'Fold headless runs…' }))
    const asked = await dialog()
    expect(onChanged).not.toHaveBeenCalled()
    fireEvent.click(within(asked).getByRole('button', { name: 'Fold' }))
    await screen.findByTestId('confirm-outcome')
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
  })

  it('is off, with the reason in a sentence, on a server that will not harvest, and posts nothing', async () => {
    vi.spyOn(api.harvest, 'state').mockResolvedValue(status({
      enabled: false, reason: 'not-own-store', why_not: 'This server is serving a copy.', fix: 'Start it without --db.',
    }))
    const run = vi.spyOn(api.harvest, 'run')
    draw()
    const record = await screen.findByRole('button', { name: 'Record tool outcomes…' })
    const fold = screen.getByRole('button', { name: 'Fold headless runs…' })
    expect(record.getAttribute('aria-disabled')).toBe('true')
    expect(fold.getAttribute('aria-disabled')).toBe('true')
    expect(screen.getByTestId('maintenance-off').textContent)
      .toBe('Off on this server: This server is serving a copy. Start it without --db.')
    fireEvent.click(record)
    fireEvent.click(fold)
    expect(run).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('keeps names the dialog cannot collide with, in the order the passes must run', async () => {
    server()
    draw()
    const record = await screen.findByRole('button', { name: 'Record tool outcomes…' })
    const fold = screen.getByRole('button', { name: 'Fold headless runs…' })
    expect(record.compareDocumentPosition(fold) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(record)
    const asked = await dialog()
    expect(within(asked).getByRole('button', { name: 'Record' })).not.toBeNull()
    expect(screen.getAllByRole('button', { name: /^Record/ }).map((b) => b.textContent)).toEqual(
      ['Record tool outcomes…', 'Record'])
  })
})
