import type { TabPayload } from '@/api'
import { DataTable } from './DataTable'
import { describePoint } from './inspect'
import { Inspector } from './Inspector'
import { Plot, type PlotPoint } from './Plot'
import { Section } from './Section'
import { Heading, TableHeading, joinNotes, tableName } from './TableHeading'
import { useRowInspector } from './useRowInspector'

export function Pane({
  payload,
  onRowClick,
  onOpenTable,
  onOpenFigure,
  onOpenCompaction,
}: {
  payload: TabPayload
  onRowClick?: (row: Record<string, unknown>) => void
  /** Open table `index` of this tab in a window of its own; the toolbar shows the control. */
  onOpenTable?: (index: number, focus?: { query: string; filter: { key: string; value: string } | null }) => void
  /** Open chart `index` of this tab in a window of its own, for the same reason a table can be. */
  onOpenFigure?: (index: number) => void
  /** Open one compaction full width: a boundary is two documents and a drawer shows one. */
  onOpenCompaction?: (uuid: string) => void
}) {
  const figures = payload.plotly ?? []
  const sections = payload.details ?? []
  const meta = payload.meta ?? []
  const stats = payload.stats ?? []
  const figureMeta = payload.figure_meta ?? []
  const empties = payload.empty ?? []

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
    // The caption half of a collapsible's label. It reaches the reader on the heading's hover, so
    // printing it again in the body would be the same sentence twice, once where it means nothing.
    if (section.summary_note) claim(section.summary_note)
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
  // WHAT THIS VIEW IS. The header chip carries these; without the claim they are on the chip AND
  // still in the body, which is the wall this was meant to end, with one more copy of it.
  for (const line of payload.about ?? []) claim(line)
  // A panel that cannot be filled is drawn below as a placeholder, so its two lines are not prose.
  for (const panel of empties) {
    claim(panel.title)
    if (panel.note) claim(panel.note)
  }
  // A CHART'S CAPTION BELONGS TO THE CHART. The server pairs it and lists the exact lines it
  // folded in; without this every one of them printed as a paragraph in the body, far from the
  // chart it explains, which is how ten captions across seven tabs became a wall of prose.
  for (const entry of figureMeta) {
    for (const line of entry.absorbed ?? []) claim(line)
  }
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
  const wrapping = (wraps: 'table' | 'figure', index: number) =>
    sections.find((s) => s.wraps === wraps && s.wraps_index === index)
  const titleFor = (wraps: 'table' | 'figure', index: number) => wrapping(wraps, index)?.summary
  /** The caption half of the collapsible whose title became this heading, if there is one. */
  const noteFor = (wraps: 'table' | 'figure', index: number) =>
    wrapping(wraps, index)?.summary_note

  const attached = (index: number) => textSections.filter((s) => s.table_index === index)
  const names = payload.tables.map((_, index) => tableName(index, meta[index], titleFor('table', index)))
  // NO onSelectSession HERE, and it is not an omission. In the pane a table whose rows carry a
  // session key is navigable, so its click selects the session and this drawer never opens for
  // such a row; the only rows that do reach it carry no session to offer. The standalone page is
  // the opposite case, because it passes no row click, and it does pass the handler.
  const reader = useRowInspector(payload, names, undefined, onOpenCompaction)

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
            // THE CAPTION IS THE HOVER. It was shown under every figure on the reasoning that
            // half of these captions are what the number MEANS, and that is still true; what
            // changed is that a row of eight cards each carrying a wrapped second line reads as a
            // row of paragraphs rather than a row of figures, and the captions were being cut off
            // by the card width anyway, so half of each one already only existed on the hover.
            <div
              key={index}
              title={stat.sub || undefined}
              aria-description={stat.sub || undefined}
              className="rounded-lg bg-panel px-4 py-3 shadow-panel"
            >
              <p className="text-2xs tracking-[0.06em] text-ink-faint">{stat.label}</p>
              <p className="mt-1 font-mono text-xl font-bold tabular text-ink">{stat.value}</p>
            </div>
          ))}
        </section>
      )}

      {figures.map((figure, index) => {
        // EVERY CHART GETS THE HEADING A TABLE GETS: its name, and its explanation on hover behind
        // the same glyph. The name is the collapsible's title where one wraps the chart and the
        // figure's own title otherwise; the note is whatever the server paired to it, plus the
        // caption half of that collapsible's label.
        const name = titleFor('figure', index) || payload.figures[index]?.title || ''
        const note = joinNotes(noteFor('figure', index), figureMeta[index]?.note)
        return (
          <section key={index} className="rounded-lg bg-panel shadow-panel p-4">
            <div className="mb-2 flex items-baseline justify-between gap-3">
              {name ? <Heading name={name} note={note} /> : <span />}
              {/* THE SAME AFFORDANCE A TABLE HAS. A chart on a dashboard is drawn at the height
                  the panel allows; one somebody is reading wants the window, and the address
                  carries the tab, the selection and the index, so it is also a link. */}
              {onOpenFigure && (
                <button
                  onClick={() => onOpenFigure(index)}
                  className="shrink-0 rounded border border-edge px-2 py-0.5 text-2xs text-ink-dim
                             hover:text-ink"
                >
                  Window
                </button>
              )}
            </div>
            <Plot figure={figure} onPointClick={inspectPoint(index)} />
          </section>
        )
      })}

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

      {/* WHERE A TABLE WOULD HAVE BEEN. Not prose and not a hover: on the Cost tab with a session
          selected this is the only thing that tells a reader the difference between "there are
          none" and "this question cannot be asked from the selection you are in", and only one of
          those is true. Drawn dimmer than a real panel and with a dashed edge, so it reads as an
          absence rather than as content. */}
      {empties.map((panel, index) => (
        <section
          key={`empty-${index}`}
          className="rounded-lg border border-dashed border-edge px-4 py-3"
        >
          <h3 className="text-md font-semibold text-ink-dim">{panel.title}</h3>
          {panel.note && (
            <p className="mt-1 text-sm leading-relaxed text-ink-faint">{panel.note}</p>
          )}
        </section>
      ))}

      {payload.tables.map((table, index) => {
        // EVERY TABLE HAS THE SAME HEADING: its name, the row count, and its note on hover. The
        // name comes from the server, which pairs the heading it wrote above the table; the last
        // resort is a numbered one, so no table is ever the only unnamed thing on a page. The note
        // is on the heading, not in the body, and the glyph says there is something to hover.
        const name = tableName(index, meta[index], titleFor('table', index))
        const note = joinNotes(noteFor('table', index), meta[index]?.note)
        return (
          // Keyed by INDEX, not by id. Five of the Cost tab's six tables report the id
          // `(anonymous)`, so keying on it collided four times and React warned on every render.
          <section key={index} className="flex flex-col gap-2">
            <TableHeading name={name} note={note} />
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

      {/* GENUINELY BLANK, not merely "no chart and no table".

          The banner fired whenever tables and figures were both empty, which is true of several
          panes that DID produce something a reader can use: the stat cards, the written empty
          states, and the prose a tab shows when it has a real answer that is not a table. Worst of
          all it fired UNDER the apology panel a raised tab renders, so a crashed tab said both
          "could not be rendered" and "produced nothing", and the second sentence contradicted the
          first by implying there was simply nothing to show.

          Every local here already existed above; the gate was just narrower than the sentence. */}
      {figures.length === 0 && payload.tables.length === 0 && prose.length === 0
        && stats.length === 0 && empties.length === 0 && loose.length === 0 && (
        <div className="rounded-lg bg-panel shadow-panel p-8 text-center text-ink-dim">
          This tab produced nothing for the current selection.
        </div>
      )}

      {reader.content && <Inspector content={reader.content} onClose={reader.close} />}
    </div>
  )
}
