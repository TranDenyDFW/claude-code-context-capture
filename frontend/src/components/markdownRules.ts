/**
 * The two rules this app applies to markdown before it is rendered.
 *
 * NOTHING IS EVER DROPPED. react-markdown does not escape raw HTML, it REMOVES it: with no
 * `rehype-raw` the `html` nodes produce no output at all, so a sentence reading
 * `/api/compaction/<uuid>` renders as `/api/compaction/`. Measured over the 726 markdown documents
 * this repo has collected under `.md/`, written by the same generator as the compaction summaries
 * and the plans: 358 such disappearances across 98 files (`<stdin>`, `<uuid>`, `<sid>`, `<title>`,
 * `<slug>`). Silently deleting stored text is the one thing a page for READING a store must never
 * do, so every `html` node becomes a `text` node carrying exactly what was written.
 *
 * NOTHING IS EVER CLICKED INTO A SCHEME WE DO NOT KNOW. The text comes from arbitrary transcripts,
 * so only http, https and mailto become links; anything else keeps its label and loses its anchor.
 */

/** A scheme allowlist. Returns '' for anything else, which leaves react-markdown no href to set. */
export function safeHref(url: string): string {
  const trimmed = url.trim()
  if (!trimmed) return ''
  // A relative path, a fragment or a query is same-document and safe.
  if (/^[#/?]/.test(trimmed)) return trimmed
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(trimmed)
  if (!scheme) return trimmed
  return ['http', 'https', 'mailto'].includes(scheme[1].toLowerCase()) ? trimmed : ''
}

interface Node {
  type?: string
  value?: string
  children?: Node[]
}

function isNode(value: unknown): value is Node {
  return typeof value === 'object' && value !== null
}

/**
 * A remark plugin: every raw-HTML node becomes the literal characters it was written with.
 *
 * `<uuid>` in a sentence is a placeholder somebody typed, not markup, and either way this page
 * shows what the store holds rather than interpreting it.
 */
export function remarkKeepRaw() {
  return (tree: unknown) => {
    const walk = (node: unknown) => {
      if (!isNode(node) || !Array.isArray(node.children)) return
      node.children = node.children.map((child) => {
        if (isNode(child) && (child.type === 'html' || child.type === 'inlineCode+html')) {
          return { type: 'text', value: String(child.value ?? '') }
        }
        walk(child)
        return child
      })
    }
    walk(tree)
  }
}
