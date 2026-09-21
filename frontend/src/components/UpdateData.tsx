import { hoverFor, type Tone } from './harvest'
import { useHarvest } from './useHarvest'

/**
 * Update data: read what Claude has written since the last harvest into the store, from the page.
 *
 * THE USER'S WORDS, on learning the store could only be updated from a terminal: "there's no
 * button for this in the app?". The hooks harvest on every prompt and at session end, so the
 * store is usually seconds old; this is for when it is not (a machine that was off, a chat
 * running elsewhere, a catch-up after an import), and for saying how fresh it is, which the page
 * never did.
 *
 * ONE CLICK, NO DIALOG. The header's rule is that a control which closes or restarts Claude or
 * this server asks first (`Confirm`). This does neither: it is the same additive, incremental
 * read the hooks run all day, and a dialog on it would teach people to click through the dialogs
 * that matter. The hover says so, the way the other titles say "after asking".
 *
 * NOT THE LIVE TOGGLE. Live re-reads the STORE every five seconds; this reads new TRANSCRIPTS
 * into the store. A verb and a noun, no pressed state, never the green fill.
 *
 * `aria-disabled`, NEVER `disabled`. A focused button that disables itself loses focus to the
 * top of the page, and with no dialog to take it back a keyboard user would be thrown there
 * after every sub-second run.
 */

const TONES: Record<Tone, string> = {
  good: 'text-good',
  warn: 'text-warn',
  bad: 'text-bad',
  dim: 'text-ink-faint',
}

export function UpdateData({
  onChanged,
  pollMs,
  idleMs,
  noteMs,
}: {
  /** Called when a run that may have written has ended; the parent refetches everything. */
  onChanged?: () => void
  pollMs?: number
  idleMs?: number
  noteMs?: number
}) {
  const { status, busy, note, start, now } = useHarvest({ onChanged, pollMs, idleMs, noteMs })

  // Nothing until the server has said whether it can update: a button that might be off is
  // worse than a moment without one, and the other header controls wait the same way.
  if (!status) return null
  const off = !status.enabled
  const inert = off || busy

  return (
    <div
      role="group"
      aria-label="Store data"
      className="ml-1 flex flex-wrap items-center gap-2 border-l border-edge pl-3"
    >
      {/* BEFORE the button, so in this right-aligned row the control never moves under the
          pointer when words appear. Always mounted: a live region that arrives with its text
          is not announced. */}
      <span
        role="status"
        data-testid="update-note"
        className={note ? `min-w-0 max-w-[24rem] text-xs ${TONES[note.tone]}` : 'sr-only'}
      >
        {note?.text ?? ''}
      </span>
      <button
        type="button"
        aria-disabled={inert}
        aria-busy={busy}
        title={hoverFor(status, now)}
        onClick={() => {
          if (!inert) void start('incremental')
        }}
        className={
          'grid rounded-md border border-edge bg-panel px-2.5 py-1.5 text-sm text-ink-dim ' +
          `transition-colors ${inert ? 'cursor-not-allowed opacity-50' : 'hover:text-ink'}`
        }
      >
        {/* Both labels in one cell, so the width is the wider one's and nothing shifts. The one
            not shown is hidden from the accessibility tree too: jsdom, like a screen reader,
            does not apply `invisible`. */}
        <span className={`col-start-1 row-start-1 ${busy ? 'invisible' : ''}`} aria-hidden={busy}>
          Update data
        </span>
        <span className={`col-start-1 row-start-1 ${busy ? '' : 'invisible'}`} aria-hidden={!busy}>
          Updating…
        </span>
      </button>
    </div>
  )
}
