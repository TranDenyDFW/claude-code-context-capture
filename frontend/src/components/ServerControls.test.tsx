import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ServerControls } from './ServerControls'
import { api, ApiError } from '@/api'

/**
 * Stop C4X and Restart C4X. What matters: stop asks first and says what follows; restart is
 * not satisfied by the server it asked, only by a different process answering.
 */
const health = (pid: number) => ({
  ok: true, db: 'd', pid, read_only: true, writes_enabled: true, cache: {},
})

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('ServerControls', () => {
  it('asks before stopping, then stops and says the next session starts it again', async () => {
    const stop = vi.spyOn(api.server, 'stop').mockResolvedValue({ stopped: true })
    render(<ServerControls />)
    fireEvent.click(screen.getByRole('button', { name: 'Stop C4X' }))
    expect(stop).not.toHaveBeenCalled()
    expect(screen.getByText(/Stop C4X\? This page stops answering/)).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'Stop C4X' })).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Stop C4X' }))
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/C4X stopped\. The next Claude session starts it again/)).not.toBeNull()
    expect(screen.queryByRole('button', { name: /Stop/ })).toBeNull()
  })

  it('restarts and waits for a DIFFERENT process to answer health', async () => {
    vi.spyOn(api, 'health')
      .mockResolvedValueOnce(health(11))          // before asking
      .mockResolvedValueOnce(health(11))          // the old server, still answering
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(health(22))          // the replacement
    const restart = vi.spyOn(api.server, 'restart').mockResolvedValue({ restarting: true, pid: 22, argv: [] })
    const onRestarted = vi.fn()
    render(<ServerControls onRestarted={onRestarted} pollMs={5} waitMs={2_000} />)
    fireEvent.click(screen.getByRole('button', { name: 'Restart C4X' }))
    expect(await screen.findByRole('button', { name: 'Restarting C4X…' })).not.toBeNull()
    await waitFor(() => expect(restart).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('C4X is back.')).not.toBeNull()
    expect(onRestarted).toHaveBeenCalledTimes(1)
    expect(api.health).toHaveBeenCalledTimes(4)
  })

  it('a reply cut by the exit is the restart working, not a failure', async () => {
    vi.spyOn(api, 'health').mockResolvedValueOnce(health(11)).mockResolvedValue(health(22))
    vi.spyOn(api.server, 'restart').mockRejectedValue(new TypeError('Failed to fetch'))
    const onRestarted = vi.fn()
    render(<ServerControls onRestarted={onRestarted} pollMs={5} waitMs={2_000} />)
    fireEvent.click(screen.getByRole('button', { name: 'Restart C4X' }))
    expect(await screen.findByText('C4X is back.')).not.toBeNull()
    expect(onRestarted).toHaveBeenCalledTimes(1)
  })

  it('a refusal with a status is shown and nothing is waited for', async () => {
    vi.spyOn(api, 'health').mockResolvedValue(health(11))
    vi.spyOn(api.server, 'restart')
      .mockRejectedValue(new ApiError('403 from /api/server/restart', 403, { error: 'a cross-origin request cannot change this store' }))
    const onRestarted = vi.fn()
    render(<ServerControls onRestarted={onRestarted} pollMs={5} waitMs={2_000} />)
    fireEvent.click(screen.getByRole('button', { name: 'Restart C4X' }))
    expect(await screen.findByText('a cross-origin request cannot change this store')).not.toBeNull()
    expect(onRestarted).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Restart C4X' })).not.toBeNull()
  })

  it('gives up after the wait and says the next session starts one', async () => {
    vi.spyOn(api, 'health').mockResolvedValue(health(11))
    vi.spyOn(api.server, 'restart').mockResolvedValue({ restarting: true, pid: 22, argv: [] })
    const onRestarted = vi.fn()
    render(<ServerControls onRestarted={onRestarted} pollMs={5} waitMs={40} />)
    fireEvent.click(screen.getByRole('button', { name: 'Restart C4X' }))
    expect(await screen.findByText(/did not come back in time/)).not.toBeNull()
    expect(onRestarted).not.toHaveBeenCalled()
  })
})
