/**
 * The plotly_click binding itself, which nothing covered: the pane's tests replace Plot with a
 * stub, so the wiring between Plotly's emitter and onPointClick was the one part of the chart
 * click that no test ran. Found by a review.
 *
 * Plotly is replaced by a fake that records what the component draws and hands back an emitter,
 * which is what the real library attaches to the node it drew into.
 */
import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'

const listeners: Record<string, ((event: unknown) => void)[]> = {}
const removed: string[] = []
const draws: { data: unknown[] }[] = []

vi.mock('plotly.js-dist-min', () => ({
  default: {
    react: (node: HTMLElement, data: unknown[]) => {
      draws.push({ data })
      const graph = node as HTMLElement & Record<string, unknown>
      graph.on = (event: string, handler: (e: unknown) => void) => {
        listeners[event] = [...(listeners[event] ?? []), handler]
      }
      graph.removeAllListeners = (event: string) => {
        removed.push(event)
        listeners[event] = []
      }
      return Promise.resolve(null)
    },
    purge: () => {},
  },
}))

import { Plot, type PlotPoint } from './Plot'

const figure = { data: [{ x: [1, 2], y: [3, 4] }], layout: {} }

async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0))
  await new Promise((resolve) => setTimeout(resolve, 0))
}

describe('the chart reports a click', () => {
  it('binds once per draw and reports the point Plotly resolved', async () => {
    const clicks: PlotPoint[] = []
    render(<Plot figure={figure} onPointClick={(point) => clicks.push(point)} />)
    await settle()
    expect(listeners.plotly_click?.length).toBe(1)
    expect(removed).toContain('plotly_click')
    listeners.plotly_click[0]({
      points: [{
        data: { name: 'resident' }, x: 158, y: 568477, customdata: ['s1'], pointIndex: 7,
      }],
    })
    expect(clicks).toHaveLength(1)
    expect(clicks[0]).toMatchObject({ traceName: 'resident', x: 158, y: 568477, pointIndex: 7 })
    expect(clicks[0].customdata).toEqual(['s1'])
  })

  it('reads the LATEST handler, so a re-render does not report to a closure that is gone', async () => {
    const first: PlotPoint[] = []
    const second: PlotPoint[] = []
    const { rerender } = render(<Plot figure={figure} onPointClick={(p) => first.push(p)} />)
    await settle()
    rerender(<Plot figure={figure} onPointClick={(p) => second.push(p)} />)
    await settle()
    listeners.plotly_click[listeners.plotly_click.length - 1]({
      points: [{ data: { name: 'x' }, x: 1, y: 2, pointIndex: 0 }],
    })
    expect(first).toHaveLength(0)
    expect(second).toHaveLength(1)
  })

  it('does nothing on a click when no handler was given (gate can fail)', async () => {
    render(<Plot figure={figure} />)
    await settle()
    const fire = () => listeners.plotly_click[listeners.plotly_click.length - 1]({
      points: [{ data: { name: 'x' }, x: 1, y: 2, pointIndex: 0 }],
    })
    expect(fire).not.toThrow()
  })

  it('falls back to pointNumber when Plotly reports that instead', async () => {
    const clicks: PlotPoint[] = []
    render(<Plot figure={figure} onPointClick={(point) => clicks.push(point)} />)
    await settle()
    listeners.plotly_click[listeners.plotly_click.length - 1]({
      points: [{ data: { name: 'bars' }, label: 'P:\\x', value: 12, pointNumber: 4 }],
    })
    expect(clicks[0]).toMatchObject({ traceName: 'bars', label: 'P:\\x', value: 12, pointIndex: 4 })
  })
})
