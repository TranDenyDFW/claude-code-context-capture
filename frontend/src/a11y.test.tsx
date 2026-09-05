/**
 * Accessibility as a GATE, not as a claim.
 *
 * Everything else in this suite asserts a specific thing I thought to check: a landmark exists, a
 * toggle says which way it goes, the active tab is marked for a screen reader. That catches only
 * what I already had in mind, and almost every defect in this project came from a check nobody
 * ran rather than from a check that failed.
 *
 * axe runs the rules instead. It catches the ones I did not think of.
 *
 * IT DOES NOT CHECK COLOUR CONTRAST HERE, and the first version of this comment claimed it did.
 * Measured: a paragraph of #0e1218 on #0d1117, which is unreadable, comes back as
 * `violations: []` and `incomplete: ['color-contrast']`. jsdom does not resolve computed colours
 * the way a browser does, so axe declines to decide rather than failing, and a gate that reads
 * only `violations` sees nothing. Contrast has to be checked against a real browser; that is a
 * separate pass, not this one.
 *
 * WHAT ELSE IT CANNOT DO, so nobody reads a green run as more than it is: axe checks a rendered
 * DOM against a rule set. It cannot tell whether the tab order makes sense, whether a label is
 * accurate, or whether the page is usable with a screen reader. A pass means no rule was broken.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import axe from 'axe-core'
import { Pane } from './components/Pane'
import { Palette } from './components/Palette'
import { CompareArms } from './components/CompareArms'
import { ProjectMoves } from './components/ProjectMoves'
import { Sidebar } from './components/Sidebar'
import { Inspector } from './components/Inspector'
import { TablePage } from './components/TablePage'
import type { TabPayload } from './api'

vi.mock('./components/Plot', () => ({ Plot: () => <div data-testid="chart" /> }))

/** The rules worth gating on here, and why the rest are left out. */
const RULES = {
  runOnly: {
    type: 'tag' as const,
    // WCAG 2 A and AA. `best-practice` is deliberately excluded: it flags things like "all page
    // content should be inside a landmark", which is true of a whole page and false of a component
    // rendered on its own, so it would fail every test here for a reason that is not a defect.
    values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'],
  },
}

async function violations(node: HTMLElement) {
  const result = await axe.run(node, RULES)
  return result.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    where: v.nodes.map((n) => n.html.slice(0, 90)),
  }))
}

/** Rules axe could not DECIDE here. Reported so an undecided rule is never read as a passing one. */
async function undecided(node: HTMLElement) {
  const result = await axe.run(node, RULES)
  return result.incomplete.map((v) => v.id)
}

function payload(over: Partial<TabPayload> = {}): TabPayload {
  return {
    tab: 'tab-test', session: null, scope: 'main', cohort: null,
    tables: [], figures: [], text: [], plotly: [], ...over,
  }
}

const tabs = [
  { id: 'tab-summary', label: 'Summary' },
  { id: 'tab-cost', label: 'Cost' },
]

describe('axe finds no WCAG A or AA violation in', () => {
  it('a pane with a table, stats and a scope bar', async () => {
    const rows = [{ session_id: 'a', turns: 10, est_usd: null }]
    const { container } = render(
      <Pane
        payload={payload({
          scoped: false,
          population: 'Store-wide. Not affected by the header selection.',
          stats: [{ label: 'sessions', value: '1,325', sub: 'in the store' }],
          tables: [{ id: 't', columns: ['turns', 'est_usd'], rows,
                     tooltips: { turns: 'what a turn is' } }],
          meta: [{
            id: 't', title: 'Sessions', filterable: true, page_size: null,
            columns: [
              { id: 'turns', label: 'Turns', numeric: true, specifier: ',', align: 'right',
                hidden: false, bands: [] },
              { id: 'est_usd', label: 'Est. USD', numeric: true, specifier: '.2f',
                align: 'right', hidden: false, bands: [] },
            ],
          }],
          details: [{ summary: 'The query behind this table', body: ['SELECT 1'],
                      table_index: 0 }],
        })}
        onRowClick={() => {}}
      />,
    )
    expect(await violations(container)).toEqual([])
  })

  // THE TWO SURFACES THE GATE DID NOT COVER. Both shipped user-facing and neither was rendered
  // here: the drawer a chart click opens, and the page a table opens into. The drawer is also
  // where aria-modal and a Tab trap were added and then reverted, so it is the one most worth
  // watching.
  it('the inspector drawer, with fields, a full text and the rows behind a point', async () => {
    const { container } = render(
      <Inspector
        content={{
          subject: 1,
          title: 'sess-1',
          source: 'sessions by peak',
          fields: [['series', 'peaks'], ['y', '990,000']],
          text: 'the whole message',
          rows: { name: 'Sessions', table: { id: 't', columns: ['title'], rows: [{ title: 'the one' }] } },
          onOpen: () => {},
          onSelectSession: () => {},
        }}
        onClose={() => {}}
      />,
    )
    expect(await violations(container)).toEqual([])
  })

  it('one table on a page of its own, with its filter chip and its query', async () => {
    const { container } = render(
      <TablePage
        payload={payload({
          population: 'the whole store, every session',
          tables: [{ id: 't', columns: ['title'], rows: [{ session_id: 's1', title: 'the one' }] }],
          meta: [{ id: 't', title: 'Sessions', note: 'why it matters', columns: [],
                   filterable: true, page_size: null }],
          details: [{ summary: 'The query behind this table', body: ['SELECT 1'], table_index: 0 }],
        })}
        index={0}
        filter={{ key: 'session_id', value: 's1' }}
        onBack={() => {}}
      />,
    )
    expect(await violations(container)).toEqual([])
  })

  it('the sidebar, expanded', async () => {
    const { container } = render(
      <Sidebar tabs={tabs} active="tab-summary" collapsed={false} onPick={() => {}}
               onToggle={() => {}} />,
    )
    expect(await violations(container)).toEqual([])
  })

  it('the sidebar, collapsed to icons', async () => {
    // The state most likely to fail: labels are gone and the only name is a tooltip.
    const { container } = render(
      <Sidebar tabs={tabs} active="tab-summary" collapsed onPick={() => {}} onToggle={() => {}} />,
    )
    expect(await violations(container)).toEqual([])
  })

  it('the command palette', async () => {
    const { container } = render(
      <Palette
        open
        onClose={() => {}}
        tabs={tabs}
        cohorts={[{ value: '__all__', label: 'All sessions (317)' }]}
        sessions={[{ value: 's1', label: 'F:\\SecDb  ·  A title  ·  2026-08-31 01:47' }]}
        onPick={() => {}}
      />,
    )
    expect(await violations(container)).toEqual([])
  })

  // Two selects and a pair of toggles. Both selects carry a visible label element, which is
  // what a screen reader reads out as "Arm A" rather than "combo box".
  it('the compare arms', async () => {
    const { container } = render(
      <CompareArms
        selection={{ session: 's1' }}
        sessions={[{ value: 's1', label: 'P:\\x  ·  A title  ·  2026-08-31 01:47' },
                   { value: 's2', label: 'P:\\x  ·  A title (fork)  ·  2026-08-30 21:08' }]}
        cohorts={[{ value: '__all__', label: 'All sessions (317)' }]}
        onChange={() => {}}
      />,
    )
    expect(await violations(container)).toEqual([])
  })

  // The dialog that can delete a project. It is the densest set of form controls in the app: a
  // file input, a checkbox, a text field and three buttons, and every one of them used to be
  // unlabelled until this ran.
  // A real Windows path: the backslash is doubled so the literal holds exactly one.
  const PROJECT = 'F:\\SecDb'

  it('the project dialog', async () => {
    const { container } = render(
      <ProjectMoves
        // ONE CONSTANT FOR BOTH, because these two lines are escaped by DIFFERENT rules and
        // looked identical. A JSX attribute processes no escapes, so the `cohort=` string was
        // already a real Windows path; the line below is a JavaScript literal, where a lone
        // backslash before S is not an escape and is dropped without a word. So this a11y
        // check ran against `project::F:SecDb`, a cohort shape the app cannot produce.
        cohort={`project::${PROJECT}`}
        cohorts={[{ value: `project::${PROJECT}`, label: `Project: ${PROJECT} (12)` }]}
        writesEnabled
        onChanged={() => {}}
      />,
    )
    container.querySelector('button')?.click()
    expect(await violations(container.ownerDocument.body)).toEqual([])
  })
})

describe('the gate is honest about what it did not check', () => {
  it('names colour contrast as UNDECIDED rather than passed', async () => {
    // If this ever starts coming back decided, the environment gained real colour resolution and
    // the docstring above needs revisiting. Until then, contrast is not covered by this file.
    const { container } = render(
      <div style={{ background: '#0d1117' }}>
        <p style={{ color: '#0e1218' }}>unreadable on purpose</p>
      </div>,
    )
    expect(await violations(container)).toEqual([])
    expect(await undecided(container)).toContain('color-contrast')
  })
})

describe('the gate itself', () => {
  it('REPORTS a violation when there is one, so a pass means something', async () => {
    // A must-fail control for the checker, not for the app. An accessibility suite that cannot
    // fail is the most reassuring kind of useless.
    const { container } = render(
      <div>
        <img src="x.png" />
        <button aria-label="" />
      </div>,
    )
    const found = await violations(container)
    expect(found.length).toBeGreaterThan(0)
    expect(found.map((v) => v.id)).toContain('image-alt')
  })
})

describe('a tab button is named by what it shows', () => {
  it('has the visible label as its accessible name, and the sentence as its description', () => {
    const tabs = [
      { id: 'tab-summary', label: 'Summary', help: 'Findings worth acting on.', scoped: false },
      { id: 'tab-cost', label: 'Cost', help: 'What was paid for twice.', scoped: true },
    ]
    // COLLAPSED, which is where the defect was. Expanded, the visible label already supplies the
    // accessible name, so the assertion passed with the aria-label deleted: a gate that cannot
    // fail. A review found that; both states are checked now.
    const { unmount } = render(
      <Sidebar tabs={tabs} active="tab-summary" onPick={() => {}} collapsed onToggle={() => {}} />)
    const collapsed = screen.getByRole('button', { name: 'Summary' })
    expect(collapsed.getAttribute('aria-description')).toBe('Findings worth acting on.')
    expect(collapsed.textContent).not.toContain('Summary')
    expect(screen.queryByRole('button', { name: /Findings worth acting on/ })).toBeNull()
    unmount()

    render(<Sidebar tabs={tabs} active="tab-summary" onPick={() => {}} collapsed={false} onToggle={() => {}} />)
    expect(screen.getByRole('button', { name: 'Summary' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Cost' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Findings worth acting on/ })).toBeNull()
  })
})
