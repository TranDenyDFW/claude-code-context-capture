/**
 * The navigation says which tabs the header selection reaches, on the page rather than on hover.
 *
 * The list interleaved them: Summary, All sessions, Session, Compactions, Window, Cost, Compare,
 * Diagnostics, so the three tabs whose numbers never move sat at positions 1, 2 and 8. Picking a
 * session changed five of the eight and left three identical, and the only thing on screen saying
 * so was a line of tooltip.
 *
 * `scoped` comes from the server. These check that the sidebar draws the split it is given and does
 * not invent one, which is the failure that matters: a hand-written list of tab ids in the frontend
 * is exactly what the payload exists to replace.
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { TabInfo } from '@/api'
import { Sidebar } from './Sidebar'

const TABS: TabInfo[] = [
  { id: 'tab-summary', label: 'Summary', scoped: false },
  { id: 'tab-sessions', label: 'All sessions', scoped: false },
  { id: 'tab-diagnostics', label: 'Diagnostics', scoped: false },
  { id: 'tab-session', label: 'Session', scoped: true },
  { id: 'tab-cost', label: 'Cost', scoped: true },
]

function draw(tabs = TABS, collapsed = false) {
  return render(
    <Sidebar tabs={tabs} active="tab-summary" onPick={() => {}}
             collapsed={collapsed} onToggle={() => {}} />,
  )
}

/** The tab labels in the group whose heading reads `heading`, in document order.
 *
 * Walks from the heading through its own siblings, NOT through the nav's children: each group is
 * wrapped in a `display: contents` div, which flattens the group visually and not in the DOM.
 */
function labelsUnder(heading: string): string[] {
  const title = screen.getByText(heading)
  const group = title.parentElement!
  const nodes = [...group.children]
  const at = nodes.indexOf(title)
  const out: string[] = []
  for (const node of nodes.slice(at + 1)) {
    if (node.tagName === 'H2') break
    if (node.tagName === 'BUTTON' && node.id.startsWith('btn-')) out.push(node.textContent ?? '')
  }
  return out
}

describe('the sidebar', () => {
  it('names both groups on the page, not in a tooltip', () => {
    draw()
    expect(screen.getByText('All')).toBeTruthy()
    expect(screen.getByText('Selection')).toBeTruthy()
  })

  it('puts each tab under the group the server put it in', () => {
    draw()
    expect(labelsUnder('All')).toEqual(['Summary', 'All sessions', 'Diagnostics'])
    expect(labelsUnder('Selection')).toEqual(['Session', 'Cost'])
  })

  it('still renders every tab it was given (gate can fail)', () => {
    // THE FAILURE A GROUPING INTRODUCES. A tab that matches no group would simply vanish, and a
    // test that only checked the two groups would not notice.
    draw()
    for (const tab of TABS) expect(screen.getByText(tab.label)).toBeTruthy()
  })

  it('keeps a tab the server does not classify, in the selection group', () => {
    // Same rule as the icon map: a presentation gap must not hide a tab.
    draw([...TABS, { id: 'tab-new', label: 'Something New' }])
    expect(screen.getByText('Something New')).toBeTruthy()
    expect(labelsUnder('Selection')).toContain('Something New')
  })

  it('drops the headings when collapsed but keeps the split', () => {
    // A heading is not readable in a 3.5rem rail, and a truncated one is worse than none.
    const { container } = draw(TABS, true)
    expect(screen.queryByText('All')).toBeNull()
    expect(container.querySelectorAll('hr').length).toBe(2)
  })

  it('shows one group when every tab is in it', () => {
    draw(TABS.filter((t) => t.scoped === false))
    expect(screen.getByText('All')).toBeTruthy()
    expect(screen.queryByText('Selection')).toBeNull()
  })
})
