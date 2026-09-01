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
  const stats = payload.stats ?? []

  // A section's body is ALSO in `text`, because `extract.texts()` flattens the whole pane including
  // what is inside a collapsed block. Rendered naively every SQL query appears twice: once as a
  // wall of prose and again inside its own section.
  const claimed = new Set<string>()
  // A stat card's three strings are in `text` too, for the same flattening reason, so without this
  // the page prints "sessions / 1,325 / in the store" as body prose directly under the card.
  for (const stat of stats) {
    claimed.add(stat.label)
    claimed.add(stat.value)
    if (stat.sub) claimed.add(stat.sub)
  }
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

  // A SECTION IS ONLY A COLLAPSIBLE WHEN IT HOLDS PROSE. One that wraps a table, a chart or the
  // stat cards has an empty body, because `extract.texts()` reads prose and nothing else, so
  // rendering it as a collapsible drew a heading over nothing. Instead its title becomes the
  // heading of the thing it wraps, and a section wrapping the stat cards is dropped outright since
  // those cards are already at the top of the tab.
  const isText = (s: (typeof sections)[number]) => (s.wraps ?? 'text') === 'text'
  const textSections = sections.filter(isText)
  const titleFor = (wraps: 'table' | 'figure', index: number) =>
    sections.find((s) => s.wraps === wraps && s.wraps_index === index)?.summary

  const attached = (index: number) => textSections.filter((s) => s.table_index === index)
  const loose = textSections.filter(
    (s) => s.table_index === null || s.table_index < 0 || s.table_index >= payload.tables.length,
  )

  return (
    <div className="flex flex-col gap-5">

      {stats.length > 0 && (
        // THE NUMBERS FIRST. These are the figures the tab is about, and they arrived as
        // twenty-one loose strings in the middle of a wall of prose. A dashboard that opens with a
        // chart and a paragraph makes a reader hunt for the totals it already computed.
        <section
          className="grid gap-3"
          style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(11rem, 1fr))' }}
        >
          {stats.map((stat, index) => (
            // THE CAPTION IS SHOWN, not left to a hover. It used to be the tooltip only, on the
            // reasoning that printing it under every card turned a row of figures into a row of
            // paragraphs, and that reasoning is right about the LOOK and wrong about the risk: on
            // at least half of these the caption is what the number MEANS. "Peak Resident 999.8k"
            // is the largest single API call in the store and not any session's peak; "Sessions
            // 1,325" counts every session while only 317 are listed on All sessions; "API Calls"
            // exists to be told apart from the transcript row count in its own caption. A reader
            // cannot hover for a qualifier they have no reason to suspect is there.
            //
            // Small, muted and one line, so it reads as a caption rather than as a second figure.
            // The full text stays on the title for the few that run long.
            <div
              key={index}
              title={stat.sub || undefined}
              className="rounded-lg bg-panel px-4 py-3 shadow-panel"
            >
              <p className="text-2xs tracking-[0.06em] text-ink-faint">{stat.label}</p>
              <p className="mt-1 font-mono text-xl font-bold tabular text-ink">{stat.value}</p>
              {stat.sub && (
                <p className="mt-1 truncate text-2xs leading-snug text-ink-faint">{stat.sub}</p>
              )}
            </div>
          ))}
        </section>
      )}

      {figures.map((figure, index) => (
        <section key={index} className="rounded-lg bg-panel shadow-panel p-4">
          {(titleFor('figure', index) || payload.figures[index]?.title) && (
            <h3 className="mb-2 text-md font-semibold text-ink">
              {titleFor('figure', index) || payload.figures[index]?.title}
            </h3>
          )}
          <Plot figure={figure} />
        </section>
      ))}

      {prose.length > 0 && (
        <section className="rounded-lg bg-panel shadow-panel px-4 py-3">
          {prose.map((line, index) => (
            <p key={index} className="text-sm leading-relaxed text-ink-dim">
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
          {(titleFor('table', index) || meta[index]?.title) && (
            <h3 className="text-md font-semibold text-ink-dim">
              {titleFor('table', index) || meta[index]?.title}
            </h3>
          )}
          <DataTable
            table={table}
            meta={meta[index]}
            title={titleFor('table', index) || meta[index]?.title || undefined}
            onRowClick={onRowClick}
          />
          {attached(index).map((section, at) => (
            <Section key={at} section={section} table={table} />
          ))}
        </section>
      ))}

      {figures.length === 0 && payload.tables.length === 0 && (
        <div className="rounded-lg bg-panel shadow-panel p-8 text-center text-ink-dim">
          This tab produced nothing for the current selection.
        </div>
      )}
    </div>
  )
}
