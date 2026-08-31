/**
 * The command palette, which is the ONLY way to reach a session by hand now that the dropdown is
 * gone. If its search is wrong, a reader cannot get to a session at all.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Palette, matches, type Choice } from './Palette'

const tabs = [{ id: 'tab-summary', label: 'Summary' }, { id: 'tab-cost', label: 'Cost' }]
const cohorts = [
  { value: '__all__', label: 'All sessions (317)' },
  { value: 'project::F:\\SecDb', label: 'Project: F:\\SecDb (12)' },
]
const sessions = [
  { value: 's1', label: 'F:\\SecDb  ·  Economic policy  ·  2026-08-31 01:47' },
  { value: 's2', label: 'P:\\Books  ·  Reading notes  ·  2026-01-04 09:02' },
]

function open(onPick = vi.fn()) {
  render(
    <Palette open onClose={vi.fn()} tabs={tabs} cohorts={cohorts} sessions={sessions}
             onPick={onPick} />,
  )
  return { field: screen.getByRole('textbox'), onPick }
}

describe('matching', () => {
  it('needs every term but not in order, so "secdb 08-31" works', () => {
    expect(matches('F:\\SecDb · Economic policy · 2026-08-31 01:47', 'secdb 08-31')).toBe(true)
    expect(matches('F:\\SecDb · Economic policy · 2026-08-31 01:47', '08-31 secdb')).toBe(true)
  })

  it('rejects when one term is absent', () => {
    expect(matches('F:\\SecDb · Economic policy', 'secdb missing')).toBe(false)
  })
})

describe('the palette', () => {
  it('offers tabs, populations and sessions in one list', () => {
    open()
    expect(screen.getByText('Summary')).toBeTruthy()
    expect(screen.getByText('All sessions (317)')).toBeTruthy()
    expect(screen.getAllByText(/session/i).length).toBeGreaterThan(0)
  })

  it('LEADS A SESSION WITH ITS DATE, because that is how a session is found', () => {
    // The app labels a session "project · title · when", which is right for a list grouped by
    // project and wrong for a search by time. Nobody remembers a session by name.
    open()
    expect(screen.getByText(/^2026-08-31 01:47/)).toBeTruthy()
  })

  it('finds a session by typing part of its date', () => {
    open()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '2026-08-31' } })
    expect(screen.getByText(/2026-08-31 01:47/)).toBeTruthy()
    expect(screen.queryByText(/2026-01-04/)).toBeNull()
  })

  it('returns the session id, not the label, so the caller can select it', () => {
    const { field, onPick } = open()
    fireEvent.change(field, { target: { value: '2026-08-31' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(onPick).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'session', id: 's1' }) as Choice,
    )
  })

  it('moves with the arrow keys and commits the highlighted row', () => {
    const { field, onPick } = open()
    fireEvent.change(field, { target: { value: 'tab' } })
    fireEvent.keyDown(field, { key: 'ArrowDown' })
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ kind: 'tab', id: 'tab-cost' }))
  })

  it('says so when nothing matches instead of showing an empty box', () => {
    const { field } = open()
    fireEvent.change(field, { target: { value: 'zzzz' } })
    expect(screen.getByText(/Nothing matches/)).toBeTruthy()
  })

  it('is a labelled modal dialog, so a screen reader announces it', () => {
    open()
    const dialog = screen.getByRole('dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.getAttribute('aria-label')).toBeTruthy()
  })

  it('renders nothing at all when closed', () => {
    const { container } = render(
      <Palette open={false} onClose={vi.fn()} tabs={tabs} cohorts={cohorts} sessions={sessions}
               onPick={vi.fn()} />,
    )
    expect(container.firstChild).toBeNull()
  })
})
