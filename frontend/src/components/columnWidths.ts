import type { ColumnMeta } from '@/api'

/**
 * How wide each column is, decided once from the data and never from the page on screen.
 *
 * THE DEFECT THIS REMOVES. The table had no width rule at all: `table-layout` was never set, so the
 * browser measured the cells that happened to be in the DOM, and only one page of rows ever is.
 * Sorting changes which values are on the page, so the columns re-fitted themselves on every click
 * and the whole table jumped sideways. The user's words: "CHANGE ALL TABLES TO NOT RESIZE COLUMNS
 * WHEN SORTING".
 *
 * THE FIX IS THE SIGNATURE, NOT A FLAG. `estimateWidths` takes every row the table holds and knows
 * nothing about sorting, paging or filtering, so a width cannot depend on them. That is a property
 * of what this function can see rather than a behaviour anybody has to remember.
 *
 * CHARACTERS, NOT PIXELS. Measuring text needs a browser, and this must run in a test where
 * `getBoundingClientRect` is hardcoded to zero; a canvas measurer can be dropped in through
 * `measure` later without any other change. The estimate is deliberately generous by a character
 * or two: a column slightly too wide reads fine, one slightly too narrow ellipsises a value.
 */
export const COLUMN = {
  MIN: 64,
  MAX: 640,
  /** 24rem, exactly the cap the one `wide` column already had. */
  MAX_WIDE: 384,
  /** `px-3` on both sides. */
  PAD: 24,
  /** The sort arrow's slot, reserved on every header so sorting never shoves a label. */
  ARROW: 16,
  /** Average advance of the 12.5px system sans, and of the monospace the right-aligned cells use. */
  CH_SANS: 6.6,
  CH_MONO: 7.6,
} as const

/** Fullwidth ranges (CJK, kana, fullwidth forms) take two cells, not one. */
function cells(text: string): number {
  let n = 0
  for (const ch of text) {
    const code = ch.codePointAt(0) ?? 0
    const wide =
      (code >= 0x1100 && code <= 0x115f) || (code >= 0x2e80 && code <= 0xa4cf) ||
      (code >= 0xac00 && code <= 0xd7a3) || (code >= 0xf900 && code <= 0xfaff) ||
      (code >= 0xfe30 && code <= 0xfe6f) || (code >= 0xff00 && code <= 0xff60) ||
      (code >= 0xffe0 && code <= 0xffe6)
    n += wide ? 2 : 1
  }
  return n
}

export function charWidth(text: string, mono: boolean): number {
  return cells(text) * (mono ? COLUMN.CH_MONO : COLUMN.CH_SANS)
}

/**
 * A width per column id, from the label and EVERY row, never a page of them.
 *
 * Keyed by id and not by position, so a column keeps its width when the reader drags it somewhere
 * else in the row.
 */
export function estimateWidths(
  columns: ColumnMeta[],
  rows: Record<string, unknown>[],
  format: (value: unknown, column: ColumnMeta) => string,
  measure: (text: string, mono: boolean) => number = charWidth,
): Record<string, number> {
  const out: Record<string, number> = {}
  for (const column of columns) {
    const mono = column.align === 'right'
    let widest = measure(column.label, false)
    for (const row of rows) {
      const text = format(row[column.id], column)
      if (text) widest = Math.max(widest, measure(text, mono))
    }
    const cap = column.wide ? COLUMN.MAX_WIDE : COLUMN.MAX
    const want = Math.ceil(widest) + COLUMN.PAD + COLUMN.ARROW
    out[column.id] = Math.max(COLUMN.MIN, Math.min(cap, want))
  }
  return out
}

/**
 * The widths actually used: what the reader set, else what the data suggests, plus any slack.
 *
 * SLACK GOES TO ONE COLUMN, the rightmost the reader has NOT set. Spreading it across all of them
 * would quietly override every derived width and make a drag feel like it moved the wrong edge;
 * giving it to a column somebody sized by hand would undo their choice. When every column is
 * pinned, the table simply sits narrower than its container, which is what pinning means.
 *
 * `containerWidth` is null wherever nothing can be measured (a test, or a browser with no
 * ResizeObserver), and then this returns exactly `override ?? base`.
 */
export function layoutWidths(
  base: Record<string, number>,
  override: Record<string, number>,
  order: string[],
  containerWidth: number | null,
): { widths: Record<string, number>; total: number } {
  const widths: Record<string, number> = {}
  let sum = 0
  for (const id of order) {
    const width = override[id] ?? base[id] ?? COLUMN.MIN
    widths[id] = width
    sum += width
  }
  if (containerWidth && containerWidth > sum) {
    const free = [...order].reverse().find((id) => override[id] === undefined)
    if (free) {
      widths[free] += containerWidth - sum
      sum = containerWidth
    }
  }
  return { widths, total: sum }
}
