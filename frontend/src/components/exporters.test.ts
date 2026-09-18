/**
 * An export is never the preview. The messages table carries the first 220 characters of each
 * message; the file somebody asked for carries the message.
 */
import { describe, expect, it, vi } from 'vitest'
import {
  downloadMarkdown, downloadTextFile, hydrate, toMarkdownTable, type Sheet,
} from './exporters'

describe('the markdown exports', () => {
  const table = {
    name: 'A table: notes',
    columns: [
      { id: 'a', label: 'Name', numeric: false, specifier: null, align: 'left' as const, hidden: false, bands: [] },
      { id: 'n', label: 'Rows', numeric: true, specifier: null, align: 'right' as const, hidden: false, bands: [] },
    ],
    rows: [{ a: 'one | two', n: 3 }, { a: 'line\nbreak', n: 4 }],
    format: (value: unknown) => (value === null || value === undefined ? '' : String(value)),
  }

  it('writes the document as it is held, with no byte-order mark', () => {
    // The CSV writer adds one so Excel reads UTF-8. A markdown file must not have one: some
    // editors draw it as a stray glyph, and it can stop a leading `#` from parsing as a heading.
    const types: string[] = []
    const names: string[] = []
    const url = vi.spyOn(URL, 'createObjectURL').mockImplementation((blob) => {
      types.push((blob as Blob).type)
      return 'blob:x'
    })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) { names.push(this.download) })
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    downloadMarkdown('A summary: one', '# Title\n')
    downloadTextFile('A summary: one', '# Title\n')
    expect(types).toEqual(['text/markdown;charset=utf-8', 'text/plain;charset=utf-8'])
    expect(names).toEqual(['A summary_ one.md', 'A summary_ one.txt'])
    url.mockRestore(); click.mockRestore(); revoke.mockRestore()
  })

  it('writes a pipe table that survives a pipe and a newline inside a cell', () => {
    const lines = toMarkdownTable(table).split('\n')
    expect(lines[0]).toBe('| Name | Rows |')
    // The numeric column is right-aligned by the delimiter row, the rule the PDF export uses.
    expect(lines[1]).toBe('| --- | ---: |')
    expect(lines[2]).toBe('| one \\| two | 3 |')
    expect(lines[3]).toBe('| line break | 4 |')
    // EVERY ROW HAS THE SAME NUMBER OF CELLS, counted the way a markdown parser counts them: a
    // pipe that is escaped is content, not a cell boundary. An unescaped pipe or a stray newline
    // silently splits a row, and a table that renders with a shifted column is worse than none.
    const cells = (line: string) => line.split(/(?<!\\)\|/).length
    expect(new Set(lines.map(cells)).size).toBe(1)
  })
})


const column = (id: string) => ({
  id, label: id, numeric: false, specifier: null, align: 'left' as const, hidden: false, bands: [],
})

function sheet(over: Partial<Sheet> = {}): Sheet {
  return {
    columns: [column('uuid'), column('preview')],
    rows: [
      { uuid: 'a', preview: 'first two hundred and twenty characters of a' },
      { uuid: 'b', preview: 'first two hundred and twenty characters of b' },
    ],
    name: 'messages',
    format: (v) => String(v ?? ''),
    fullText: { url: '/api/messages/text', key: 'uuid', column: 'preview', as: 'text' },
    ...over,
  }
}

describe('hydrate fills a cut column from the server, once, for the file', () => {
  it('posts the keys and replaces the preview with the full text', async () => {
    const post = vi.fn(async () => ({ a: 'the whole of a', b: 'the whole of b' }))
    const full = await hydrate(sheet(), post)
    expect(post).toHaveBeenCalledWith('/api/messages/text', { uuids: ['a', 'b'] })
    expect(full.rows.map((r) => r.preview)).toEqual(['the whole of a', 'the whole of b'])
  })

  it('keeps the preview for a row the server did not answer, rather than blanking it', async () => {
    const post = vi.fn(async () => ({ a: 'the whole of a' }))
    const full = await hydrate(sheet(), post)
    expect(full.rows[1].preview).toBe('first two hundred and twenty characters of b')
  })

  it('does not touch the network for a sheet with nothing cut (gate can fail)', async () => {
    const post = vi.fn(async () => ({}))
    const same = await hydrate(sheet({ fullText: undefined }), post)
    expect(post).not.toHaveBeenCalled()
    expect(same.rows[0].preview).toBe('first two hundred and twenty characters of a')
  })

  it('leaves the rows it was given alone', async () => {
    const original = sheet()
    await hydrate(original, async () => ({ a: 'changed' }))
    expect(original.rows[0].preview).toBe('first two hundred and twenty characters of a')
  })
})
