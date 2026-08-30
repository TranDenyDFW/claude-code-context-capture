import { useEffect, useRef } from 'react'
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

let pending: Promise<PlotlyModule> | null = null

function plotly(): Promise<PlotlyModule> {
  // One shared promise, so eight charts on one tab trigger one download rather than eight.
  pending ??= import('plotly.js-dist-min').then((module) => (module.default ?? module) as PlotlyModule)
  return pending
}

export function Plot({ figure, height = 380 }: { figure: PlotlyFigure; height?: number }) {
  const holder = useRef<HTMLDivElement>(null)

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
    void plotly().then((Plotly) => {
      // The tab may have changed while the library was downloading. Drawing into a node React has
      // already unmounted throws inside Plotly, where the stack names none of this.
      if (dropped || !holder.current) return
      return Plotly.react(node, figure.data ?? [], layout, {
        displayModeBar: false,
        responsive: true,
        // Plotly's own scroll zoom fights the page: a reader scrolling past a chart zooms it.
        scrollZoom: false,
      })
    })
    return () => {
      dropped = true
      void plotly().then((Plotly) => Plotly.purge(node))
    }
  }, [figure, height])

  return <div ref={holder} className="w-full" style={{ height }} />
}
