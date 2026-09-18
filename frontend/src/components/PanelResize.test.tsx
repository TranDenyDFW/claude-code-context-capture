/**
 * The right-hand drawer can be made wider, and Escape means two things depending on what is
 * happening: cancel the drag, or close the drawer.
 *
 * The sidebar's half of this lives in `Sidebar.test.tsx`, where its harness already is.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Inspector, drawerCeiling } from './Inspector'

const content = { title: 'Messages: row', fields: [['ts', '10:00']] as [string, string][] }

function show(onClose = vi.fn()) {
  const view = render(<Inspector content={content} onClose={onClose} />)
  return { ...view, onClose }
}

const bar = () => screen.getByRole('separator', { name: 'Resize the details panel' })
const drawer = () => screen.getByRole('dialog', { name: 'Messages: row' }) as HTMLElement

beforeEach(() => {
  try {
    localStorage.clear()
  } catch { /* storage is not required for the drawer to work */ }
})

describe('the details panel', () => {
  it('opens at the width it has always had', () => {
    show()
    expect(drawer().style.width).toBe('576px')
  })

  it('grows when its left edge is pulled left', () => {
    // THE DIRECTION THAT READS RIGHT. The handle is on the left edge of a right-hand panel, so
    // moving left must make it bigger; the naive sign makes it shrink as it is pulled open.
    show()
    fireEvent.pointerDown(bar(), { clientX: 900, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 820 })
    fireEvent.pointerUp(window, { clientX: 820 })
    expect(drawer().style.width).toBe('656px')
  })

  it('keeps the full-bleed rule a narrow screen needs', () => {
    // jsdom computes no layout, so the declaration is what can be pinned here.
    show()
    expect(drawer().className).toContain('max-w-full')
  })

  it('always leaves a strip of the page beside it', () => {
    expect(drawerCeiling(1200)).toBe(1040)
    expect(drawerCeiling(4000)).toBe(1400)
    // Narrower than the floor plus the strip: the floor wins, and max-w-full does the rest.
    expect(drawerCeiling(375)).toBe(320)
  })

  it('remembers the width', () => {
    const { unmount } = show()
    fireEvent.pointerDown(bar(), { clientX: 900, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 800 })
    fireEvent.pointerUp(window, { clientX: 800 })
    expect(localStorage.getItem('c4x.inspector.width')).toBe('676')
    unmount()
    show()
    expect(drawer().style.width).toBe('676px')
  })

  it('closes on Escape when nothing is being dragged', () => {
    const { onClose } = show()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('cancels the drag on Escape and stays open (gate can fail)', () => {
    // WITHOUT THE FLAG this closes the drawer and throws the width away, because both listeners
    // sit on `window` and the drawer's was bound first.
    const { onClose } = show()
    fireEvent.pointerDown(bar(), { clientX: 900, pointerId: 1 })
    fireEvent.pointerMove(window, { clientX: 700 })
    expect(drawer().style.width).toBe('776px')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    expect(drawer().style.width).toBe('576px')
    // And Escape closes it again once the drag is over.
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
