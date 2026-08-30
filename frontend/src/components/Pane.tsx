import type { TabPayload } from '@/api'
import { DataTable } from './DataTable'
import { Plot } from './Plot'

/**
 * A whole tab, drawn from the payload, with no per-tab code.
 *
 * This is the payoff from making `extract.describe()` the API contract rather than inventing a new
 * one per page. Every tab in this app is some tables, some charts and some prose, and the server
 * says which. So ONE renderer covers all eight from the first day, and per-tab work is only needed
 * where a tab has an interaction, not merely content. The migration plan budgeted a page per tab;
 * most of that turned out to be unnecessary.
 *
 * The order is charts, then prose, then tables, which is not the order the payload lists them in.
 * It is the order they are read in: the shape first, the caveat second, the numbers third.
 */
/**
 * A table's heading, or nothing.
 *
 * `(anonymous)` is not a name. It is what `extract.describe()` prints for a table that has no DOM
 * id, and rendering it would put the word five times down the Cost tab. The real ids are DOM ids
 * (`tbl-compactions`), so the prefix comes off: a heading is for a reader, not for a selector.
 */
function heading(id: string | null): string | null {
  if (!id || id === '(anonymous)') return null
  return id.replace(/^tbl-/, '').replace(/-/g, ' ')
}

export function Pane({
  payload,
  onRowClick,
}: {
  payload: TabPayload
  onRowClick?: (row: Record<string, unknown>) => void
}) {
  const figures = payload.plotly ?? []
  return (
    <div className="flex flex-col gap-5">
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

      {payload.text.length > 0 && (
        <section className="rounded-lg border border-edge bg-panel px-4 py-3">
          {payload.text.map((line, index) => (
            <p key={index} className="text-[12.5px] leading-relaxed text-ink-dim">
              {line}
            </p>
          ))}
        </section>
      )}

      {payload.tables.map((table, index) => (
        // Keyed by INDEX, not by id. Five of the Cost tab's six tables report the id
        // `(anonymous)`, which is what the extractor calls a table that has none, so keying on it
        // collided four times and React warned about duplicate keys on every render. Index is
        // stable here because the whole payload is replaced at once rather than reordered.
        <section key={index} className="flex flex-col gap-2">
          {heading(table.id) && (
            <h3 className="text-[13px] font-semibold text-ink-dim">{heading(table.id)}</h3>
          )}
          <DataTable table={table} onRowClick={onRowClick} />
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
