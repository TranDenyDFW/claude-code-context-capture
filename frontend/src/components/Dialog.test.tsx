import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { useRef, useState } from 'react'
import { Dialog } from './Dialog'

/**
 * The window chrome, on its own. What matters is what the Adopt window and the confirm on top
 * of it rely on: the window sits at the end of the document over a backdrop, focus goes in and
 * comes back, Escape closes only the window it was pressed in, Tab stays inside, and a busy
 * window survives a click on the backdrop.
 *
 * PLAIN ASSERTIONS, no `jest-dom`, as in the sibling suites.
 */
function Host({
  busy = false,
  nested = false,
  onInnerClose = () => {},
}: {
  busy?: boolean
  nested?: boolean
  onInnerClose?: () => void
}) {
  const opener = useRef<HTMLButtonElement>(null)
  const first = useRef<HTMLInputElement>(null)
  const [open, setOpen] = useState(false)
  return (
    <>
      <button ref={opener} type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      {open ? (
        <Dialog
          id="outer"
          label="Outer"
          onClose={() => setOpen(false)}
          opener={opener}
          initialFocus={first}
          busy={busy}
          header={<h2>Outer</h2>}
          footer={
            <button type="button" onClick={() => setOpen(false)}>
              Close outer
            </button>
          }
        >
          <input ref={first} aria-label="First" />
          <button type="button">Middle</button>
          {nested ? (
            <Dialog id="inner" label="Inner" onClose={onInnerClose} z="z-[60]">
              <button type="button">Inner button</button>
            </Dialog>
          ) : null}
        </Dialog>
      ) : null}
    </>
  )
}

describe('Dialog', () => {
  it('sits at the end of the document over a backdrop, focus in on open and back on close', () => {
    const { container } = render(<Host />)
    const opener = screen.getByRole('button', { name: 'Open' })
    fireEvent.click(opener)
    const dialog = screen.getByRole('dialog', { name: 'Outer' })
    expect(container.contains(dialog)).toBe(false)
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    const backdrop = dialog.parentElement as HTMLElement
    expect(backdrop.getAttribute('role')).toBe('presentation')
    expect(backdrop.parentElement).toBe(document.body)
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'First' }))
    fireEvent.click(screen.getByRole('button', { name: 'Close outer' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it('closes on Escape only the window the key was pressed in', () => {
    const onInnerClose = vi.fn()
    render(<Host nested onInnerClose={onInnerClose} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    expect(screen.getAllByRole('dialog')).toHaveLength(2)
    // The inner window is a React child of the outer one: its keys bubble to the outer handler
    // through the React tree, and DOM containment is what keeps the outer window open.
    fireEvent.keyDown(screen.getByRole('button', { name: 'Inner button' }), { key: 'Escape' })
    expect(onInnerClose).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('dialog', { name: 'Outer' })).not.toBeNull()
    fireEvent.keyDown(screen.getByRole('button', { name: 'Middle' }), { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Outer' })).toBeNull()
  })

  it('closes on a click on the backdrop, unless it is busy', () => {
    const { rerender } = render(<Host busy />)
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    const dialog = screen.getByRole('dialog', { name: 'Outer' })
    fireEvent.mouseDown(dialog.parentElement as HTMLElement)
    expect(screen.getByRole('dialog', { name: 'Outer' })).not.toBeNull()
    // A click inside the panel never reaches the backdrop's handler.
    fireEvent.mouseDown(screen.getByRole('button', { name: 'Middle' }))
    expect(screen.getByRole('dialog', { name: 'Outer' })).not.toBeNull()
    rerender(<Host busy={false} />)
    fireEvent.mouseDown(screen.getByRole('dialog', { name: 'Outer' }).parentElement as HTMLElement)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('wraps Tab inside the panel, both ways', () => {
    render(<Host />)
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    const first = screen.getByRole('textbox', { name: 'First' })
    const last = screen.getByRole('button', { name: 'Close outer' })
    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(document.activeElement).toBe(first)
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
    // In the middle the browser does its own thing; the handler leaves it alone.
    const middle = screen.getByRole('button', { name: 'Middle' })
    middle.focus()
    fireEvent.keyDown(middle, { key: 'Tab' })
    expect(document.activeElement).toBe(middle)
  })
})
