import { useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { safeHref } from './markdownRules'
import { downloadMarkdown, downloadTextFile } from './exporters'

/**
 * A markdown body, rendered, with the source one click away and two ways to take it with you.
 *
 * WHAT IS MARKDOWN HERE, and what only looks like it. A compaction summary and an `ExitPlanMode`
 * plan are documents Claude wrote in markdown: headings, lists, fenced code, tables. A 220-character
 * preview whose newlines the server already replaced with spaces is not a document any more, and
 * SQL is not markdown at all. Those stay exactly as they were.
 *
 * NO RAW HTML IS EVER PARSED. `rehype-raw` is deliberately absent, so nothing ever reaches
 * `dangerouslySetInnerHTML` and a `<script>` in a transcript is text.
 *
 * AND NOTHING IS DROPPED. Measured against react-markdown 10.1.0: `<uuid>`, `<div>x</div>` and
 * `<script>...</script>` all render as their literal characters. That matters because this store's
 * markdown is full of angle-bracket placeholders: 343 of them across 100 documents in this repo's
 * own collection, `<stdin>` 42 times and `<uuid>` 31, `<sid>` 16. A renderer that treated them as
 * markup would delete them silently, which is the one thing a page for READING a store must never
 * do, so `Markdown.test.tsx` pins the behaviour rather than trusting it.
 *
 * NO IMAGES ARE FETCHED. An `img src` in a transcript is a request to a third party that says the
 * reader opened a particular record; the alt text and the address are shown as text instead.
 */
const LINK = 'text-accent underline decoration-edge-bright underline-offset-2 hover:decoration-accent'

const PIECES = {
  h1: ({ children }: { children?: React.ReactNode }) =>
    <h3 className="mt-4 mb-1.5 text-lg font-semibold text-ink first:mt-0">{children}</h3>,
  h2: ({ children }: { children?: React.ReactNode }) =>
    <h4 className="mt-4 mb-1 text-md font-semibold text-ink first:mt-0">{children}</h4>,
  h3: ({ children }: { children?: React.ReactNode }) =>
    <h5 className="mt-3 mb-1 text-sm font-semibold text-ink-dim">{children}</h5>,
  h4: ({ children }: { children?: React.ReactNode }) =>
    <h6 className="mt-3 mb-1 text-xs font-semibold tracking-[0.06em] text-ink-faint uppercase">{children}</h6>,
  p: ({ children }: { children?: React.ReactNode }) =>
    <p className="my-2 text-sm leading-relaxed text-ink first:mt-0 last:mb-0">{children}</p>,
  strong: ({ children }: { children?: React.ReactNode }) =>
    <strong className="font-semibold text-ink">{children}</strong>,
  em: ({ children }: { children?: React.ReactNode }) => <em className="italic">{children}</em>,
  ul: ({ children }: { children?: React.ReactNode }) =>
    <ul className="my-2 list-disc space-y-0.5 pl-5 marker:text-ink-faint">{children}</ul>,
  ol: ({ children }: { children?: React.ReactNode }) =>
    <ol className="my-2 list-decimal space-y-0.5 pl-5 marker:text-ink-faint">{children}</ol>,
  li: ({ children }: { children?: React.ReactNode }) =>
    <li className="text-sm leading-relaxed text-ink">{children}</li>,
  blockquote: ({ children }: { children?: React.ReactNode }) =>
    <blockquote className="my-2 border-l-2 border-edge pl-3 text-sm text-ink-dim italic">{children}</blockquote>,
  hr: () => <hr className="my-3 border-0 border-t border-edge" />,
  // A CODE BLOCK SCROLLS ON ITS OWN, so one long line inside a 17,000-character summary does not
  // push the whole document sideways.
  pre: ({ children }: { children?: React.ReactNode }) =>
    <pre className="my-2 overflow-x-auto rounded bg-page px-3 py-2 font-mono text-xs leading-relaxed text-ink">{children}</pre>,
  code: ({ className, children }: { className?: string; children?: React.ReactNode }) =>
    className?.startsWith('language-')
      ? <code className={className}>{children}</code>
      : <code className="rounded bg-page px-1 py-0.5 font-mono text-xs text-ink">{children}</code>,
  table: ({ children }: { children?: React.ReactNode }) => (
    <div className="my-2 overflow-x-auto rounded border border-edge">
      <table className="w-full border-collapse text-xs">{children}</table>
    </div>
  ),
  th: ({ children }: { children?: React.ReactNode }) =>
    <th scope="col" className="border-b border-edge px-2 py-1 text-left font-semibold text-ink-dim">{children}</th>,
  td: ({ children }: { children?: React.ReactNode }) =>
    <td className="border-b border-edge/40 px-2 py-1 align-top text-ink">{children}</td>,
  a: ({ href, children }: { href?: string; children?: React.ReactNode }) =>
    href
      ? <a href={href} target="_blank" rel="noopener noreferrer nofollow" className={LINK}>{children}</a>
      : <span className="text-ink-dim">{children}</span>,
  img: ({ alt, src }: { alt?: string; src?: string }) => (
    <span className="text-xs text-ink-faint">{alt ? `${alt} ` : ''}({String(src ?? '')})</span>
  ),
}

export function Markdown({ source }: { source: string }) {
  // NOT AN OPTIMISATION. The longest plan this store holds is 80,428 characters and the drawer
  // re-renders whenever the page behind it does.
  const plugins = useMemo(() => [remarkGfm], [])
  return (
    <ReactMarkdown remarkPlugins={plugins} urlTransform={safeHref} components={PIECES}>
      {source}
    </ReactMarkdown>
  )
}

const button =
  'rounded border border-edge px-2 py-0.5 text-2xs text-ink-dim transition-colors hover:text-ink'

/**
 * The chrome around a markdown body: which view, a copy, and a file.
 *
 * RAW IS THE GUARANTEE. It is the same `<pre>` this page had before anything was rendered, byte for
 * byte, so a reader who suspects the renderer can always see what the store holds. Copy and both
 * exports always take the SOURCE, never the rendered text: an export that quietly rewrote the
 * document would be the same defect as a renderer that dropped part of it.
 */
export function TextBody({
  source,
  name,
  boxClass,
  defaultView = 'markdown',
  actions,
  canExport = true,
}: {
  source: string
  /** The filename stem an export gets, before `safeName`. */
  name: string
  /** The height the caller has always owned, e.g. "max-h-[40vh]". */
  boxClass: string
  /** Raw for anything that is not a document: tool output, a listing, JSON. */
  defaultView?: 'markdown' | 'raw'
  /** Extra controls for the same row, such as "Read the whole plan". */
  actions?: React.ReactNode
  /** False while only part of the document is in hand, so nobody saves a prefix under its name. */
  canExport?: boolean
}) {
  const [view, setView] = useState<'markdown' | 'raw'>(defaultView)
  const [copied, setCopied] = useState(false)
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="View" className="inline-flex overflow-hidden rounded border border-edge">
          {(['markdown', 'raw'] as const).map((which) => (
            <button
              key={which}
              type="button"
              aria-pressed={view === which}
              onClick={() => setView(which)}
              className={
                'px-2 py-0.5 text-2xs transition-colors ' +
                (view === which ? 'bg-panel text-ink' : 'bg-page text-ink-dim hover:text-ink')
              }
            >
              {which === 'markdown' ? 'Markdown' : 'Raw'}
            </button>
          ))}
        </div>
        <button
          type="button"
          className={button}
          onClick={() => {
            void navigator.clipboard?.writeText(source)
              .then(() => setCopied(true))
              .catch(() => setCopied(false))
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
        {canExport ? (
          <>
            <button type="button" className={button} onClick={() => downloadMarkdown(name, source)}>
              .md
            </button>
            <button type="button" className={button} onClick={() => downloadTextFile(name, source)}>
              .txt
            </button>
          </>
        ) : null}
        {actions}
      </div>
      <div className={`${boxClass} overflow-auto rounded bg-page px-3 py-2`}>
        {view === 'markdown' ? (
          <Markdown source={source} />
        ) : (
          <pre className="font-mono text-xs leading-relaxed whitespace-pre-wrap text-ink">{source}</pre>
        )}
      </div>
    </div>
  )
}
