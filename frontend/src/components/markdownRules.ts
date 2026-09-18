/**
 * Which links a markdown body is allowed to make.
 *
 * NOTHING IS EVER CLICKED INTO A SCHEME WE DO NOT KNOW. The text comes from arbitrary transcripts,
 * including pages a session fetched, so only http, https and mailto become links; anything else
 * keeps its label and loses its anchor.
 *
 * There was a second rule here, a remark plugin turning every raw-HTML node back into text, written
 * against the claim that react-markdown deletes raw HTML. Measured against 10.1.0, it does not:
 * `<uuid>`, `<div>x</div>` and `<script>...</script>` all render as their literal characters with
 * and without the plugin, so the plugin was doing nothing and is gone. The property it was meant to
 * protect is pinned by a test instead, which is what would catch an upgrade that changed it.
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
