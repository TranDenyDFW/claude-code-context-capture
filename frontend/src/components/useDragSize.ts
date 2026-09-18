import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, MouseEvent, PointerEvent as ReactPointerEvent, DragEvent } from 'react'

/**
 * One size, dragged with a pointer or moved with the keyboard, clamped between two bounds.
 *
 * Every resizable thing on this page goes through here: a table column, the left navigation and
 * the right-hand details drawer. They differ in three ways only, and each is a parameter: which
 * axis, which direction growing means, and what the bounds are.
 *
 * THE MOVE IS TRACKED ON `window`, NOT ON THE HANDLE. With pointer capture the events still reach
 * `window`, so one code path serves a real browser (where capture keeps the drag alive when the
 * pointer leaves the element, crosses an iframe, or leaves the window) and jsdom (which has no
 * pointer capture at all). A test dispatches `pointerMove` at `window` and the same code runs.
 *
 * `setPointerCapture` IS CALLED ONLY WHEN IT EXISTS. jsdom implements PointerEvent and not the
 * capture methods, so an unguarded call throws a TypeError in every test that drags anything.
 *
 * ESCAPE PUTS IT BACK. A drag is a direct manipulation and needs an undo that costs nothing; the
 * size returns to what it was when the pointer went down, and the drag ends. Callers with their
 * own Escape handler (the drawer closes on Escape) are told through `onDragChange` so they can
 * stand aside while a drag is live.
 */
export interface DragSize {
  /** True while a pointer drag is under way. */
  dragging: boolean
  handleProps: {
    onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void
    onKeyDown: (event: KeyboardEvent<HTMLElement>) => void
    onClick: (event: MouseEvent<HTMLElement>) => void
    onDoubleClick: (event: MouseEvent<HTMLElement>) => void
    onDragStart: (event: DragEvent<HTMLElement>) => void
  }
}

export function clampSize(value: number, min: number, max: number): number {
  // min wins over max: a max below the min is a caller's mistake on a narrow window, and a size
  // below the minimum is the one that makes a panel unreadable.
  return Math.max(min, Math.min(Math.max(min, max), Math.round(value)))
}

export function useDragSize({
  size,
  min,
  max,
  axis = 'x',
  direction = 1,
  step = 16,
  coarse = 64,
  onSize,
  onReset,
  onDragChange,
}: {
  size: number
  min: number
  max: number
  axis?: 'x' | 'y'
  /** 1 when moving the handle right or down GROWS the thing; -1 for a right-hand drawer. */
  direction?: 1 | -1
  step?: number
  coarse?: number
  onSize: (next: number) => void
  /** Double-click. Omit and a double-click does nothing. */
  onReset?: () => void
  onDragChange?: (dragging: boolean) => void
}): DragSize {
  const [dragging, setDragging] = useState(false)
  const from = useRef<{ at: number; size: number } | null>(null)
  // The callbacks are read through a ref inside the window listeners, so the effect below is not
  // torn down and rebuilt on every render of a caller that passes an inline function.
  const latest = useRef({ min, max, direction, onSize, onDragChange })
  latest.current = { min, max, direction, onSize, onDragChange }
  const axisRef = useRef(axis)
  axisRef.current = axis

  const end = useCallback((restore: boolean) => {
    const start = from.current
    from.current = null
    unbind.current?.()
    setDragging(false)
    latest.current.onDragChange?.(false)
    if (restore && start) latest.current.onSize(start.size)
  }, [])

  /**
   * BOUND WHEN THE POINTER GOES DOWN, not by an effect watching a state flag.
   *
   * Measured in Chrome, which is the only place it shows: an effect keyed on `dragging` runs
   * AFTER React has re-rendered, so a `pointermove` arriving in the same tick as the `pointerdown`
   * reaches nothing and the drag does not start. In jsdom the state update and the effect flush
   * inside the same `fireEvent`, so every test passed while a real drag moved nothing.
   */
  const unbind = useRef<(() => void) | null>(null)
  const bind = useCallback(() => {
    const move = (event: PointerEvent) => {
      const start = from.current
      if (!start) return
      const now = axisRef.current === 'x' ? event.clientX : event.clientY
      const { min: low, max: high, direction: way, onSize: say } = latest.current
      say(clampSize(start.size + (now - start.at) * way, low, high))
    }
    const done = () => end(false)
    const cancel = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      end(true)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', done)
    window.addEventListener('pointercancel', done)
    window.addEventListener('lostpointercapture', done)
    window.addEventListener('keydown', cancel)
    unbind.current = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', done)
      window.removeEventListener('pointercancel', done)
      window.removeEventListener('lostpointercapture', done)
      window.removeEventListener('keydown', cancel)
      unbind.current = null
    }
  }, [end])

  // Nothing may outlive the component: a drag interrupted by an unmount would leave three
  // listeners on `window` holding a closure over a dead tree.
  useEffect(() => () => unbind.current?.(), [])

  function onPointerDown(event: ReactPointerEvent<HTMLElement>) {
    if (event.pointerType === 'mouse' && event.button !== 0) return
    // Kills the text selection AND the native dragstart that the column reorder listens for.
    event.preventDefault()
    event.stopPropagation()
    const target = event.currentTarget
    const capture = (target as Partial<HTMLElement>).setPointerCapture
    if (typeof capture === 'function') capture.call(target, event.pointerId)
    from.current = { at: axis === 'x' ? event.clientX : event.clientY, size }
    bind()
    setDragging(true)
    onDragChange?.(true)
  }

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    const grow = axis === 'x' ? 'ArrowRight' : 'ArrowDown'
    const shrink = axis === 'x' ? 'ArrowLeft' : 'ArrowUp'
    const by = event.shiftKey ? coarse : step
    let next: number | null = null
    if (event.key === grow) next = size + by * direction
    else if (event.key === shrink) next = size - by * direction
    else if (event.key === 'PageUp') next = size + coarse * direction
    else if (event.key === 'PageDown') next = size - coarse * direction
    else if (event.key === 'Home') next = min
    else if (event.key === 'End') next = max
    if (next === null) return
    event.preventDefault()
    event.stopPropagation()
    onSize(clampSize(next, min, max))
  }

  return {
    dragging,
    handleProps: {
      onPointerDown,
      onKeyDown,
      // A header cell sorts on click and a table row opens a drawer on click. The handle sits
      // inside both, so every click it sees stops there.
      onClick: (event) => event.stopPropagation(),
      onDoubleClick: (event) => {
        event.preventDefault()
        event.stopPropagation()
        onReset?.()
      },
      // THE THIRD DEFENCE against the native drag-to-reorder on the same header cell, after
      // `preventDefault` above and `draggable={false}` on the handle itself.
      onDragStart: (event) => {
        event.preventDefault()
        event.stopPropagation()
      },
    },
  }
}
