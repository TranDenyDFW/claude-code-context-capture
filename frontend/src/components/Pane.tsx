import type { TabPayload } from '@/api'
import { DataTable } from './DataTable'
import { Plot } from './Plot'
import { Section } from './Section'

/**
 * A whole tab, drawn from the payload, with no per-tab code.
 *
 * This is the payoff from making `extract.describe()` the API contract rather than inventing a new
 * one per page. Every tab in this app is some tables, some charts and some prose, and the server
 * says which. So ONE renderer covers all eight, and per-tab work is only needed where a tab has an
 * interaction rather than merely content.
 *
 * The order is: what this tab describes, then the shape, then the caveats, then the numbers.
 */
export function Pane({
  payload,
  onRowClick,
}: {
  payload: TabPayload
  onRowClick?: (row: Record<string, unknown>) => void
}) {
  const figures = payload.plotly ?? []
  const sections = payload.details ?? []
  const meta = payload.meta ?? []

  // A section's body is ALSO in `text`, because `extract.texts()` flattens the whole pane including
  // what is inside a collapsed block. Rendered naively every SQL query appears twice: once as a
  // wall of prose and again inside its own section.
  const claimed = new Set<string>()
  for (const section of sections) {
    for (const line of section.body) claimed.add(line)
    if (section.summary) claimed.add(section.summary)
  }
  const prose = payload.text.filter(
    (line) =>
      line !== payload.population &&
      !claimed.has(line) &&
      !sections.some((s) => s.summary.includes(line)),
  )

  const attached = (index: number) => sections.filter((s) => s.table_index === index)
  const loose = sections.filter(
    (s) => s.table_index === null || s.table_index < 0 || s.table_index >= payload.tables.length,
  )

  return (
    <div className="flex flex-col gap-5">
      {/*
        WHICH POPULATION THIS TAB DESCRIBES, first and unmistakable.

        The app has always produced this sentence and it always reached the page, as prose line 1
        of up to 27 identical grey lines. So on Diagnostics, "Store-wide. Not affected by the
        header selection." sat far above the table being looked at, and the honest answer to "why
        do these values never change?" was on screen and unfindable. The data was right; the page
        was not saying so where the question gets asked.

        An unscoped tab is marked differently from a scoped one, because "your selection does
        nothing here" is the more surprising of the two and the one worth colouring.
      */}
      {payload.population && (
        <div
          data-scoped={payload.scoped ? 'true' : 'false'}
          className={`rounded-lg border px-4 py-2.5 text-[12.5px] ${
            payload.scoped
              ? 'border-edge bg-panel text-ink-dim'
              : 'border-warn/40 bg-warn/5 text-warn'
          }`}
        >
          {payload.population}
        </div>
      )}

      {figures.map((figure, index) => (
        <section key={index} className="rounded-lg border border-edge bg-panel p-4">
          {payload.figures[index]?.title && (
            <h3 className="mb-2 text-[13px] font-semibold text-ink">
              {payload.figures[index].title}
            </h3>
          )}
          <Plot figure={figure} />
        </section>
      ))}

      {prose.length > 0 && (
        <section className="rounded-lg border border-edge bg-panel px-4 py-3">
          {prose.map((line, index) => (
            <p key={index} className="text-[12.5px] leading-relaxed text-ink-dim">
              {line}
            </p>
          ))}
        </section>
      )}

      {loose.map((section, index) => (
        <Section key={`loose-${index}`} section={section} />
      ))}

      {payload.tables.map((table, index) => (
        // Keyed by INDEX, not by id. Five of the Cost tab's six tables report the id
        // `(anonymous)`, so keying on it collided four times and React warned on every render.
        <section key={index} className="flex flex-col gap-2">
          {/* The heading comes from `table_label()` on the server. Deriving it here by stripping a
              `tbl-` prefix was a naming rule living in the browser, which is the mistake this
              whole pass exists to undo. */}
          {meta[index]?.title && (
            <h3 className="text-[13px] font-semibold text-ink-dim">{meta[index].title}</h3>
          )}
          <DataTable table={table} meta={meta[index]} onRowClick={onRowClick} />
          {attached(index).map((section, at) => (
            <Section key={at} section={section} table={table} />
          ))}
        </section>
      ))}

      {figures.length === 0 && payload.tables.length === 0 && (
        <div className="rounded-lg border border-edge bg-panel p-8 text-center text-ink-dim">
          This tab produced nothing for the current selection.
        </div>
      )}
    </div>
  )
}
