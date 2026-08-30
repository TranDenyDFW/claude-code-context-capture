/**
 * CSV export, which is the one place this frontend PRODUCES data rather than displaying it.
 *
 * A row rendered wrong is visible. A CSV written wrong is not: it opens in a spreadsheet, the
 * columns shift by one from the first value that contained a comma, and every number after it is
 * under the wrong heading. Nothing about the file looks broken.
 */
import { describe, expect, it } from 'vitest'
import { toCsv } from './Section'

const table = (columns: string[], rows: Record<string, unknown>[]) => ({ id: 't', columns, rows })

describe('CSV export', () => {
  it('writes a header and one line per row', () => {
    const csv = toCsv(table(['a', 'b'], [{ a: 1, b: 2 }, { a: 3, b: 4 }]))
    expect(csv.split('\r\n')).toEqual(['a,b', '1,2', '3,4'])
  })

  it('quotes a value containing a comma, which would otherwise shift every later column', () => {
    const csv = toCsv(table(['name', 'n'], [{ name: 'Smith, John', n: 1 }]))
    expect(csv.split('\r\n')[1]).toBe('"Smith, John",1')
  })

  it('doubles a quote inside a value rather than ending the field early', () => {
    const csv = toCsv(table(['q'], [{ q: 'he said "no"' }]))
    expect(csv.split('\r\n')[1]).toBe('"he said ""no"""')
  })

  it('quotes a value containing a newline, so one row stays one record', () => {
    const csv = toCsv(table(['sql'], [{ sql: 'SELECT 1\nFROM t' }]))
    expect(csv).toContain('"SELECT 1\nFROM t"')
  })

  it('writes an unknown value as EMPTY, never as zero or "null"', () => {
    // Same rule as the table: a missing price is unknown, not free. A CSV that says 0 there would
    // be summed by a spreadsheet into a total that is confidently wrong.
    const csv = toCsv(table(['usd'], [{ usd: null }, { usd: undefined }, { usd: 0 }]))
    expect(csv.split('\r\n').slice(1)).toEqual(['', '', '0'])
  })

  it('exports every row, not the page that happens to be on screen', () => {
    const rows = Array.from({ length: 317 }, (_, index) => ({ n: index }))
    expect(toCsv(table(['n'], rows)).split('\r\n')).toHaveLength(318)
  })
})
