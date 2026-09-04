import type { Table, TableMeta } from '@/api'

/**
 * The one heading anything on a page gets: its name, its row count when it has rows, and its note
 * on hover.
 *
 * Shared by the pane, the single-table page and every chart, so the three can never drift into
 * three ways of naming the thing above them. The note is on the heading, never in the body; the
 * glyph says there is something to hover, because a description a reader has no reason to suspect
 * is not a description.
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
  count,
  as: Tag = 'h3',
}: {
  name: string
  note: string | null
  /** Rows behind this heading, omitted for a chart, which has no row count to state. */
  count?: number
  as?: 'h1' | 'h2' | 'h3'
}) {
  return (
    <Tag
      className="flex items-baseline gap-2 text-md font-semibold text-ink-dim"
      title={note ?? undefined}
      aria-description={note ?? undefined}
    >
      <span>{name}</span>
      {count !== undefined && (
        <span className="text-2xs font-normal tabular-nums text-ink-faint">
          {count.toLocaleString()} {count === 1 ? 'row' : 'rows'}
        </span>
      )}
      {note && (
        <span
          aria-hidden="true"
          className="rounded border border-edge px-1 text-2xs font-normal leading-4 text-ink-faint"
        >
          ?
        </span>
      )}
    </Tag>
  )
}

export function TableHeading({
  name,
  table,
  note,
  as,
}: {
  name: string
  table: Table
  note: string | null
  as?: 'h1' | 'h2' | 'h3'
}) {
  return <Heading name={name} note={note} count={table.rows.length} as={as} />
}
