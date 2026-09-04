/**
 * A chart is named and explained the same way a table is, and a collapsible label stays two halves.
 *
 * These are the two defects the user photographed. The Summary tab drew
 * "What to do about it 6 finding(s), each with an action" as a heading over a table whose own name
 * is "Findings", with no hover, because a section summary had one slot and the accordion's two
 * spans were joined into it. And every chart in the app was drawn with a bare `h3` and no note at
 * all, so ten captions across the tabs printed as paragraphs in the body.
 *
 * Asserted on the rendered heading rather than on the payload: the payload already carried enough
 * to draw both correctly before this, and the renderer was throwing it away.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { Pane } from './Pane'
import type { TabPayload } from '@/api'

vi.mock('./Plot', () => ({ Plot: () => <div data-testid="chart" /> }))

function payload(over: Partial<TabPayload> = {}): TabPayload {
  return {
    tab: 'tab-test', session: null, scope: 'main', cohort: null,
    tables: [], figures: [], text: [], plotly: [], ...over,
  }
}

const oneChart = {
  figures: [{ title: 'Context Window Over the Session', traces: [] }],
  plotly: [{ data: [], layout: {} }],
}

describe('a chart heading', () => {
  it('names the chart and puts its caption on the hover, behind the glyph', () => {
    render(<Pane payload={payload({
      ...oneChart,
      // `absorbed` is the licence to drop the line from the body, and it is deliberately not the
      // same field as `note`: the server states which exact `text` lines it folded in, so a note
      // it rewrote cannot silently delete a line it did not fold.
      figure_meta: [{ note: 'Session 1d3708a2, 1,822 turns.',
                      absorbed: ['Session 1d3708a2, 1,822 turns.'] }],
      text: ['Session 1d3708a2, 1,822 turns.'],
    })} />)
    const heading = screen.getByRole('heading', { name: /Context Window Over the Session/ })
    expect(heading.getAttribute('title')).toBe('Session 1d3708a2, 1,822 turns.')
    // No glyph: a bordered "?" beside every heading on the page reads as a control and is
    // not one. The note is the hover, and that is what has to be true.
    expect(within(heading).queryByText('?')).toBeNull()
    // And the caption is not ALSO printed in the body, which is the whole point of pairing it.
    expect(screen.queryAllByText('Session 1d3708a2, 1,822 turns.')).toHaveLength(0)
  })

  it('carries no glyph and no hover when the chart has no caption', () => {
    render(<Pane payload={payload(oneChart)} />)
    const heading = screen.getByRole('heading', { name: /Context Window Over the Session/ })
    expect(heading.getAttribute('title')).toBeNull()
    expect(within(heading).queryByText('?')).toBeNull()
  })

  it('shows a collapsible section title over the figure title, with its caption on the hover', () => {
    render(<Pane payload={payload({
      ...oneChart,
      details: [{
        summary: 'Where the tokens went', summary_note: 'cumulative resident by project, top 15',
        body: [], table_index: -1, wraps: 'figure', wraps_index: 0,
      }],
    })} />)
    const heading = screen.getByRole('heading', { name: /Where the tokens went/ })
    expect(heading.getAttribute('title')).toBe('cumulative resident by project, top 15')
  })
})

describe('a table heading built from a collapsible label', () => {
  it('keeps the title in the heading and the caption on the hover', () => {
    render(<Pane payload={payload({
      tables: [{ id: 'tbl-findings', columns: ['finding'], rows: [{ finding: 'a' }] }],
      meta: [{ id: 'tbl-findings', title: 'Findings', note: null, columns: [], filterable: false,
               page_size: null, absorbed: [] }],
      details: [{
        summary: 'What to do about it', summary_note: '6 finding(s), each with an action',
        body: [], table_index: 0, wraps: 'table', wraps_index: 0,
      }],
    })} />)
    const heading = screen.getByRole('heading', { name: /What to do about it/ })
    // The two halves used to arrive joined, so the heading read
    // "What to do about it 6 finding(s), each with an action1 row".
    expect(heading.textContent).not.toContain('each with an action')
    expect(heading.getAttribute('title')).toBe('6 finding(s), each with an action')
    expect(within(heading).queryByText('?')).toBeNull()
  })

  it('shows both halves when a table has a section caption AND a note of its own', () => {
    render(<Pane payload={payload({
      tables: [{ id: 't', columns: ['a'], rows: [{ a: 1 }] }],
      meta: [{ id: 't', title: 'T', note: 'One row per item.', columns: [], filterable: false,
               page_size: null, absorbed: [] }],
      details: [{
        summary: 'A section', summary_note: 'its caption',
        body: [], table_index: 0, wraps: 'table', wraps_index: 0,
      }],
    })} />)
    expect(screen.getByRole('heading', { name: /A section/ }).getAttribute('title'))
      .toBe('its caption\nOne row per item.')
  })
})

describe('what a view IS', () => {
  it('is claimed by the pane and never printed, because the tab hover carries it', () => {
    render(<Pane payload={payload({
      about: ['This tab describes the capture machinery and the window math.'],
      text: ['This tab describes the capture machinery and the window math.'],
    })} />)
    // It moved from a wall in the body, to an 826-character tooltip on a 61-pixel chip, to the
    // tab's own hover, which is the thing on screen that already answers what am I looking at.
    // Whatever the pane does with it, printing it here is what must not happen.
    expect(screen.queryByText(/capture machinery/)).toBeNull()
  })
})
