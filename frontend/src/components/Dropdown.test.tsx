import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Dropdown } from './Dropdown'

/**
 * The select that can say more on hover than its label does.
 *
 * What matters: every row carries its full path as the hover (the thing a native select could
 * not do), the trigger says which row is chosen, the keyboard reaches every row without moving
 * focus, and Escape closes this list without reaching the window.
 *
 * PLAIN ASSERTIONS, no `jest-dom`, as in the sibling suites.
 */
const OPTIONS = [
  { value: '', label: 'No restriction', title: 'Every chat the store lists' },
  { value: 'project::P:\\ClaudeExt\\ccxe\\c4x', label: 'c4x (150 listed)', title: 'P:\\ClaudeExt\\ccxe\\c4x' },
  { value: 'project::L:\\Books', label: 'L:\\Books (4 listed)', title: 'L:\\Books' },
]

function draw(value = '', onChange = vi.fn()) {
  render(<Dropdown label="Population" value={value} options={OPTIONS} onChange={onChange} />)
  return { onChange, trigger: screen.getByRole('combobox', { name: 'Population' }) }
}

describe('Dropdown', () => {
  it('is a closed combobox showing the chosen label, with no list in the document', () => {
    const { trigger } = draw('project::L:\\Books')
    expect(trigger.textContent).toContain('L:\\Books (4 listed)')
    expect(trigger.getAttribute('aria-haspopup')).toBe('listbox')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('names the chosen option\'s full path on the trigger', () => {
    const { trigger } = draw('project::P:\\ClaudeExt\\ccxe\\c4x')
    expect(trigger.getAttribute('title')).toBe('P:\\ClaudeExt\\ccxe\\c4x')
  })

  it('opens to rows that each carry their full path as the hover', () => {
    const { trigger } = draw('project::P:\\ClaudeExt\\ccxe\\c4x')
    fireEvent.click(trigger)
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    const rows = screen.getAllByRole('option')
    expect(rows.map((r) => r.getAttribute('title'))).toEqual(
      ['Every chat the store lists', 'P:\\ClaudeExt\\ccxe\\c4x', 'L:\\Books'])
    expect(rows.map((r) => r.getAttribute('aria-selected'))).toEqual(['false', 'true', 'false'])
    expect(screen.getByRole('listbox', { name: 'Population' })).not.toBeNull()
  })

  it('commits a clicked row and closes; clicking the chosen row changes nothing', () => {
    const { trigger, onChange } = draw('')
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('option', { name: /c4x/ }))
    expect(onChange).toHaveBeenCalledWith('project::P:\\ClaudeExt\\ccxe\\c4x')
    expect(screen.queryByRole('listbox')).toBeNull()
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('option', { name: 'No restriction' }))
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('opens on the chosen row, moves without moving focus, and Enter commits', () => {
    const { trigger, onChange } = draw('project::L:\\Books')
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    let rows = screen.getAllByRole('option')
    expect(rows[2].getAttribute('data-active')).toBe('true')
    expect(trigger.getAttribute('aria-activedescendant')).toBe(rows[2].id)
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    expect(screen.getAllByRole('option')[2].getAttribute('data-active')).toBe('true')
    fireEvent.keyDown(trigger, { key: 'Home' })
    rows = screen.getAllByRole('option')
    expect(rows[0].getAttribute('data-active')).toBe('true')
    expect(trigger.getAttribute('aria-activedescendant')).toBe(rows[0].id)
    fireEvent.keyDown(trigger, { key: 'End' })
    expect(screen.getAllByRole('option')[2].getAttribute('data-active')).toBe('true')
    fireEvent.keyDown(trigger, { key: 'ArrowUp' })
    expect(screen.getAllByRole('option')[1].getAttribute('data-active')).toBe('true')
    fireEvent.keyDown(trigger, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('project::P:\\ClaudeExt\\ccxe\\c4x')
    expect(document.activeElement).toBe(trigger)
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('Escape closes the list and goes no further; a click outside closes it too', () => {
    const { trigger, onChange } = draw('')
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    expect(screen.getByRole('listbox')).not.toBeNull()
    // The Adopt drawer listens on the window for Escape; the press that closes this list must
    // not reach it. Listened for only now, after the ArrowDown that opened the list.
    const heard = vi.fn()
    window.addEventListener('keydown', heard)
    fireEvent.keyDown(trigger, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(heard).not.toHaveBeenCalled()
    window.removeEventListener('keydown', heard)
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.click(trigger)
    fireEvent.mouseDown(screen.getByRole('listbox'))
    expect(screen.getByRole('listbox')).not.toBeNull()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('a value the options do not name shows itself and marks no row', () => {
    const { trigger } = draw('project::P:\\Gone')
    expect(trigger.textContent).toContain('project::P:\\Gone')
    expect(trigger.getAttribute('title')).toBeNull()
    fireEvent.click(trigger)
    expect(screen.getAllByRole('option').every((r) => r.getAttribute('aria-selected') === 'false'))
      .toBe(true)
  })
})
