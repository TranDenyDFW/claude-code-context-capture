import type { Table, TableMeta } from '@/api'

/**
 * The one heading every table gets: its name, its row count, and its note on hover.
 *
 * Shared by the pane and by the single-table page, so the two can never drift into two ways of
 * naming the same table. The note is on the heading, never in the body; the glyph says there is
 * something to hover, because a description a reader has no reason to suspect is not a description.
 */
export function tableName(
  index: number, meta: TableMeta | undefined, sectionTitle: string | undefined,
): string {
  return sectionTitle || meta?.title || `Table ${index + 1}`
}

export function TableHeading({
  name,
  table,
  note,
  as: Tag = 'h3',
}: {
  name: string
  table: Table
  note: string | null
  as?: 'h1' | 'h2' | 'h3'
}) {
  const count = table.rows.length
  return (
    <Tag
      className="flex items-baseline gap-2 text-md font-semibold text-ink-dim"
      title={note ?? undefined}
      aria-description={note ?? undefined}
    >
      <span>{name}</span>
      <span className="text-2xs font-normal tabular-nums text-ink-faint">
        {count.toLocaleString()} {count === 1 ? 'row' : 'rows'}
      </span>
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
