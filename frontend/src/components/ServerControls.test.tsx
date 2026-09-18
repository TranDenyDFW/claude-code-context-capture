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
    expect(screen.getByRole('dialog', { name: 'Stop C4X' }).textContent)
      .toContain('stops the server that serves this page')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('button', { name: 'Stop C4X' })).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Stop C4X' }))
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/C4X stopped\. The next Claude session starts it again/)).not.toBeNull()
    expect(screen.queryByRole('button', { name: /Stop/ })).toBeNull()
  })

  it('reads as one control with two sides, and says the verb once', () => {
    render(<ServerControls />)
    const stop = screen.getByRole('button', { name: 'Stop C4X' })
    const restart = screen.getByRole('button', { name: 'Restart C4X' })
    // The group is already named "C4X server", so the buttons say the verb alone.
    expect(stop.textContent).toBe('Stop')
    expect(restart.textContent).toBe('Restart')
    // One bordered box, the shape the Account switch uses, not two buttons with a gap.
    expect(stop.parentElement).toBe(restart.parentElement)
    expect(stop.parentElement!.className).toContain('inline-flex')
    expect(stop.parentElement!.className).toContain('rounded-md')
    // Equal width by rule; jsdom lays nothing out, so the rule is what can be asserted.
    for (const side of [stop, restart]) expect(side.className).toContain('basis-0')
  })

  it('keeps a name the dialog cannot collide with', () => {
    // WHY THE ARIA-LABEL IS NOT DECORATION. The dialog's own action button reads "Stop", and
    // nothing marks the page behind a dialog inert. Were the header button named "Stop" too,
    // two buttons would answer to one name and every query here would break.
    render(<ServerControls />)
    fireEvent.click(screen.getByRole('button', { name: 'Stop C4X' }))
    expect(screen.getByRole('button', { name: 'Stop' }).closest('[role="dialog"]')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Stop C4X' }).closest('[role="dialog"]')).toBeNull()
  })

  it('asks before restarting too, and restarts only on Restart', async () => {
    const restart = vi.spyOn(api.server, 'restart')
    render(<ServerControls />)
    fireEvent.click(screen.getByRole('button', { name: 'Restart C4X' }))
    expect(screen.getByRole('dialog', { name: 'Restart C4X' }).textContent)
      .toContain('Claude is not touched')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(restart).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
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
    fireEvent.click(screen.getByRole('button', { name: 'Restart' }))
    expect(await screen.findByRole('button', { name: 'Restart…' })).not.toBeNull()
    await waitFor(() => expect(restart).toHaveBeenCalledTimes(1))
    expect((await screen.findAllByText('C4X is back.')).length).toBeGreaterThan(0)
    expect(onRestarted).toHaveBeenCalledTimes(1)
    expect(api.health).toHaveBeenCalledTimes(4)
  })

  it('a reply cut by the exit is the restart working, not a failure', async () => {
    vi.spyOn(api, 'health').mockResolvedValueOnce(health(11)).mockResolvedValue(health(22))
    vi.spyOn(api.server, 'restart').mockRejectedValue(new TypeError('Failed to fetch'))
    const onRestarted = vi.fn()
    render(<ServerControls onRestarted={onRestarted} pollMs={5} waitMs={2_000} />)
    fireEvent.click(screen.getByRole('button', { name: 'Restart C4X' }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart' }))
    expect((await screen.findAllByText('C4X is back.')).length).toBeGreaterThan(0)
    expect(onRestarted).toHaveBeenCalledTimes(1)
  })

  it('a refusal with a status is shown and nothing is waited for', async () => {
    vi.spyOn(api, 'health').mockResolvedValue(health(11))
    vi.spyOn(api.server, 'restart')
      .mockRejectedValue(new ApiError('403 from /api/server/restart', 403, { error: 'a cross-origin request cannot change this store' }))
    const onRestarted = vi.fn()
    render(<ServerControls onRestarted={onRestarted} pollMs={5} waitMs={2_000} />)
    fireEvent.click(screen.getByRole('button', { name: 'Restart C4X' }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart' }))
    expect((await screen.findAllByText('a cross-origin request cannot change this store')).length)
      .toBeGreaterThan(0)
    expect(onRestarted).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Restart C4X' })).not.toBeNull()
  })

  it('gives up after the wait and says the next session starts one', async () => {
    vi.spyOn(api, 'health').mockResolvedValue(health(11))
    vi.spyOn(api.server, 'restart').mockResolvedValue({ restarting: true, pid: 22, argv: [] })
    const onRestarted = vi.fn()
    render(<ServerControls onRestarted={onRestarted} pollMs={5} waitMs={40} />)
    fireEvent.click(screen.getByRole('button', { name: 'Restart C4X' }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart' }))
    expect((await screen.findAllByText(/did not come back in time/)).length).toBeGreaterThan(0)
    expect(onRestarted).not.toHaveBeenCalled()
  })
})

describe('its place in the header', () => {
  it('is set apart from the controls before it, in every state', async () => {
    vi.spyOn(api.server, 'stop').mockResolvedValue({ stopped: true })
    render(<ServerControls />)
    const group = () => screen.getByRole('group', { name: 'C4X server' })
    expect(group().className).toContain('border-l')
    fireEvent.click(screen.getByRole('button', { name: 'Stop C4X' }))
    expect(group().className).toContain('border-l')
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await screen.findByText(/C4X stopped/)
    expect(group().className).toContain('border-l')
  })
})
