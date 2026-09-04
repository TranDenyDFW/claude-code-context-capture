import { useEffect } from 'react'
import type { TabPayload } from '@/api'
import { Heading, joinNotes } from './TableHeading'
import { Plot } from './Plot'

/**
 * One chart, full width, in a window of its own.
 *
 * The same reason the single-table page exists. A chart on a dashboard is one panel among eight
 * and is drawn at the height the panel allows; a chart somebody is actually reading wants the
 * window. And because the address carries the tab, the selection and the chart index, the result
 * is a link: what you are looking at is what the other person opens.
 *
 * It does NOT carry the row-inspector. A click on a point opens the rows behind it, and the rows
 * behind it live on the dashboard beside the table they came from; sending a reader from a chart
 * in its own window into a drawer over that window would be a third place to be, reached by
 * accident. The chart is the subject here.
 */
export function FigurePage({
  payload,
  index,
  onBack,
}: {
  payload: TabPayload
  index: number
  onBack: () => void
}) {
  const figure = (payload.plotly ?? [])[index]
  const name = payload.figures?.[index]?.title ?? ''
  const note = joinNotes(payload.figure_meta?.[index]?.note)

  useEffect(() => {
    const before = document.title
    document.title = `${name || 'Chart'} · C4X`
    return () => { document.title = before }
  }, [name])

  if (!figure) {
    return (
      <main className="mx-auto w-full max-w-[1600px] px-6 py-5">
        <p role="alert" className="text-sm text-ink-dim">
          This tab has no chart {index + 1}; it has {(payload.plotly ?? []).length}.
        </p>
        <button onClick={onBack} className="mt-3 text-sm text-accent hover:underline">
          Back to the dashboard
        </button>
      </main>
    )
  }

  return (
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-3 px-6 py-5">
      <div className="flex items-baseline justify-between gap-4">
        <Heading name={name || `Chart ${index + 1}`} note={note} as="h1" />
        <button onClick={onBack} className="shrink-0 text-sm text-accent hover:underline">
          Back to the dashboard
        </button>
      </div>
      <section className="rounded-lg bg-panel p-4 shadow-panel">
        {/* Taller than the dashboard draws it, which is the point of being here. */}
        <Plot figure={figure} height={640} />
      </section>
    </main>
  )
}
