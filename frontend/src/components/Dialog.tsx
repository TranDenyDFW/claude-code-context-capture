import { useEffect, useRef } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode, RefObject } from 'react'
import { Portal } from './Portal'

/**
 * A window over the page: the one chrome for every dialog that dims the page behind it.
 *
 * LIFTED FROM THE PROJECT DIALOG, the user's choice ("do it like the Project button - greys out
 * background"): the backdrop that dims and blurs the page, the panel as a column with a height
 * cap so the middle scrolls and the header and footer stay put, Escape and a click on the
 * backdrop to close, `role="dialog"` with `aria-modal`. Rendered through `Portal`, because the
 * header's backdrop filter would clip a fixed element to the header's own box (Portal.tsx).
 *
 * WHAT IT ADDS TO THAT CHROME, for the two windows it was written for (the Adopt window, and the
 * confirm that opens on top of it):
 * - Escape is scoped by DOM containment: the handler acts only when the key was pressed inside
 *   THIS panel. A nested dialog is a React child of its opener, so its keys bubble through the
 *   React tree to this handler even though its DOM is portaled elsewhere; without the check an
 *   Escape in the inner window would close both.
 * - Focus goes to `initialFocus` (else the panel) on open and back to `opener` on close, which is
 *   what opening and closing a window mean to a keyboard.
 * - Tab wraps inside the panel, so the keyboard cannot walk out into the dimmed page.
 * - `busy` keeps a click on the backdrop from closing a window whose write is under way.
 *
 * ProjectMoves and Palette keep their own copies of the chrome for now; they move to this
 * component on their own, not as part of the change that introduced it.
 */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export function Dialog({
  id,
  label,
  onClose,
  header,
  footer,
  children,
  width = 'max-w-[44rem]',
  z = 'z-50',
  busy = false,
  initialFocus,
  opener,
}: {
  id?: string
  /** The accessible name of the window. */
  label: string
  onClose: () => void
  header?: ReactNode
  footer?: ReactNode
  children: ReactNode
  /** A Tailwind max-width class; the literal has to appear in some source file to be generated. */
  width?: string
  /** A Tailwind z-index class; a window opened from inside another one sits above it. */
  z?: string
  /** A write is under way: the backdrop click is ignored. */
  busy?: boolean
  /** Where focus goes on open; the panel itself when absent. */
  initialFocus?: RefObject<HTMLElement | null>
  /** The control that opened the window; focus returns to it on close. */
  opener?: RefObject<HTMLElement | null>
}) {
  const panel = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const target = initialFocus?.current ?? panel.current
    target?.focus()
    const back = opener?.current ?? null
    return () => {
      back?.focus()
    }
  }, [initialFocus, opener])

  function onKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    const here = panel.current
    if (!here || !(event.target instanceof Node) || !here.contains(event.target)) return
    if (event.key === 'Escape') {
      event.stopPropagation()
      onClose()
      return
    }
    if (event.key !== 'Tab') return
    const focusable = Array.from(here.querySelectorAll<HTMLElement>(FOCUSABLE))
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    const active = document.activeElement
    if (event.shiftKey && (active === first || active === here)) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && active === last) {
      event.preventDefault()
      first.focus()
    }
  }

  return (
    <Portal>
      <div
        onMouseDown={() => {
          if (!busy) onClose()
        }}
        role="presentation"
        className={`fixed inset-0 ${z} flex items-start justify-center bg-black/60 p-4 pt-[10vh]
                    backdrop-blur-sm`}
      >
        <div
          ref={panel}
          id={id}
          role="dialog"
          aria-modal="true"
          aria-label={label}
          tabIndex={-1}
          onMouseDown={(event) => event.stopPropagation()}
          onKeyDown={onKeyDown}
          className={`flex max-h-[85vh] w-full ${width} flex-col overflow-hidden rounded-lg
                      bg-panel shadow-float outline-none`}
        >
          {header ? <div className="shrink-0 border-b border-edge px-5 py-3">{header}</div> : null}
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer ? (
            <div className="flex shrink-0 flex-wrap items-center justify-end gap-3 border-t border-edge px-5 py-3">
              {footer}
            </div>
          ) : null}
        </div>
      </div>
    </Portal>
  )
}
