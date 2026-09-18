/**
 * The width function, with no DOM anywhere near it.
 *
 * The point of most of these is what the function CANNOT see: it is handed every row, never a
 * page, so no ordering of the rows can change its answer. That is what stops a sort from moving
 * the columns, and it is a property of the signature rather than a behaviour to be remembered.
 */
import { describe, expect, it } from 'vitest'
import type { ColumnMeta } from '@/api'
import { COLUMN, charWidth, estimateWidths, layoutWidths } from './columnWidths'

const col = (id: string, over: Partial<ColumnMeta> = {}): ColumnMeta => ({
  id, label: id, numeric: false, specifier: null, align: 'left', hidden: false, bands: [], ...over,
})

const text = (value: unknown) => (value === null || value === undefined ? '' : String(value))

describe('estimateWidths', () => {
  it('reads the longest value wherever it sits, not the first page of rows', () => {
    const rows = Array.from({ length: 100 }, (_, i) => ({ a: i === 99 ? 'a'.repeat(40) : 'x' }))
    const wide = estimateWidths([col('a')], rows, text)
    const short = estimateWidths([col('a')], rows.slice(0, 25), text)
    expect(wide.a).toBeGreaterThan(short.a)
  })

  it('answers the same whatever order the rows arrive in', () => {
    const rows = [{ a: 'one' }, { a: 'a much longer value' }, { a: 'two' }]
    expect(estimateWidths([col('a')], rows, text))
      .toEqual(estimateWidths([col('a')], [...rows].reverse(), text))
  })

  it('caps the one wide column where it was always capped, and not the others', () => {
    const rows = [{ a: 'z'.repeat(400), b: 'z'.repeat(400) }]
    const got = estimateWidths([col('a', { wide: true }), col('b')], rows, text)
    expect(got.a).toBe(COLUMN.MAX_WIDE)
    expect(got.b).toBeGreaterThan(COLUMN.MAX_WIDE)
    expect(got.b).toBeLessThanOrEqual(COLUMN.MAX)
  })

  it('keeps a floor under a column of empty values', () => {
    expect(estimateWidths([col('a')], [{ a: '' }, { a: null }], text).a).toBe(COLUMN.MIN)
  })

  it('takes the measurer it is given', () => {
    const got = estimateWidths([col('a'), col('b')], [{ a: 'x', b: 'y'.repeat(40) }], text, () => 100)
    expect(got.a).toBe(got.b)
  })

  it('reserves the sort arrow on every column, sorted or not', () => {
    // The arrow used to appear only on the active header, so sorting widened that column by the
    // glyph. The slot is always there, so the first click cannot move anything.
    const got = estimateWidths([col('a', { label: 'Date' })], [{ a: '' }], text)
    expect(got.a).toBe(Math.max(COLUMN.MIN,
      Math.ceil(charWidth('Date', false)) + COLUMN.PAD + COLUMN.ARROW))
  })

  it('measures a right-aligned column in the monospace it is drawn in', () => {
    const rows = [{ a: '1234567890', b: '1234567890' }]
    const got = estimateWidths([col('a'), col('b', { align: 'right' })], rows, text)
    expect(got.b).toBeGreaterThan(got.a)
  })

  it('counts a fullwidth character as two', () => {
    expect(charWidth('日本語', false)).toBeCloseTo(charWidth('abcdef', false))
  })
})

describe('layoutWidths', () => {
  it('is exactly what was derived or set when nothing can be measured', () => {
    const got = layoutWidths({ a: 100, b: 200 }, {}, ['a', 'b'], null)
    expect(got.widths).toEqual({ a: 100, b: 200 })
    expect(got.total).toBe(300)
  })

  it('gives the slack to the rightmost column the reader has not set', () => {
    const got = layoutWidths({ a: 100, b: 200, c: 100 }, { c: 100 }, ['a', 'b', 'c'], 600)
    expect(got.widths).toEqual({ a: 100, b: 400, c: 100 })
    expect(got.total).toBe(600)
  })

  it('leaves a table narrow rather than overriding every width the reader set', () => {
    const got = layoutWidths({ a: 100, b: 100 }, { a: 100, b: 100 }, ['a', 'b'], 900)
    expect(got.widths).toEqual({ a: 100, b: 100 })
    expect(got.total).toBe(200)
  })

  it('never shrinks anything to fit a narrow container', () => {
    const got = layoutWidths({ a: 400, b: 400 }, {}, ['a', 'b'], 300)
    expect(got.widths).toEqual({ a: 400, b: 400 })
    expect(got.total).toBe(800)
  })

  it('follows the order it is given, so a reordered column keeps its own width', () => {
    const got = layoutWidths({ a: 100, b: 200 }, {}, ['b', 'a'], null)
    expect(got.widths).toEqual({ a: 100, b: 200 })
  })
})
