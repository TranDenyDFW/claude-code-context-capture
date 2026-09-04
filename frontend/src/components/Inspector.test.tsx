/**
 * The drawer: what it shows, how it closes, and the two states of a fetched text.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Inspector, type InspectorContent } from './Inspector'

// Reset between cases: a module-level spy that counts across tests couples each assertion to the
// order the file happens to run in, which is how a green suite hides a broken button.
beforeEach(() => vi.clearAllMocks())

const content: InspectorContent = {
  title: 'resident at turn 158',
  source: 'Context window over the session',
  fields: [['series', 'resident'], ['x', '158'], ['y', '568,477']],
  rows: {
    name: 'Messages',
    table: { id: 'tbl-messages', columns: ['ts', 'role'], rows: [{ ts: '10:00', role: 'user' }] },
  },
  onOpen: vi.fn(),
}

describe('the inspector drawer', () => {
  it('is a dialog named by its title, listing every field and the rows behind it', () => {
    render(<Inspector content={content} onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'resident at turn 158' })
    expect(dialog.textContent).toContain('568,477')
    expect(dialog.textContent).toContain('Context window over the session')
    expect(screen.getByRole('table')).toBeTruthy()
    expect(dialog.textContent).toContain('1 row')
  })

  it('closes on Escape and on its close button', () => {
    const close = vi.fn()
    render(<Inspector content={content} onClose={close} />)
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(close).toHaveBeenCalledTimes(2)
  })

  it('offers to open the rows in a new window only when something can open them', () => {
    render(<Inspector content={content} onClose={() => {}} />)
    fireEvent.click(screen.getByText('Open in new window'))
    expect(content.onOpen).toHaveBeenCalledTimes(1)
    render(<Inspector content={{ ...content, onOpen: null }} onClose={() => {}} />)
    expect(screen.getAllByText('Open in new window')).toHaveLength(1)
  })

  it('says the text is being fetched, then shows it, and names a problem beside it', () => {
    const { rerender } = render(<Inspector content={{ ...content, rows: null, text: null }} onClose={() => {}} />)
    expect(screen.getByText('fetching the full text')).toBeTruthy()
    rerender(<Inspector content={{ ...content, rows: null, text: 'the whole message' }} onClose={() => {}} />)
    expect(screen.getByText('the whole message')).toBeTruthy()
    rerender(<Inspector content={{ ...content, rows: null, text: 'the preview', textProblem: 'could not be fetched' }} onClose={() => {}} />)
    expect(screen.getByRole('alert').textContent).toContain('could not be fetched')
  })
})

describe('the drawer is non-modal, and says so', () => {
  it('never claims the page behind it is inert, and never swallows Tab', () => {
    render(<><button>behind</button><Inspector content={content} onClose={() => {}} /></>)
    const dialog = screen.getByRole('dialog')
    // aria-modal was added by "fixing" a finding the review had rejected, with the reasoning that
    // it would be an accessibility regression: there is no backdrop and nothing behind is inert,
    // so it tells assistive tech a falsehood, and a Tab trap strands the reader away from the
    // chart the drawer exists to keep in view.
    expect(dialog.getAttribute('aria-modal')).toBeNull()
    expect(document.querySelector('[role="presentation"]')).toBeNull()
    const close = screen.getByRole('button', { name: 'Close' })
    close.focus()
    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    window.dispatchEvent(tab)
    expect(tab.defaultPrevented).toBe(false)
    expect(document.activeElement).toBe(close)
    expect(screen.getByText('behind')).toBeTruthy()
  })

  it('moves focus when the subject changes, and not when the same subject gains its text', () => {
    const a: InspectorContent = { ...content, subject: 1, title: 'Messages: row', text: null }
    const { rerender } = render(<Inspector content={a} onClose={() => {}} />)
    const close = screen.getByRole('button', { name: 'Close' })
    close.focus()
    rerender(<Inspector content={{ ...a, text: 'the whole message' }} onClose={() => {}} />)
    expect(document.activeElement).toBe(close)
    // A different row carries the same title, which is why the subject is a number and not a name.
    rerender(<Inspector content={{ ...a, subject: 2, fields: [['ts', '11:00']] }} onClose={() => {}} />)
    expect(document.activeElement).toBe(screen.getByRole('dialog'))
  })
})
