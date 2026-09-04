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
}: {
  tabs: TabInfo[]
  active: string | null
  onPick: (id: string) => void
  collapsed: boolean
  onToggle: () => void
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

      {tabs.map((tab) => {
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
            aria-description={tab.help}
            // WHAT THIS TAB IS, on the tab. The sentence used to be printed across the top of
            // every pane; it belongs on the thing it describes. When collapsed the label leads,
            // because the rail shows only an icon.
            title={[
              collapsed ? tab.label : '',
              tab.help,
              tab.scoped === false ? 'Store-wide: the header selection does not change it.' : '',
            ].filter(Boolean).join('\n')}
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
