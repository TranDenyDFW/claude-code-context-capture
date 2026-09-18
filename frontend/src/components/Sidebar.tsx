import { useEffect, useState } from 'react'
import { ResizeHandle } from './ResizeHandle'
import { clampSize } from './useDragSize'
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
const WIDTH = 'c4x.sidebar.width'
/** `w-52`, the width this rail has had; the floor is where the group headings stop being readable. */
export const SIDEBAR = { DEFAULT: 208, MIN: 144, MAX: 420 } as const

/** The widest this may get: never more than half the window, whatever is remembered. */
export function sidebarCeiling(viewport = window.innerWidth): number {
  return Math.max(SIDEBAR.MIN, Math.min(SIDEBAR.MAX, Math.floor(viewport / 2)))
}

/**
 * How wide the rail is, remembered.
 *
 * Kept apart from the collapsed preference on purpose: collapsing does not forget the width, and
 * dragging does not un-collapse. Somebody who narrows the rail and then collapses it gets their
 * own width back when they expand it again. A drag below the minimum CLAMPS; it does not collapse,
 * because a gesture that silently flips a different remembered choice is a surprise, and the
 * collapse button is an inch away.
 */
export function useSidebarWidth(): [number, (next: number) => void] {
  const [width, setWidth] = useState(() => {
    try {
      const said = Number(localStorage.getItem(WIDTH))
      return Number.isFinite(said) && said > 0 ? clampSize(said, SIDEBAR.MIN, SIDEBAR.MAX) : SIDEBAR.DEFAULT
    } catch {
      return SIDEBAR.DEFAULT
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem(WIDTH, String(width))
    } catch {
      /* a preference that cannot be saved is not worth an error */
    }
  }, [width])
  return [width, setWidth]
}

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
  const [width, setWidth] = useSidebarWidth()
  // THE TRANSITION IS FOR THE COLLAPSE, NOT FOR THE HANDLE. A width transition turns a drag into
  // a rail that lags a fifth of a second behind the pointer, and under `prefers-reduced-motion`
  // it is worse than that: measured in Chrome, the 0.01ms transition the reduced-motion rule
  // forces sat at time zero in a page that was not painting, so the rail kept its OLD width while
  // the style said the new one. So any change that comes from the handle turns it off for that
  // commit, and the collapse keeps its animation.
  const [adjusting, setAdjusting] = useState(false)
  const size = (next: number) => {
    setAdjusting(true)
    setWidth(next)
  }
  useEffect(() => {
    if (!adjusting) return
    const settle = setTimeout(() => setAdjusting(false), 250)
    return () => clearTimeout(settle)
  }, [adjusting, width])
  return (
    <nav
      aria-label="Tabs"
      style={collapsed ? undefined : { width }}
      className={`relative sticky top-0 flex h-dvh shrink-0 flex-col gap-1 border-r border-edge/60
                  bg-panel px-2 py-3 ${adjusting ? '' : 'transition-[width] duration-200'}
                  ${collapsed ? 'w-[3.5rem]' : ''}`}
    >
      {/*
        NO HANDLE ON THE RAIL. Collapsed it is 3.5rem of icons, there is nothing to resize, and a
        divider that does nothing is worse than none.
      */}
      {!collapsed && (
        <ResizeHandle
          label="Resize the sidebar"
          size={width}
          min={SIDEBAR.MIN}
          max={sidebarCeiling()}
          onSize={size}
          onReset={() => size(SIDEBAR.DEFAULT)}
          className="absolute inset-y-0 -right-1 z-20 w-2"
        />
      )}
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
              // down at text-xs, semibold, wide-tracked, faint and uppercased, which reads as a
              // heading rather than as a thing you click.
              //
              // THE SHOUT IS A CSS TRANSFORM, NOT THE STRING. The words in the DOM stay "All" and
              // "Selection", so the accessible name, a find-in-page and every assertion on this
              // text still see what was written.
              <h2 className="mt-2.5 px-2.5 pb-1 text-xs font-semibold tracking-wider uppercase
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
