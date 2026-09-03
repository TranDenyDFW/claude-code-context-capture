import type { ColumnMeta, TableMeta } from '@/api'

/**
 * Getting a table out of the page: copy, CSV, Excel, PDF, print.
 *
 * EVERY TABLE GETS THESE, which is the point. Dash gave every table `export_format="csv"` through
 * `TABLE_STYLE`; this port put CSV inside the SQL accordion instead, and only Cost and Summary have
 * one, so eleven of seventeen tables had no way to get the data out at all. Nobody reported it,
 * because a missing button looks exactly like a table that never had one.
 *
 * EXCEL AND PDF ARE LAZY. `write-excel-file` and `jspdf` together are far larger than the rest of
 * this app, so they are behind `import()` exactly as Plotly is: they become their own chunks, land
 * in the committed build, and are never fetched until somebody actually clicks Export.
 *
 * `write-excel-file` rather than `xlsx`: the last build of SheetJS published to the npm registry is
 * 0.18.5 and carries known parser CVEs. This app only ever WRITES, so the exposure is small, but a
 * maintained write-only package is the better choice in a tool that can read every conversation on
 * the machine.
 *
 * `jspdf` + `jspdf-autotable` rather than `pdfmake`, which is mostly embedded fonts.
 */

export interface Sheet {
  /** The visible columns, in the order they are shown. */
  columns: ColumnMeta[]
  /** Where a cut column's full text is; see `TableMeta.full_text`. */
  fullText?: TableMeta['full_text']
  /** Rows as the table has them: filtered and sorted, keyed by column id. */
  rows: Record<string, unknown>[]
  /** What the file and the document title are called. */
  name: string
  /** How a value is written, so an export matches the screen. */
  format: (value: unknown, column: ColumnMeta) => string
}

/** A filename Windows will accept, with the extension left to the caller. */
/**
 * The sheet with every cut column filled in from the server, or the sheet as it is when nothing
 * was cut. Called once per export, on purpose: 400 full messages are 841 KB on the largest session,
 * which is nothing for a file somebody asked for and far too much for every render of a tab.
 *
 * A row whose key the server does not answer keeps its preview rather than going blank: a preview
 * is a true prefix and a blank would read as an empty message.
 */
export async function hydrate(sheet: Sheet, post = postJson): Promise<Sheet> {
  const spec = sheet.fullText
  if (!spec) return sheet
  const keys = sheet.rows.map((row) => row[spec.key]).filter((k): k is string => typeof k === 'string')
  if (keys.length === 0) return sheet
  const full = await post(spec.url, { [`${spec.key}s`]: keys })
  const rows = sheet.rows.map((row) => {
    const key = row[spec.key]
    const text = typeof key === 'string' ? full[key] : undefined
    return typeof text === 'string' ? { ...row, [spec.column]: text } : row
  })
  return { ...sheet, rows }
}

async function postJson(url: string, body: unknown): Promise<Record<string, string>> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`${url}: ${response.status}`)
  return (await response.json()) as Record<string, string>
}

export function safeName(name: string): string {
  return (name || 'table')
    .replace(/[\\/:*?"<>|]+/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 120) || 'table'
}

function matrix(sheet: Sheet): string[][] {
  return [
    sheet.columns.map((c) => c.label),
    ...sheet.rows.map((row) => sheet.columns.map((c) => sheet.format(row[c.id], c))),
  ]
}

/** RFC 4180 enough to survive a value containing a comma, a quote or a newline. */
export function toCsv(sheet: Sheet): string {
  const escape = (text: string) =>
    /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
  return matrix(sheet).map((row) => row.map(escape).join(',')).join('\r\n')
}

/** Tab separated, which is what a spreadsheet expects from the clipboard. */
export function toClipboardText(sheet: Sheet): string {
  return matrix(sheet).map((row) => row.join('\t')).join('\n')
}

function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  // Revoked on the next tick: revoking synchronously can cancel the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function downloadCsv(sheet: Sheet) {
  // A BOM, so Excel opens a UTF-8 CSV without mangling anything outside ASCII. Project paths in
  // this store are full of characters that come back as mojibake without it.
  download(new Blob(['﻿', toCsv(sheet)], { type: 'text/csv;charset=utf-8' }),
           `${safeName(sheet.name)}.csv`)
}

export async function copyToClipboard(sheet: Sheet): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(toClipboardText(sheet))
    return true
  } catch {
    // Clipboard access can be refused outright. Reported to the caller so the button can say so,
    // rather than appearing to work.
    return false
  }
}

export async function downloadExcel(sheet: Sheet) {
  // The `/browser` subpath, not the package root: this package publishes no root export, only
  // `./node`, `./browser` and `./universal`. The node build reaches for `fs`.
  const writeXlsx = (await import('write-excel-file/browser')).default
  // Numbers stay NUMBERS in the sheet. Writing the formatted string would make every column text,
  // and a spreadsheet cannot sum text: the export would look right and be useless for the one
  // thing a spreadsheet is for.
  type Cell = { value?: string | number; type?: unknown; fontWeight?: 'bold' }
  const header: Cell[] = sheet.columns.map((c) => ({ value: c.label, fontWeight: 'bold' }))
  const body: Cell[][] = sheet.rows.map((row) =>
    sheet.columns.map((c) => {
      const raw = row[c.id]
      // An EMPTY CELL, not a zero and not the string "null". Same rule as the table: an unpriced
      // model has an unknown cost, and a spreadsheet that reads 0 there would sum it.
      if (raw === null || raw === undefined) return {}
      if (c.numeric && typeof raw === 'number' && Number.isFinite(raw)) {
        return { type: Number, value: raw }
      }
      return { type: String, value: String(raw) }
    }),
  )
  await writeXlsx([header, ...body] as never, {
    fileName: `${safeName(sheet.name)}.xlsx`,
  } as never)
}

export async function downloadPdf(sheet: Sheet) {
  const { jsPDF } = await import('jspdf')
  const autoTable = (await import('jspdf-autotable')).default
  // Landscape: these tables are wide, and a portrait page turns thirteen columns into confetti.
  const doc = new jsPDF({ orientation: 'landscape', unit: 'pt', format: 'a4' })
  doc.setFontSize(12)
  doc.text(sheet.name, 40, 34)
  autoTable(doc, {
    head: [sheet.columns.map((c) => c.label)],
    body: sheet.rows.map((row) => sheet.columns.map((c) => sheet.format(row[c.id], c))),
    startY: 46,
    styles: { fontSize: 7, cellPadding: 3, overflow: 'linebreak' },
    headStyles: { fillColor: [28, 33, 41], textColor: [230, 237, 243] },
    // Numeric columns right-aligned in the PDF too, so it reads like the table it came from.
    columnStyles: Object.fromEntries(
      sheet.columns.map((c, index) => [index, { halign: c.numeric ? 'right' : 'left' }]),
    ),
  })
  doc.save(`${safeName(sheet.name)}.pdf`)
}

/**
 * Print just this table.
 *
 * A new window with a plain document rather than a print stylesheet over the app: the page has a
 * sidebar, a sticky header and a scroll container, and getting all of that to lay out on paper is
 * more work than writing the table out again, with a worse result.
 */
export function printSheet(sheet: Sheet) {
  const win = window.open('', '_blank')
  if (!win) return false           // pop-up blocked; the caller says so rather than doing nothing

  const rows = matrix(sheet)
  const doc = win.document

  // BUILT AS NODES, not as a string of HTML. Every value here comes from the local store, which
  // includes verbatim message text and file paths, and `textContent` cannot be made to inject
  // markup at all. Hand-written escaping in a template string is one forgotten character away from
  // the opposite, and there is no reason to take that on for a print view.
  const style = doc.createElement('style')
  style.textContent = [
    'body{font:11px system-ui,sans-serif;margin:24px}',
    'h1{font-size:14px;margin:0 0 12px}',
    'table{border-collapse:collapse;width:100%}',
    'th,td{border:1px solid #ccc;padding:4px 6px;text-align:left}',
    'th{background:#eee}',
    'td.n{text-align:right;font-variant-numeric:tabular-nums}',
    'tr:nth-child(even){background:#f7f7f7}',
  ].join('')
  doc.head.append(style)
  doc.title = sheet.name

  const heading = doc.createElement('h1')
  heading.textContent = sheet.name
  doc.body.append(heading)

  const table = doc.createElement('table')
  const thead = doc.createElement('thead')
  const headRow = doc.createElement('tr')
  for (const label of rows[0]) {
    const th = doc.createElement('th')
    th.textContent = label
    headRow.append(th)
  }
  thead.append(headRow)
  table.append(thead)

  const tbody = doc.createElement('tbody')
  for (const row of rows.slice(1)) {
    const tr = doc.createElement('tr')
    row.forEach((cell, at) => {
      const td = doc.createElement('td')
      if (sheet.columns[at]?.numeric) td.className = 'n'
      td.textContent = cell
      tr.append(td)
    })
    tbody.append(tr)
  }
  table.append(tbody)
  doc.body.append(table)

  win.focus()
  win.print()
  return true
}
