import { useEffect, useRef, useState } from 'react'
import type { PlotlyFigure } from '@/api'

/**
 * A chart, drawn from the figure the server already built.
 *
 * WHY PLOTLY AND NOT RECHARTS, which the migration plan named. The API returns every chart as full
 * Plotly JSON because the Python side builds them with Plotly, so rendering them here is exact and
 * free. Rebuilding them in another library would mean re-deriving anomaly bands, per-segment zones,
 * compaction markers, budget lines, treemaps and heat-shaded cells by hand, which is both the
 * largest single piece of work in the whole migration and the one most likely to end up subtly
 * different from the thing it replaced. The stated constraint was "mostly looks"; the looks being
 * changed are the shell, the tables and the layout, and silently redrawing every chart in a
 * different library would change far more than that while claiming to change less.
 *
 * LOADED ON DEMAND. Plotly is 3.5 MB of the 4.3 MB bundle, and a static import means the page shows
 * nothing until all of it has arrived. Imported here instead, it downloads while the first tab
 * request is in flight, which is between 0.5 and 1.6 seconds of otherwise idle time, and a tab with
 * no charts never pays for it at all.
 *
 * The layout is overridden, not replaced: the server's traces and axes are kept exactly, and only
 * the page-level chrome (paper colour, font, margins) is brought into this design.
 */

type PlotlyModule = {
  react: (node: HTMLElement, data: unknown[], layout: unknown, config: unknown) => Promise<unknown>
  purge: (node: HTMLElement) => void
}

/** What a click on a chart reports: the point Plotly resolved, reduced to what a reader can use. */
export interface PlotPoint {
  traceName: string | null
  x: unknown
  y: unknown
  text?: unknown
  customdata?: unknown
  /** Treemap and pie cells name themselves by label rather than by x. */
  label?: unknown
  value?: unknown
  pointIndex: number
}

type PlotlyEvent = {
  points?: {
    data?: { name?: string }
    x?: unknown; y?: unknown; text?: unknown; customdata?: unknown; label?: unknown; value?: unknown
    pointIndex?: number; pointNumber?: number
  }[]
}

/** The div Plotly draws into gains an event emitter once drawn. */
type GraphDiv = HTMLElement & {
  on?: (event: string, handler: (event: PlotlyEvent) => void) => void
  removeAllListeners?: (event: string) => void
}

let pending: Promise<PlotlyModule> | null = null

function plotly(): Promise<PlotlyModule> {
  // One shared promise, so eight charts on one tab trigger one download rather than eight.
  pending ??= import('plotly.js-dist-min').then((module) => (module.default ?? module) as PlotlyModule)
  return pending
}

export function Plot({
  figure,
  height = 380,
  onPointClick,
}: {
  figure: PlotlyFigure
  height?: number
  /** A click on a point, bar or cell. Absent, the chart is hover-only as before. */
  onPointClick?: (point: PlotPoint) => void
}) {
  const holder = useRef<HTMLDivElement>(null)
  // The latest handler, read at click time, so a re-render with a new closure does not rebind the
  // chart's listeners or, worse, leave the old closure attached.
  const clickHandler = useRef(onPointClick)
  clickHandler.current = onPointClick
  // A chunk that fails to download left every chart on the page blank and silent, with nothing
  // in the DOM or the console naming the cause. Found by a review sweep; pre-existing.
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    const node = holder.current
    if (!node) return
    let dropped = false
    const layout = {
      ...(figure.layout ?? {}),
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: '#8b949e', family: '-apple-system, Segoe UI, sans-serif', size: 11 },
      margin: { l: 56, r: 20, t: 34, b: 44 },
      height,
      // The server's figures carry their own titles and the panel already shows one above the
      // chart. Two titles, one of them larger than the section it sits in, reads as a mistake.
      title: undefined,
      legend: { orientation: 'h' as const, y: -0.18, font: { size: 10 } },
    }
    plotly().then((Plotly) => {
      // The tab may have changed while the library was downloading. Drawing into a node React has
      // already unmounted throws inside Plotly, where the stack names none of this.
      if (dropped || !holder.current) return
      return Plotly.react(node, figure.data ?? [], layout, {
        displayModeBar: false,
        responsive: true,
        // Plotly's own scroll zoom fights the page: a reader scrolling past a chart zooms it.
        scrollZoom: false,
      }).then(() => {
        // Bound once per draw, on the emitter Plotly attaches to the node it drew into. A
        // re-draw of the same node replaces the listener rather than stacking a second one.
        const graph = node as GraphDiv
        graph.removeAllListeners?.('plotly_click')
        graph.on?.('plotly_click', (event) => {
          const point = event.points?.[0]
          const handler = clickHandler.current
          if (!point || !handler) return
          handler({
            traceName: point.data?.name ?? null,
            x: point.x, y: point.y, text: point.text, customdata: point.customdata,
            label: point.label, value: point.value,
            pointIndex: point.pointIndex ?? point.pointNumber ?? 0,
          })
        })
      })
    }).catch((error: unknown) => {
      // Forget the failed download so the next chart, or a reload of this one, tries again
      // rather than inheriting a rejected promise for the life of the page.
      pending = null
      if (!dropped) setFailed(error instanceof Error ? error.message : String(error))
    })
    return () => {
      dropped = true
      void plotly().then((Plotly) => Plotly.purge(node)).catch(() => { /* never loaded */ })
    }
  }, [figure, height])

  if (failed) {
    return (
      <div role="alert" className="flex w-full items-center justify-center text-sm text-ink-dim"
           style={{ height }}>
        The chart library could not be loaded ({failed}). Reload the page to try again.
      </div>
    )
  }
  return (
    <div
      ref={holder}
      className={`w-full ${onPointClick ? 'cursor-pointer' : ''}`}
      style={{ height }}
      title={onPointClick ? 'Click a point, bar or cell to see the data behind it' : undefined}
    />
  )
}
