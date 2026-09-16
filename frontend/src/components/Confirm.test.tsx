import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Confirm } from './Confirm'
import { ApiError } from '@/api'

/**
 * The dialog every restart goes through. What matters: Cancel calls nothing, Continue calls the
 * action once, the progress line follows what it is told, the outcome stays until Close, and a
 * throw is shown rather than swallowed.
 *
 * PLAIN ASSERTIONS, no `jest-dom`, as in the sibling suites.
 */
const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

describe('Confirm', () => {
  it('calls nothing on Cancel, and hands back null', () => {
    const action = vi.fn()
    const onClose = vi.fn()
    render(<Confirm label="Switch" question="Do it? Continue?" action={action} onClose={onClose} />)
    expect(screen.getByRole('dialog', { name: 'Switch' }).getAttribute('aria-modal')).toBe('true')
    expect(screen.getByText('Do it? Continue?')).not.toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(action).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledWith(null)
  })

  it('runs the action once on Continue, shows the outcome, and hands it back on Close', async () => {
    let settle: (v: { ok: boolean; text: string }) => void = () => {}
    const action = vi.fn(() => new Promise<{ ok: boolean; text: string }>((resolve) => { settle = resolve }))
    const progress = vi.fn(async () => 'Claude is closed')
    const onClose = vi.fn()
    render(<Confirm label="Switch" question="Do it? Continue?" action={action} progress={progress}
                    onClose={onClose} />)
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(action).toHaveBeenCalledTimes(1)
    // While it runs: the button is busy, the backdrop does nothing, the progress line follows.
    expect((screen.getByRole('button', { name: 'Continue…' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.mouseDown(screen.getByRole('dialog').parentElement as HTMLElement)
    expect(onClose).not.toHaveBeenCalled()
    expect((await screen.findByTestId('confirm-progress')).textContent).toBe('Claude is closed')
    settle({ ok: true, text: 'The directories are linked. Claude was closed and started again.' })
    expect((await screen.findByTestId('confirm-outcome')).textContent)
      .toBe('The directories are linked. Claude was closed and started again.')
    expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledWith({ ok: true, text: 'The directories are linked. Claude was closed and started again.' })
    expect(action).toHaveBeenCalledTimes(1)
  })

  it('shows a throw as its sentence, marked as not ok', async () => {
    const action = vi.fn(async () => {
      await wait(1)
      throw new ApiError('409', 409, { error: 'a restart is already under way; wait for it to finish' })
    })
    render(<Confirm label="Switch" question="Q? Continue?" action={action} onClose={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    const outcome = await screen.findByTestId('confirm-outcome')
    expect(outcome.textContent).toBe('a restart is already under way; wait for it to finish')
    expect(outcome.className).toContain('text-warn')
  })

  it('uses the action label it is given, and Escape before Continue is a Cancel', () => {
    const onClose = vi.fn()
    render(<Confirm label="Stop C4X" question="Stop? Continue?" actionLabel="Stop"
                    action={async () => ({ ok: true, text: 'stopped' })} onClose={onClose} />)
    expect(screen.getByRole('button', { name: 'Stop' })).not.toBeNull()
    fireEvent.keyDown(screen.getByRole('button', { name: 'Stop' }), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledWith(null)
  })

  it('sits above the window it was opened from', () => {
    render(<Confirm label="Adopt chats" question="Q? Continue?" action={async () => ({ ok: true, text: 'x' })}
                    onClose={() => {}} />)
    const backdrop = screen.getByRole('dialog', { name: 'Adopt chats' }).parentElement as HTMLElement
    expect(backdrop.className).toContain('z-[60]')
    waitFor(() => expect(backdrop.parentElement).toBe(document.body))
  })
})
