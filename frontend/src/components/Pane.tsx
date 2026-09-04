import type { TabPayload } from '@/api'
import { DataTable } from './DataTable'
import { describePoint } from './inspect'
import { Inspector } from './Inspector'
import { Plot, type PlotPoint } from './Plot'
import { Section } from './Section'
import { TableHeading, tableName } from './TableHeading'
import { useRowInspector } from './useRowInspector'

export function Pane({
  payload,
  onRowClick,
  onOpenTable,
}: {
  payload: TabPayload
  onRowClick?: (row: Record<string, unknown>) => void
  /** Open table `index` of this tab in a window of its own; the toolbar shows the control. */
  onOpenTable?: (index: number, focus?: { query: string; filter: { key: string; value: string } | null }) => void
}) {
  const figures = payload.plotly ?? []
  const sections = payload.details ?? []
  const meta = payload.meta ?? []
  const stats = payload.stats ?? []

  // A section's body is ALSO in `text`, because `extract.texts()` flattens the whole pane including
  // what is inside a collapsed block. Rendered naively every SQL query appears twice: once as a
  // wall of prose and again inside its own section.
  // CASE-INSENSITIVE. The server title-cases a card's label ("Re-read Groups") and `text` keeps
  // the raw one ("Re-read groups"), so an exact match claimed nothing and every card's label was
  // printed again as prose. Measured on the Cost tab: nineteen leaked lines, five of them labels.
  const claimed = new Set<string>()
  const claim = (line: string) => claimed.add(line.trim().toLowerCase())
  const isClaimed = (line: string) => claimed.has(line.trim().toLowerCase())
  // A stat card's three strings are in `text` too, for the same flattening reason, so without this
  // the page prints "sessions / 1,325 / in the store" as body prose directly under the card.
  for (const stat of stats) {
    claim(stat.label)
    claim(stat.value)
    if (stat.sub) claim(stat.sub)
  }
  for (const section of sections) {
    for (const line of section.body) claim(line)
    if (section.summary) claim(section.summary)
  }
  // A TABLE'S HEADING AND NOTE BELONG TO THE TABLE. The server pairs them and lists the exact
  // lines it folded in; printing them here as well put six headings and their notes on the
  // Diagnostics tab in one block above the first table, with nothing saying which was whose.
  for (const entry of meta) {
    for (const line of entry.absorbed ?? []) claim(line)
  }
  // The labels of controls this page does not draw. The window calculator is Dash-only; its
  // "Resident tokens", "Window" and constants sentence read as orphaned prose here.
  for (const line of payload.dash_only ?? []) claim(line)
  const prose = payload.text.filter(
    (line) =>
      line !== payload.population &&
      !isClaimed(line) &&
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
  const names = payload.tables.map((_, index) => tableName(index, meta[index], titleFor('table', index)))
  // NO onSelectSession HERE, and it is not an omission. In the pane a table whose rows carry a
  // session key is navigable, so its click selects the session and this drawer never opens for
  // such a row; the only rows that do reach it carry no session to offer. The standalone page is
  // the opposite case, because it passes no row click, and it does pass the handler.
  const reader = useRowInspector(payload, names)

  /** A click on a chart: the point's fields, the rows behind it, and a way to open those rows. */
  const inspectPoint = (figureIndex: number) => (point: PlotPoint) => {
    const figureTitle = titleFor('figure', figureIndex) || payload.figures[figureIndex]?.title || null
    const { content, tableIndex, filter, query } = describePoint(
      point, figureTitle, payload.tables, meta, names)
    reader.showPoint({
      ...content,
      // The link carries the EXACT filter when the value lives in a key column, since a text
      // search over the columns a page draws would never find a hidden session id; otherwise the
      // visible value, which is what a reader would have typed.
      onOpen: onOpenTable && tableIndex !== null
        ? () => onOpenTable(tableIndex, { query: query ?? '', filter })
        : null,
    })
  }
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
          <Plot figure={figure} onPointClick={inspectPoint(index)} />
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

      {payload.tables.map((table, index) => {
        // EVERY TABLE HAS THE SAME HEADING: its name, the row count, and its note on hover. The
        // name comes from the server, which pairs the heading it wrote above the table; the last
        // resort is a numbered one, so no table is ever the only unnamed thing on a page. The note
        // is on the heading, not in the body, and the glyph says there is something to hover.
        const name = tableName(index, meta[index], titleFor('table', index))
        const note = meta[index]?.note ?? null
        return (
          // Keyed by INDEX, not by id. Five of the Cost tab's six tables report the id
          // `(anonymous)`, so keying on it collided four times and React warned on every render.
          <section key={index} className="flex flex-col gap-2">
            <TableHeading name={name} table={table} note={note} />
            <DataTable
              table={table}
              meta={meta[index]}
              title={name}
              onRowClick={onRowClick}
              onOpenWindow={onOpenTable ? () => onOpenTable(index) : undefined}
              onOpenRow={reader.openRow(index)}
            />
            {attached(index).map((section, at) => (
              <Section key={at} section={section} table={table} meta={meta[index]} />
            ))}
          </section>
        )
      })}

      {figures.length === 0 && payload.tables.length === 0 && (
        <div className="rounded-lg bg-panel shadow-panel p-8 text-center text-ink-dim">
          This tab produced nothing for the current selection.
        </div>
      )}

      {reader.content && <Inspector content={reader.content} onClose={reader.close} />}
    </div>
  )
}
