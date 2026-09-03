/**
 * An export is never the preview. The messages table carries the first 220 characters of each
 * message; the file somebody asked for carries the message.
 */
import { describe, expect, it, vi } from 'vitest'
import { hydrate, type Sheet } from './exporters'

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
