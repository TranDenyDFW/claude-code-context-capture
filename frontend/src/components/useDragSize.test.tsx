/**
 * The drag primitive, on its own.
 *
 * jsdom has PointerEvent and real coordinates, and has NO `setPointerCapture` and no layout. So
 * these drive the same code a browser runs, except for the capture call, which has its own case
 * below because an unguarded call throws in every test here.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { ResizeHandle } from './ResizeHandle'
import { clampSize } from './useDragSize'

function Harness({
  start = 200, min = 100, max = 400, direction = 1 as 1 | -1, axis = 'x' as 'x' | 'y',
  onReset, onDragChange, sizes,
}: {
  start?: number; min?: number; max?: number; direction?: 1 | -1; axis?: 'x' | 'y'
  onReset?: () => void; onDragChange?: (dragging: boolean) => void; sizes?: number[]
}) {
  const [size, setSize] = useState(start)
  return (
    <div onClick={() => sizes?.push(-1)}>
      <span data-testid="size">{size}</span>
      <ResizeHandle
        label="Resize the sidebar" size={size} min={min} max={max} axis={axis} direction={direction}
        onSize={(next) => { sizes?.push(next); setSize(next) }}
        onReset={onReset} onDragChange={onDragChange}
      />
    </div>
  )
}

const handle = () => screen.getByRole('separator', { name: 'Resize the sidebar' })
const size = () => Number(screen.getByTestId('size').textContent)

describe('a drag', () => {
  it('moves the size by how far the pointer moved, not to where it is', () => {
    render(<Harness start={200} />)
    fireEvent.pointerDown(handle(), { clientX: 1000, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 1060 })
    expect(size()).toBe(260)
    fireEvent.pointerUp(window, { clientX: 1060 })
  })

  it('runs in jsdom, which has no setPointerCapture (gate can fail)', () => {
    // THE GUARD THIS TEST EXISTS FOR. `event.currentTarget.setPointerCapture(...)` called
    // unconditionally throws a TypeError here and takes every drag test with it.
    expect('setPointerCapture' in HTMLElement.prototype).toBe(false)
    render(<Harness />)
    expect(() => fireEvent.pointerDown(handle(), { clientX: 10, pointerId: 1 })).not.toThrow()
  })

  it('captures the pointer where the browser has it', () => {
    render(<Harness />)
    const spy = vi.fn()
    Object.assign(handle(), { setPointerCapture: spy })
    fireEvent.pointerDown(handle(), { clientX: 10, pointerId: 7 })
    expect(spy).toHaveBeenCalledWith(7)
  })

  it('grows when pulled the other way for a right-hand panel', () => {
    render(<Harness start={300} max={600} direction={-1} />)
    fireEvent.pointerDown(handle(), { clientX: 900, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 820 })
    expect(size()).toBe(380)
  })

  it('stops at both bounds', () => {
    render(<Harness start={200} min={100} max={400} />)
    fireEvent.pointerDown(handle(), { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 5000 })
    expect(size()).toBe(400)
    fireEvent.pointerMove(window, { clientX: -5000 })
    expect(size()).toBe(100)
  })

  it('puts the size back on Escape, and stops listening', () => {
    const sizes: number[] = []
    render(<Harness start={200} sizes={sizes} />)
    fireEvent.pointerDown(handle(), { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 150 })
    expect(size()).toBe(350)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(size()).toBe(200)
    // A move after Escape is nobody's business: the drag is over.
    fireEvent.pointerMove(window, { clientX: 300 })
    expect(size()).toBe(200)
  })

  it('tells the caller when a drag starts and ends, so its own Escape can stand aside', () => {
    const said: boolean[] = []
    render(<Harness onDragChange={(on) => said.push(on)} />)
    fireEvent.pointerDown(handle(), { clientX: 0, pointerId: 1 })
    fireEvent.pointerUp(window, { clientX: 0 })
    expect(said).toEqual([true, false])
  })

  it('never reaches the click handler of whatever it sits inside', () => {
    // A header cell sorts on click; a row opens the drawer. Neither may fire from the handle.
    const sizes: number[] = []
    render(<Harness sizes={sizes} />)
    fireEvent.pointerDown(handle(), { clientX: 0, pointerId: 1 })
    fireEvent.click(handle())
    expect(sizes).not.toContain(-1)
  })

  it('refuses the native drag that reorders a column', () => {
    render(<Harness />)
    expect(handle().getAttribute('draggable')).toBe('false')
    const started = fireEvent.dragStart(handle())
    // fireEvent returns false when a handler called preventDefault.
    expect(started).toBe(false)
  })
})

describe('the keyboard', () => {
  it('moves by a step, by a bigger step with Shift, and to each bound', () => {
    render(<Harness start={200} min={100} max={400} />)
    fireEvent.keyDown(handle(), { key: 'ArrowRight' })
    expect(size()).toBe(216)
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    expect(size()).toBe(200)
    fireEvent.keyDown(handle(), { key: 'ArrowRight', shiftKey: true })
    expect(size()).toBe(264)
    fireEvent.keyDown(handle(), { key: 'Home' })
    expect(size()).toBe(100)
    fireEvent.keyDown(handle(), { key: 'End' })
    expect(size()).toBe(400)
  })

  it('reads the arrows along its own axis only', () => {
    render(<Harness start={200} />)
    fireEvent.keyDown(handle(), { key: 'ArrowUp' })
    fireEvent.keyDown(handle(), { key: 'ArrowDown' })
    expect(size()).toBe(200)
  })

  it('follows the panel it is on, so the arrow does what it looks like', () => {
    render(<Harness start={300} max={600} direction={-1} />)
    fireEvent.keyDown(handle(), { key: 'ArrowRight' })
    expect(size()).toBe(284)
  })
})

describe('the handle itself', () => {
  it('is a named separator a screen reader can place', () => {
    render(<Harness start={200} min={100} max={400} />)
    const bar = handle()
    expect(bar.getAttribute('aria-orientation')).toBe('vertical')
    expect(bar.getAttribute('tabindex')).toBe('0')
    expect(bar.getAttribute('aria-valuenow')).toBe('200')
    expect(bar.getAttribute('aria-valuemin')).toBe('100')
    expect(bar.getAttribute('aria-valuemax')).toBe('400')
    fireEvent.keyDown(bar, { key: 'ArrowRight' })
    expect(handle().getAttribute('aria-valuenow')).toBe('216')
  })

  it('says vertical for a horizontal drag, because the attribute describes the line', () => {
    render(<Harness axis="y" />)
    expect(handle().getAttribute('aria-orientation')).toBe('horizontal')
  })

  it('carries no text, because a column header is read for its exact text', () => {
    render(<Harness />)
    expect(handle().textContent).toBe('')
  })

  it('declares that a touch drag is not a scroll', () => {
    // jsdom cannot perform the gesture, so the declaration is what is pinned.
    render(<Harness />)
    expect(handle().className).toContain('touch-none')
  })

  it('resets on a double-click when the caller offers one', () => {
    const reset = vi.fn()
    render(<Harness onReset={reset} />)
    fireEvent.doubleClick(handle())
    expect(reset).toHaveBeenCalledTimes(1)
  })
})

describe('clampSize', () => {
  it('rounds to whole pixels and keeps the minimum when the bounds cross', () => {
    expect(clampSize(200.4, 100, 400)).toBe(200)
    expect(clampSize(50, 100, 400)).toBe(100)
    expect(clampSize(500, 100, 400)).toBe(400)
    // A narrow window can hand a max below the min. Unreadable beats invisible.
    expect(clampSize(300, 320, 200)).toBe(320)
  })
})
