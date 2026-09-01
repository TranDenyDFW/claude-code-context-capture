import { useEffect, useMemo, useRef, useState } from 'react'
import type { Cohort, TabInfo } from '@/api'

/**
 * Cmd/Ctrl+K. Jump to a tab, pick a population, or find a session BY WHEN IT RAN.
 *
 * This is the honest replacement for the session dropdown, not a decoration. A dropdown of 317
 * titles asks a reader to recognise a name they never chose and will not remember; the useful
 * handle on a session is its date and time, which is what the app's own label already leads with
 * once the parts are reordered. Typing "08-31" or a project path finds it in one keystroke each.
 *
 * ONE LIST, not three tabs of a wizard. Everything the header can do is in here, ranked, because a
 * palette that makes you first choose a category is a menu with extra steps.
 */

export interface Choice {
  kind: 'tab' | 'population' | 'session'
  id: string
  label: string
  hint?: string
}

function useHotkey(onOpen: () => void) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        onOpen()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onOpen])
}

/** Every term must appear somewhere, in any order, so "secdb 08-31" works. */
function matches(haystack: string, needle: string): boolean {
  const text = haystack.toLowerCase()
  return needle.toLowerCase().split(/\s+/).filter(Boolean).every((term) => text.includes(term))
}

export function Palette({
  open,
  onClose,
  tabs,
  cohorts,
  sessions,
  onPick,
}: {
  open: boolean
  onClose: () => void
  tabs: TabInfo[]
  cohorts: Cohort[]
  sessions: Cohort[]
  onPick: (choice: Choice) => void
}) {
  const [query, setQuery] = useState('')
  const [cursor, setCursor] = useState(0)
  const field = useRef<HTMLInputElement>(null)
  const list = useRef<HTMLDivElement>(null)

  const choices = useMemo<Choice[]>(() => {
    const out: Choice[] = tabs.map((t) => ({ kind: 'tab', id: t.id, label: t.label, hint: 'tab' }))
    for (const c of cohorts) out.push({ kind: 'population', id: c.value, label: c.label,
                                        hint: 'population' })
    for (const s of sessions) {
      // Reordered so the DATE leads. `selector_options` builds "project · title · when", which is
      // the right order for a list grouped by project and the wrong one for a search by time.
      const parts = s.label.split('·').map((p) => p.trim())
      const label = parts.length >= 3
        ? `${parts[parts.length - 1]}  ·  ${parts[0]}`
        : s.label
      out.push({ kind: 'session', id: s.value, label, hint: parts[1] || 'session' })
    }
    return out
  }, [tabs, cohorts, sessions])

  const found = useMemo(() => {
    const needle = query.trim()
    const pool = needle
      ? choices.filter((c) => matches(`${c.label} ${c.hint ?? ''}`, needle))
      : choices
    return pool.slice(0, 60)
  }, [choices, query])

  useEffect(() => {
    if (open) {
      setQuery('')
      setCursor(0)
      // Focused on the next frame: the input does not exist until this render commits.
      requestAnimationFrame(() => field.current?.focus())
    }
  }, [open])

  useEffect(() => setCursor(0), [query])

  // Keep the highlighted row in view when arrowing past the fold. Called defensively: jsdom has no
  // `scrollIntoView` at all, and scrolling is a nicety that must not be able to break selection.
  useEffect(() => {
    const active = list.current?.querySelector('[data-active="true"]')
    if (active instanceof HTMLElement) active.scrollIntoView?.({ block: 'nearest' })
  }, [cursor, found])

  useHotkey(() => (open ? onClose() : field.current?.focus()))

  if (!open) return null

  const commit = (choice: Choice | undefined) => {
    if (!choice) return
    onPick(choice)
    onClose()
  }

  return (
    <div
      // Click the backdrop to dismiss. Escape does the same from the keyboard.
      onMouseDown={onClose}
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 p-4 pt-[12vh]
                 backdrop-blur-sm"
      role="presentation"
    >
      <div
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Jump to a tab, population or session"
        className="w-full max-w-[42rem] overflow-hidden rounded-lg bg-panel shadow-float"
      >
        <input
          ref={field}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape') return onClose()
            if (event.key === 'ArrowDown') {
              event.preventDefault()
              setCursor((c) => Math.min(c + 1, found.length - 1))
            } else if (event.key === 'ArrowUp') {
              event.preventDefault()
              setCursor((c) => Math.max(c - 1, 0))
            } else if (event.key === 'Enter') {
              event.preventDefault()
              commit(found[cursor])
            }
          }}
          placeholder="Jump to a tab, a population, or a session by date"
          aria-label="Search tabs, populations and sessions"
          className="w-full bg-transparent px-4 py-3.5 text-md text-ink outline-none
                     placeholder:text-ink-faint"
        />
        <div ref={list} className="max-h-[52vh] overflow-auto border-t border-edge/60">
          {found.length === 0 && (
            <p className="px-4 py-6 text-center text-sm text-ink-dim">Nothing matches that.</p>
          )}
          {found.map((choice, index) => (
            <button
              key={`${choice.kind}:${choice.id}`}
              data-active={index === cursor ? 'true' : 'false'}
              onMouseEnter={() => setCursor(index)}
              onClick={() => commit(choice)}
              className={`flex w-full items-center gap-3 px-4 py-2 text-left text-sm
                          transition-colors duration-150 ${
                            index === cursor ? 'bg-accent/12 text-ink' : 'text-ink-dim'
                          }`}
            >
              <span
                className={`w-[4.5rem] shrink-0 text-2xs tracking-wide uppercase ${
                  choice.kind === 'tab' ? 'text-accent' : 'text-ink-faint'
                }`}
              >
                {choice.hint === 'tab' || choice.hint === 'population' ? choice.hint : 'session'}
              </span>
              <span className="truncate">{choice.label}</span>
            </button>
          ))}
        </div>
        <div className="flex gap-4 border-t border-edge/60 px-4 py-2 text-2xs text-ink-faint">
          <span>enter to go</span>
          <span>up and down to move</span>
          <span>esc to close</span>
        </div>
      </div>
    </div>
  )
}

export { matches }
