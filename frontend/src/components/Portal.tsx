import { createPortal } from 'react-dom'
import type { ReactNode } from 'react'

/**
 * Render children at the end of `document.body`.
 *
 * THE HEADER IS A CONTAINING BLOCK. It has `backdrop-blur`, and a backdrop filter makes an element
 * the containing block for its `position: fixed` descendants, the same way a transform does.
 * Measured in the live page: a fixed element rendered inside the header was clipped to the
 * header's box (bottom 157 px, right 1270 px of a 1280 px viewport). The project mover's
 * `fixed inset-0` backdrop therefore covered only the header strip while its dialog declared
 * `aria-modal`, and the adopt drawer rendered from the header would have been a strip too. A
 * portal takes the element out from under the header; React keeps the component tree, so state,
 * handlers and the accessibility tree behave as if nothing moved.
 */
export function Portal({ children }: { children: ReactNode }) {
  return createPortal(children, document.body)
}
