import { useEffect, useRef, useState } from 'react'
import { Check, ChevronDown, Columns3, Download } from 'lucide-react'
import type { ColumnMeta } from '@/api'
import {
  copyToClipboard, downloadCsv, downloadExcel, downloadPdf, printSheet, type Sheet,
} from './exporters'

/**
 * The controls that belong to a table: how many rows, which columns, and how to get it out.
 *
 * ON THE TABLE, not in the collapsible below it. CSV used to live inside the SQL accordion, and
 * only two of the eight tabs have one, so eleven of seventeen tables could not be exported at all.
 */

function Menu({
  label,
  icon,
  children,
}: {
  label: string
  icon: React.ReactNode
  children: (close: () => void) => React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const away = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', escape)
    }
  }, [open])

  return (
    <div ref={box} className="relative">
      <button
        onClick={() => setOpen((was) => !was)}
        aria-expanded={open}
        aria-haspopup="menu"
        className="flex items-center gap-1.5 rounded border border-edge px-2 py-1 text-xs
                   text-ink-dim transition-colors duration-150 hover:text-ink"
      >
        {icon}
        <span>{label}</span>
        <ChevronDown size={12} aria-hidden="true" />
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-30 mt-1 max-h-80 min-w-52 overflow-auto rounded-md
                     bg-panel-raised p-1 shadow-float"
        >
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  )
}

function Item({
  onClick,
  children,
  checked,
}: {
  onClick: () => void
  children: React.ReactNode
  checked?: boolean
}) {
  return (
    <button
      role="menuitem"
      onClick={onClick}
      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-ink-dim
                 transition-colors duration-150 hover:bg-panel hover:text-ink"
    >
      {checked === undefined ? null : (
        <span className="w-3.5 shrink-0 text-accent">
          {checked ? <Check size={13} aria-hidden="true" /> : null}
        </span>
      )}
      <span className="truncate">{children}</span>
    </button>
  )
}

export function TableToolbar({
  sheet,
  allColumns,
  hidden,
  onToggleColumn,
  onShowAll,
  onHideAll,
  onHideEmpty,
  pageSize,
  onPageSize,
  children,
}: {
  sheet: Sheet
  allColumns: ColumnMeta[]
  hidden: Set<string>
  onToggleColumn: (id: string) => void
  onShowAll: () => void
  onHideAll: () => void
  onHideEmpty: () => void
  pageSize: number
  onPageSize: (size: number) => void
  children?: React.ReactNode
}) {
  const [said, setSaid] = useState('')
  const say = (message: string) => {
    setSaid(message)
    setTimeout(() => setSaid(''), 1800)
  }

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-edge/60 px-3 py-2">
      {children}

      <div className="ml-auto flex items-center gap-2">
        {/* Announced politely so a screen reader hears "Copied" without the focus moving. */}
        <span aria-live="polite" className="text-2xs text-ink-faint">{said}</span>

        <label className="flex items-center gap-1.5 text-2xs text-ink-faint">
          Rows
          <select
            value={pageSize}
            onChange={(event) => onPageSize(Number(event.target.value))}
            aria-label="Rows per page"
            className="rounded border border-edge bg-page px-1.5 py-1 text-xs text-ink
                       outline-none focus:border-accent"
          >
            {[10, 25, 50, 100].map((size) => (
              <option key={size} value={size}>{size}</option>
            ))}
            {/* -1 is "all". A number would have to be a guess about the largest table. */}
            <option value={-1}>All</option>
          </select>
        </label>

        <Menu label="Columns" icon={<Columns3 size={13} aria-hidden="true" />}>
          {(close) => (
            <>
              <Item onClick={() => { onShowAll(); close() }}>Show All</Item>
              <Item onClick={() => { onHideAll(); close() }}>Hide All</Item>
              {/* The reference calls this "Uncheck Empty". Worth keeping: a column that is blank
                  for every row is a column that costs width and says nothing. */}
              <Item onClick={() => { onHideEmpty(); close() }}>Hide Empty</Item>
              <div className="my-1 border-t border-edge/60" />
              {allColumns.map((column) => (
                <Item
                  key={column.id}
                  checked={!hidden.has(column.id)}
                  onClick={() => onToggleColumn(column.id)}
                >
                  {column.label}
                </Item>
              ))}
            </>
          )}
        </Menu>

        <Menu label="Export" icon={<Download size={13} aria-hidden="true" />}>
          {(close) => (
            <>
              <Item onClick={async () => {
                say(await copyToClipboard(sheet) ? 'Copied' : 'Clipboard refused')
                close()
              }}>Copy</Item>
              <Item onClick={() => { downloadCsv(sheet); close() }}>CSV</Item>
              {/* Excel and PDF load their libraries on click, so nothing is fetched for a reader
                  who never exports. Failures are reported rather than swallowed. */}
              <Item onClick={async () => {
                try {
                  await downloadExcel(sheet)
                } catch {
                  say('Excel export failed')
                }
                close()
              }}>Excel</Item>
              <Item onClick={async () => {
                try {
                  await downloadPdf(sheet)
                } catch {
                  say('PDF export failed')
                }
                close()
              }}>PDF</Item>
              <Item onClick={() => {
                if (!printSheet(sheet)) say('Pop-up blocked')
                close()
              }}>Print</Item>
            </>
          )}
        </Menu>
      </div>
    </div>
  )
}
