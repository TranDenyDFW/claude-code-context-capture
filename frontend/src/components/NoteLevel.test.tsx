/**
 * A heading carries two different things, and they are drawn differently.
 *
 * `alert` is the levelled half and goes ON the page. `note` is the ordinary caption and stays the
 * hover. They used to be one joined string with one level, and that is the bug this pins: the
 * Session chart carries a warning ("nothing is selected, so this is the most recently active
 * session") AND a legend caption ("the shaded bands are the warn, compact and blocked zones"), and
 * under a single level the caption was drawn in warning amber too, a paragraph of alarm over an
 * explanation of some shaded bands.
 *
 * The other half matters as much: making every note visible would put a paragraph under each of
 * eight headings and bury the one that is actually urgent.
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Heading } from './TableHeading'

const ALERT = 'Nothing is selected in the header, so this is the most recently active session.'
const CAPTION = 'The shaded bands are the warn, compact and blocked zones for the model in use.'

describe('a heading with both an alert and a caption', () => {
  it('shows the alert on the page', () => {
    render(<Heading name="Session" note={CAPTION} alert={ALERT} level="warn" />)
    expect(screen.getByRole('note').textContent).toBe(ALERT)
  })

  it('keeps the caption as the hover, not on the page (gate can fail)', () => {
    // THE DEFECT. Joined, the caption rode the alert's level onto the page in amber.
    render(<Heading name="Session" note={CAPTION} alert={ALERT} level="warn" />)
    expect(screen.getByRole('note').textContent).not.toContain('shaded bands')
    expect(screen.getByRole('heading').getAttribute('title')).toBe(CAPTION)
  })

  it('does not repeat the alert in the hover', () => {
    // The same sentence in both places reads as two, and a screen reader announces it twice.
    render(<Heading name="Session" note={CAPTION} alert={ALERT} level="warn" />)
    expect(screen.getByRole('heading').getAttribute('title')).not.toContain('Nothing is selected')
  })
})

describe('a heading with only a caption', () => {
  it('is a hover, and nothing else (gate can fail)', () => {
    // THE OTHER HALF. Without this, rendering every note visibly passes the first test.
    render(<Heading name="Turns per session" note={CAPTION} />)
    expect(screen.queryByRole('note')).toBeNull()
    expect(screen.getByRole('heading').getAttribute('title')).toBe(CAPTION)
  })

  it('renders nothing extra for a level with no alert', () => {
    render(<Heading name="Session" note={CAPTION} level="warn" />)
    expect(screen.queryByRole('note')).toBeNull()
    expect(screen.getByRole('heading').getAttribute('title')).toBe(CAPTION)
  })

  it('renders nothing extra for an alert with no level', () => {
    // The server decides what is loud. An alert string alone must not promote itself.
    render(<Heading name="Session" note={CAPTION} alert={ALERT} />)
    expect(screen.queryByRole('note')).toBeNull()
  })
})

describe('the alert', () => {
  it('carries its level as data, so a level that is not warn can be styled later', () => {
    render(<Heading name="Session" note={null} alert={ALERT} level="warn" />)
    expect(screen.getByRole('note').getAttribute('data-note-level')).toBe('warn')
  })

  it('shows without a caption beside it', () => {
    render(<Heading name="Session" note={null} alert={ALERT} level="warn" />)
    expect(screen.getByRole('note').textContent).toBe(ALERT)
    expect(screen.getByRole('heading').getAttribute('title')).toBeNull()
  })
})
