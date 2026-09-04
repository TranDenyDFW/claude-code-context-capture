import type { TableMeta } from '@/api'

/**
 * The one heading anything on a page gets: its name, its row count when it has rows, and its note
 * on hover.
 *
 * Shared by the pane, the single-table page and every chart, so the three can never drift into
 * three ways of naming the thing above them. The note is on the heading, never in the body.
 *
 * NO GLYPH AND NO ROW COUNT. The glyph was a bordered "?" saying there was something to hover, and
 * it sat beside every heading on the page as a small grey box that reads as a control and is not
 * one. The row count was already stated by the table itself, at the bottom, where the pager says
 * "1 to 12 of 200"; a heading saying "200 rows" over a pager saying the same is the number twice.
 *
 * CHARTS COME THROUGH HERE TOO, and that is the point. A chart had no note field at all, so ten
 * captions across the tabs printed as loose paragraphs and one chart carried its explanation IN
 * its title: "1d3708a2 | 1822 turns | peak 995.6k | 1 compactions (red) | 5 model segments" was a
 * note wearing a title's slot, because a note had nowhere else to go.
 */
export function tableName(
  index: number, meta: TableMeta | undefined, sectionTitle: string | undefined,
): string {
  return sectionTitle || meta?.title || `Table ${index + 1}`
}

/** Two notes for one heading, in the order a reader should meet them, or null for neither. */
export function joinNotes(...notes: (string | null | undefined)[]): string | null {
  const kept = notes.filter((note): note is string => Boolean(note && note.trim()))
  return kept.length ? kept.join('\n') : null
}

export function Heading({
  name,
  note,
  as: Tag = 'h3',
}: {
  name: string
  note: string | null
  as?: 'h1' | 'h2' | 'h3'
}) {
  return (
    <Tag
      className="text-md font-semibold text-ink-dim"
      title={note ?? undefined}
      aria-description={note ?? undefined}
    >
      {name}
    </Tag>
  )
}

export function TableHeading({
  name,
  note,
  as,
}: {
  name: string
  note: string | null
  as?: 'h1' | 'h2' | 'h3'
}) {
  return <Heading name={name} note={note} as={as} />
}
