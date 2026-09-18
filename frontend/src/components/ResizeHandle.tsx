import { useDragSize } from './useDragSize'

/**
 * The thing you drag: one element, no text, keyboard-operable.
 *
 * `role="separator"` with a `tabIndex` is the ARIA widget for a divider that can be moved, which
 * is why it carries `aria-valuenow`, `aria-valuemin` and `aria-valuemax`: a focusable separator
 * without them is an axe violation, and a reader needs to know where the divider is.
 *
 * `aria-orientation` DESCRIBES THE LINE, NOT THE DRAG. A divider you pull left and right is drawn
 * as a vertical line, so a horizontal drag is `aria-orientation="vertical"`. The role's default is
 * horizontal, so it is written out every time.
 *
 * NO TEXT CONTENT, EVER. The Adopt window's test reads the exact text of every column header
 * (`AdoptSessions.test.tsx`), and a header holding a handle would gain whatever glyph it carried.
 * The handle is named by `aria-label` and drawn with a background, never with a character.
 *
 * `touch-none` is not decoration either: the table scrolls in both directions, so without it a
 * touch drag scrolls the table instead of moving the divider.
 */
export function ResizeHandle({
  label,
  size,
  min,
  max,
  onSize,
  onReset,
  onDragChange,
  axis = 'x',
  direction = 1,
  className,
}: {
  /** The accessible name, e.g. "Resize the Date column". */
  label: string
  size: number
  min: number
  max: number
  onSize: (next: number) => void
  /** Double-click. Omit and a double-click does nothing. */
  onReset?: () => void
  onDragChange?: (dragging: boolean) => void
  axis?: 'x' | 'y'
  direction?: 1 | -1
  /** Placement only: where the handle sits. Never its appearance. */
  className?: string
}) {
  const { dragging, handleProps } = useDragSize({
    size, min, max, axis, direction, onSize, onReset, onDragChange,
  })
  return (
    <span
      role="separator"
      aria-orientation={axis === 'x' ? 'vertical' : 'horizontal'}
      aria-label={label}
      aria-valuenow={size}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuetext={`${size} pixels`}
      tabIndex={0}
      draggable={false}
      {...handleProps}
      className={
        `touch-none select-none transition-colors ${axis === 'x' ? 'cursor-col-resize' : 'cursor-row-resize'} ` +
        `${dragging ? 'bg-accent' : 'hover:bg-accent/40 focus-visible:bg-accent/40'} ` +
        `${className ?? ''}`
      }
    />
  )
}
