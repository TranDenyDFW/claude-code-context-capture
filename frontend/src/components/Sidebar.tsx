import { useEffect, useState } from 'react'
import {
  Activity, BarChart3, Columns3, GaugeCircle, LayoutGrid, PanelLeftClose, PanelLeftOpen,
  Receipt, Stethoscope, Table2,
} from 'lucide-react'
import type { TabInfo } from '@/api'

/**
 * The left navigation, collapsible.
 *
 * Collapsible because both answers were right. A sidebar is what a reader expects and it scales
 * past a row of eight, but this page is dense and its charts are wide, and spending 200px of
 * horizontal room permanently on navigation is a real cost on a 1,400px window. So it collapses to
 * a rail, and the choice is remembered.
 *
 * THE ICON MAP IS PRESENTATION ONLY, and it is allowed to be incomplete. Everything else this
 * frontend needs about a tab comes from `/api/tabs`, because a hand-written list of tabs is how the
 * proposal that started this work ended up planning three pages that no longer exist. An icon is
 * not a fact about the data, so a tab with no entry here still appears, with a generic glyph, and
 * nothing is hidden by the omission.
 */
const ICONS: Record<string, typeof Activity> = {
  'tab-summary': LayoutGrid,
  'tab-sessions': Table2,
  'tab-session': Activity,
  'tab-compactions': Columns3,
  'tab-window': GaugeCircle,
  'tab-cost': Receipt,
  'tab-compare': BarChart3,
  'tab-diagnostics': Stethoscope,
}

/**
 * The two kinds of tab, and the fact that separates them.
 *
 * `scoped` comes from the server, where a tab declares it on the same line as its name. The
 * grouping is drawn from that single source rather than from a second list here, because a list
 * of tab ids in the frontend is exactly what the payload exists to replace.
 */
const GROUPS: { key: string; heading: string; holds: (scoped?: boolean) => boolean }[] = [
  { key: 'store', heading: 'All', holds: (scoped) => scoped === false },
  { key: 'selection', heading: 'Selection', holds: (scoped) => scoped !== false },
]

const REMEMBERED = 'c4x.sidebar.collapsed'

export function useCollapsed(): [boolean, (next: boolean) => void] {
  const [collapsed, setCollapsed] = useState(() => {
    // try/catch because storage throws outright in some contexts (private windows, blocked site
    // data), and a navigation bar must not fail to render over a preference.
    try {
      return localStorage.getItem(REMEMBERED) === '1'
    } catch {
      return false
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem(REMEMBERED, collapsed ? '1' : '0')
    } catch {
      /* a preference that cannot be saved is not worth an error */
    }
  }, [collapsed])
  return [collapsed, setCollapsed]
}

export function Sidebar({
  tabs,
  active,
  onPick,
  collapsed,
  onToggle,
  about,
}: {
  tabs: TabInfo[]
  active: string | null
  onPick: (id: string) => void
  collapsed: boolean
  onToggle: () => void
  /**
   * What the OPEN tab says about itself, appended to that tab's hover.
   *
   * Only the open one: these come from the rendered pane, so the other tabs have not been built
   * and there is nothing honest to put on them. It is the same sentence either way, and the tab
   * is the thing on screen that already answers "what am I looking at".
   */
  about?: string[]
}) {
  const Toggle = collapsed ? PanelLeftOpen : PanelLeftClose
  return (
    <nav
      aria-label="Tabs"
      className={`sticky top-0 flex h-dvh shrink-0 flex-col gap-1 border-r border-edge/60 bg-panel
                  px-2 py-3 transition-[width] duration-200 ${collapsed ? 'w-[3.5rem]' : 'w-52'}`}
    >
      <div className={`mb-2 flex items-center ${collapsed ? 'justify-center' : 'px-2'}`}>
        <span className="text-lg font-semibold tracking-tight text-ink">
          {collapsed ? 'C' : 'C4X'}
        </span>
      </div>

      {/*
        GROUPED BY WHAT THE HEADER SELECTION REACHES, with the groups named on the page.
        
        The list interleaved them: Summary, All sessions, Session, Compactions, Window, Cost,
        Compare, Diagnostics, so the three tabs whose numbers NEVER move sat at positions 1, 2
        and 8. Picking a session changed five of the eight and left three identical, and nothing
        on screen said which was which. `scoped` was already in the payload and was spent on a
        line of hover text, which is the one place a reader looking for that answer will not find
        it. The server decides the membership; this only draws it.

        A tab the server does not classify falls in with the selection group rather than being
        dropped, on the same rule as the icon map above: a presentation gap must not hide a tab.
      */}
      {GROUPS.map(({ key, heading, holds }) => {
        const mine = tabs.filter((tab) => holds(tab.scoped))
        if (!mine.length) return null
        return (
          <div key={key} className="contents">
            {collapsed ? (
              // A HEADING IS NOT READABLE IN A 3.5rem RAIL, and a truncated one is worse than
              // none. The rule stays visible so the split survives the collapse, and the group
              // keeps its name for a screen reader either way.
              <hr className="my-1 border-edge/60" aria-hidden="true" />
            ) : (
              // BIGGER THAN A TAB IS NOT THE GOAL; DIFFERENT FROM ONE IS. A tab is text-sm and
              // normal weight, so the group label reads as a peer at that size. It sits one step
              // down at text-xs, semibold, wide-tracked and faint, which is a heading rather than
              // a thing you click. No uppercase: the words are "All" and "Selection", and
              // shouting them made a two-word label look like a section of its own.
              <h2 className="mt-2.5 px-2.5 pb-1 text-xs font-semibold tracking-wider
                             text-ink-faint">
                {heading}
              </h2>
            )}
            {mine.map((tab) => {
            const Icon = ICONS[tab.id] ?? Activity
            const current = tab.id === active
            return (
              <button
                key={tab.id}
                // The same id the Dash page used, which `tools/screenshots.py` selects on.
                id={`btn-${tab.id}`}
                onClick={() => onPick(tab.id)}
                aria-current={current ? 'page' : undefined}
                // NAMED BY THE LABEL. With the description in `title` and the label hidden when
                // collapsed, the accessible name was the whole sentence, so a screen reader read a
                // paragraph per tab and the name did not contain the visible text. The label is the
                // name; the sentence is the description.
                aria-label={tab.label}
                aria-description={[tab.help, ...(current ? about ?? [] : [])]
                  .filter(Boolean).join(' ')}
                // WHAT THIS TAB IS, on the tab. The sentence used to be printed across the top of
                // every pane; it belongs on the thing it describes. When collapsed the label leads,
                // because the rail shows only an icon.
                title={[
                  collapsed ? tab.label : '',
                  tab.help,
                  tab.scoped === false ? 'Store-wide: the header selection does not change it.' : '',
                  ...(current ? about ?? [] : []),
                ].filter(Boolean).join('\n\n')}
                className={`flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm
                            transition-colors duration-150 ${collapsed ? 'justify-center' : ''} ${
                              current
                                ? 'bg-accent/12 text-ink'
                                : 'text-ink-dim hover:bg-panel-raised hover:text-ink'
                            }`}
              >
                <Icon
                  size={15}
                  aria-hidden="true"
                  className={current ? 'text-accent' : 'text-ink-faint'}
                />
                {!collapsed && <span className="truncate">{tab.label}</span>}
              </button>
            )
            })}
          </div>
        )
      })}

      <button
        onClick={onToggle}
        aria-label={collapsed ? 'Expand the sidebar' : 'Collapse the sidebar'}
        aria-expanded={!collapsed}
        className={`mt-auto flex items-center gap-2.5 rounded-md px-2.5 py-2 text-xs text-ink-faint
                    transition-colors duration-150 hover:bg-panel-raised hover:text-ink-dim
                    ${collapsed ? 'justify-center' : ''}`}
      >
        <Toggle size={15} aria-hidden="true" />
        {!collapsed && <span>Collapse</span>}
      </button>
    </nav>
  )
}
