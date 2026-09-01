/**
 * The sidebar is the only navigation now, so a defect here means a tab that cannot be reached.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Sidebar } from './Sidebar'

const tabs = [
  { id: 'tab-summary', label: 'Summary' },
  { id: 'tab-cost', label: 'Cost' },
  { id: 'tab-brand-new', label: 'Brand New' },
]

function show(collapsed = false, onPick = vi.fn(), onToggle = vi.fn()) {
  const view = render(
    <Sidebar tabs={tabs} active="tab-summary" collapsed={collapsed} onPick={onPick}
             onToggle={onToggle} />,
  )
  return { ...view, onPick, onToggle }
}

beforeEach(() => {
  try {
    localStorage.clear()
  } catch { /* storage is not required for the component to work */ }
})

describe('the sidebar', () => {
  it('offers every tab the server returned', () => {
    show()
    for (const tab of tabs) expect(screen.getByText(tab.label)).toBeTruthy()
  })

  it('SHOWS A TAB IT HAS NO ICON FOR, rather than hiding it', () => {
    // The icon map is presentation and is allowed to be incomplete. A tab added to the app must
    // never be invisible because nobody chose a glyph: that is how a hand-written list of pages
    // goes stale without anyone noticing.
    show()
    expect(screen.getByText('Brand New')).toBeTruthy()
  })

  it('marks the active tab for a screen reader, not only with colour', () => {
    show()
    expect(screen.getByText('Summary').closest('button')?.getAttribute('aria-current')).toBe('page')
    expect(screen.getByText('Cost').closest('button')?.getAttribute('aria-current')).toBeNull()
  })

  it('keeps the btn-<id> ids the screenshot tool selects on', () => {
    const { container } = show()
    for (const tab of tabs) expect(container.querySelector(`#btn-${tab.id}`)).toBeTruthy()
  })

  it('reports the picked tab id', () => {
    const { onPick } = show()
    fireEvent.click(screen.getByText('Cost'))
    expect(onPick).toHaveBeenCalledWith('tab-cost')
  })

  it('hides the labels when collapsed but keeps every tab reachable', () => {
    const { container, onPick } = show(true)
    expect(screen.queryByText('Cost')).toBeNull()
    // Still there, still clickable, and the full name is the tooltip.
    const button = container.querySelector('#btn-tab-cost') as HTMLElement
    expect(button.getAttribute('title')).toBe('Cost')
    fireEvent.click(button)
    expect(onPick).toHaveBeenCalledWith('tab-cost')
  })

  it('says which way the toggle goes, in words a screen reader can use', () => {
    const { rerender } = show(false)
    expect(screen.getByLabelText('Collapse the sidebar')).toBeTruthy()
    rerender(
      <Sidebar tabs={tabs} active="tab-summary" collapsed onPick={vi.fn()} onToggle={vi.fn()} />,
    )
    expect(screen.getByLabelText('Expand the sidebar')).toBeTruthy()
  })

  it('is a labelled navigation landmark', () => {
    show()
    expect(screen.getByRole('navigation', { name: 'Tabs' })).toBeTruthy()
  })
})
