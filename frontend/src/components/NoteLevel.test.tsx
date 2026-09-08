/**
 * A note the server marked as a warning is on the page; every other note stays a hover.
 *
 * The Session tab has no all-sessions mode: with nothing selected it falls back to the most recently
 * active session in the population. It says so, in a warning, and that warning went into a `title`
 * attribute while the header control kept reading "All sessions (22 listed)". The page contradicted
 * its own control and the reconciliation was behind a mouse.
 *
 * The other half matters as much. Making EVERY caption visible would put a paragraph under each of
 * eight headings and bury the one that matters, which is the same failure in the other direction:
 * this warning already spent a life as the first of five loose paragraphs at the top of the tab.
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Heading } from './TableHeading'

const WARNING = 'Nothing is selected in the header, so this is the most recently active session.'

describe('a heading note', () => {
  it('is shown on the page when the server gave it a level', () => {
    render(<Heading name="Session" note={WARNING} level="warn" />)
    expect(screen.getByRole('note').textContent).toBe(WARNING)
  })

  it('is a hover, and nothing else, when it has no level (gate can fail)', () => {
    // THE OTHER HALF. Without this, rendering every note visibly passes the test above.
    render(<Heading name="Turns per session" note="How many turns each session took." />)
    expect(screen.queryByRole('note')).toBeNull()
    expect(screen.getByRole('heading').getAttribute('title'))
      .toBe('How many turns each session took.')
  })

  it('does not say a levelled note twice', () => {
    // The same sentence as visible text AND as the hover reads as two statements, and a screen
    // reader announces it twice.
    render(<Heading name="Session" note={WARNING} level="warn" />)
    expect(screen.getByRole('heading').getAttribute('title')).toBeNull()
  })

  it('carries the level as data, so a level that is not warn can be styled later', () => {
    render(<Heading name="Session" note={WARNING} level="warn" />)
    expect(screen.getByRole('note').getAttribute('data-note-level')).toBe('warn')
  })

  it('renders nothing extra for a level with no note', () => {
    render(<Heading name="Session" note={null} level="warn" />)
    expect(screen.queryByRole('note')).toBeNull()
    expect(screen.getByRole('heading').textContent).toBe('Session')
  })
})
