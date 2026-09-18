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

describe('the sidebar can be made wider', () => {
  it('starts at the width it has always had, and drags from there', () => {
    const { container } = show()
    const nav = container.querySelector('nav')!
    expect(nav.style.width).toBe('208px')
    const bar = screen.getByRole('separator', { name: 'Resize the sidebar' })
    fireEvent.pointerDown(bar, { clientX: 208, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 268 })
    fireEvent.pointerUp(window, { clientX: 268 })
    expect(nav.style.width).toBe('268px')
  })

  it('offers no handle on the collapsed rail, where there is nothing to resize', () => {
    show(true)
    expect(screen.queryByRole('separator')).toBeNull()
  })

  it('remembers the width without touching the collapsed preference', () => {
    const { unmount } = show()
    const bar = screen.getByRole('separator', { name: 'Resize the sidebar' })
    fireEvent.pointerDown(bar, { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 60 })
    fireEvent.pointerUp(window, { clientX: 60 })
    expect(localStorage.getItem('c4x.sidebar.width')).toBe('268')
    // COLLAPSING MUST NOT FORGET THE WIDTH, and a drag must not un-collapse.
    expect(localStorage.getItem('c4x.sidebar.collapsed')).toBeNull()
    unmount()
    const again = show()
    expect(again.container.querySelector('nav')!.style.width).toBe('268px')
  })

  it('never takes more than half a narrow window', () => {
    const was = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { value: 600, configurable: true })
    const { container } = show()
    const bar = screen.getByRole('separator', { name: 'Resize the sidebar' })
    fireEvent.pointerDown(bar, { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 900 })
    fireEvent.pointerUp(window, { clientX: 900 })
    expect(container.querySelector('nav')!.style.width).toBe('300px')
    Object.defineProperty(window, 'innerWidth', { value: was, configurable: true })
  })

  it('puts a stored width that makes no sense back to the default', () => {
    localStorage.setItem('c4x.sidebar.width', 'not a number')
    const { container, unmount } = show()
    expect(container.querySelector('nav')!.style.width).toBe('208px')
    unmount()
    localStorage.setItem('c4x.sidebar.width', '99999')
    expect(show().container.querySelector('nav')!.style.width).toBe('420px')
  })
})
