/**
 * Markdown is rendered, nothing is dropped, and nothing is executed.
 *
 * The third is why this file is careful rather than cheerful: the text here comes from arbitrary
 * transcripts, including web pages a session fetched. A renderer that can be made to run a script,
 * follow a `javascript:` link or fetch an image tells a third party what somebody opened.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Markdown, TextBody } from './Markdown'
import { safeHref } from './markdownRules'
import * as exporters from './exporters'
// `?raw` is vite's own text import, so this reads the source with no node types.
import markdownSource from './Markdown.tsx?raw'
import rulesSource from './markdownRules.ts?raw'

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('the renderer', () => {
  it('makes headings, lists, code and a table out of markdown', () => {
    const source = [
      '# One', '## Two', '', '- first', '- second', '', '1. step', '', '> quoted', '',
      '```js', 'const a = 1', '```', '', 'a `span` of code and **bold** text', '',
      '| a | b |', '| --- | ---: |', '| 1 | 2 |', '', '---',
    ].join('\n')
    const { container } = render(<Markdown source={source} />)
    expect(container.querySelector('h3')?.textContent).toBe('One')
    expect(container.querySelectorAll('li').length).toBe(3)
    expect(container.querySelector('pre code')?.textContent).toContain('const a = 1')
    expect(container.querySelector('blockquote')).not.toBeNull()
    expect(container.querySelector('strong')?.textContent).toBe('bold')
    expect(container.querySelectorAll('table th').length).toBe(2)
    expect(container.querySelector('hr')).not.toBeNull()
  })

  it('keeps every angle-bracket placeholder, whatever an upgrade decides to do', () => {
    // WHY THIS IS PINNED. This store's markdown is full of them: measured over the 732 documents
    // this repo has collected, 343 tokens in 100 files, `<stdin>` 42 times and `<uuid>` 31. A
    // renderer that treated them as markup would delete them and say nothing, which is the one
    // thing a page for reading a store must never do. react-markdown 10.1.0 renders them as text;
    // this is the check that would catch a version that stopped.
    const { container } = render(
      <Markdown source={'Read /api/compaction/<uuid> for <stdin>, and a <div>block</div>'} />)
    expect(container.textContent).toContain('/api/compaction/<uuid>')
    expect(container.textContent).toContain('<stdin>')
    expect(container.textContent).toContain('<div>block</div>')
  })

  it('does not run a script, and shows it as text', () => {
    const { container } = render(
      <Markdown source={'before\n\n<script>window.__pwned = 1</script>\n\nafter'} />)
    expect(container.querySelector('script')).toBeNull()
    expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined()
    expect(container.textContent).toContain('<script>')
  })

  it('does not fetch an image a transcript names', () => {
    // An `img src` is a request to a third party saying somebody opened this record.
    const { container } = render(<Markdown source={'![a picture](https://example.invalid/x.png)'} />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('https://example.invalid/x.png')
  })

  it('links only to schemes it knows, and never lets one open with the page behind it', () => {
    const { container } = render(
      <Markdown source={'[doc](https://example.invalid/a) and [bad](javascript:alert(1))'} />)
    const links = [...container.querySelectorAll('a')]
    expect(links.length).toBe(1)
    expect(links[0].getAttribute('href')).toBe('https://example.invalid/a')
    expect(links[0].getAttribute('rel')).toBe('noopener noreferrer nofollow')
    expect(links[0].getAttribute('target')).toBe('_blank')
    expect(container.innerHTML).not.toContain('javascript:')
  })

  it('never builds HTML from a string at all (gate can fail)', () => {
    // The structural guarantee behind the three cases above: no `dangerouslySetInnerHTML` and no
    // `rehype-raw` anywhere in the component or its helper.
    //
    // COMMENTS ARE STRIPPED FIRST, or this gate fails on the sentence in the docstring that says
    // those two are deliberately absent. A check that cannot tell prose from code is not a check.
    const code = (text: string) =>
      text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
    for (const text of [markdownSource, rulesSource]) {
      expect(code(text)).not.toMatch(/dangerouslySetInnerHTML|rehype-raw|innerHTML =/)
    }
  })

  it('allows a relative path and refuses an unknown scheme', () => {
    expect(safeHref('https://x/y')).toBe('https://x/y')
    expect(safeHref('mailto:a@b')).toBe('mailto:a@b')
    expect(safeHref('/api/thing')).toBe('/api/thing')
    expect(safeHref('javascript:alert(1)')).toBe('')
    expect(safeHref('data:text/html,<b>')).toBe('')
    expect(safeHref('vbscript:x')).toBe('')
  })
})

describe('the body around it', () => {
  const SOURCE = '# Title\n\nSome **text** with `code`.\n\n| a |\n| --- |\n| 1 |\n'

  it('shows the markdown by default and the source on request, byte for byte', () => {
    const { container } = render(<TextBody source={SOURCE} name="a-summary" boxClass="max-h-[40vh]" />)
    expect(screen.getByRole('heading', { level: 3 }).textContent).toBe('Title')
    fireEvent.click(screen.getByRole('button', { name: 'Raw' }))
    // THE GUARANTEE: Raw is the document, untouched, so a reader can always check the renderer.
    // Read from the element rather than through a text query, which normalises whitespace and
    // therefore cannot compare a multi-line document to itself.
    expect(container.querySelector('pre')?.textContent).toBe(SOURCE)
    fireEvent.click(screen.getByRole('button', { name: 'Markdown' }))
    expect(screen.getByRole('heading', { level: 3 }).textContent).toBe('Title')
  })

  it('starts on the raw view when the caller says the text is not a document', () => {
    render(<TextBody source={SOURCE} name="a-result" boxClass="max-h-[40vh]" defaultView="raw" />)
    expect(screen.queryByRole('heading', { level: 3 })).toBeNull()
    expect(screen.getByRole('button', { name: 'Raw' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('exports the source, not what is on screen', () => {
    const md = vi.spyOn(exporters, 'downloadMarkdown').mockImplementation(() => {})
    const txt = vi.spyOn(exporters, 'downloadTextFile').mockImplementation(() => {})
    render(<TextBody source={SOURCE} name="a-summary" boxClass="max-h-[40vh]" />)
    fireEvent.click(screen.getByRole('button', { name: '.md' }))
    expect(md).toHaveBeenCalledWith('a-summary', SOURCE)
    fireEvent.click(screen.getByRole('button', { name: '.txt' }))
    expect(txt).toHaveBeenCalledWith('a-summary', SOURCE)
  })

  it('offers no file while only part of the document is in hand', () => {
    // Saving a 400-character preview under the plan's own name hands somebody a file that looks
    // like the document and is its first paragraph.
    render(<TextBody source={SOURCE} name="a-plan" boxClass="max-h-[24vh]" canExport={false} />)
    expect(screen.queryByRole('button', { name: '.md' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Raw' })).not.toBeNull()
  })

  it('copies the source', async () => {
    const write = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText: write } })
    render(<TextBody source={SOURCE} name="a-summary" boxClass="max-h-[40vh]" />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(write).toHaveBeenCalledWith(SOURCE)
  })
})
