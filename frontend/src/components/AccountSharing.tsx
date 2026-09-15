import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '@/api'

/**
 * Show every account's chats, or only the signed-in account's.
 *
 * The desktop app separates accounts with a directory: `<account>/<org>/local_<uuid>.json`, and the
 * listing IS the session list. All makes every one of those directories on this machine resolve to
 * one of them, so whichever account is signed in reads the same chats. Current puts them back.
 *
 * THE NUMBERS ARE ON HOVER, not in the row. "33 chat(s) shared across 2 account directories" sat
 * beside the switch as a sentence; the user asked for the row to say less. All's hover carries
 * what All shows (every record in the shared directory), Current's what Current would show the
 * signed-in account (its own chats, derived on the server from the sharing backup), and the
 * label's hover carries the one instruction a switch needs: restart Claude, which reads these
 * directories when it starts. After a switch the label turns amber so the hover is noticed.
 *
 * NOTHING HAPPENS WHILE CLAUDE IS OPEN. A directory the app holds cannot be moved, and half a move
 * leaves an account pointing at an empty directory: the server answers 409 and the message below
 * is the one instruction that resolves it.
 */
function Problem({ error }: { error: unknown }) {
  const detail = error instanceof ApiError ? error.detail : undefined
  const said =
    detail && typeof detail === 'object' && 'error' in detail
      ? String((detail as { error: unknown }).error)
      : error instanceof Error
        ? error.message
        : String(error)
  return (
    <p className="mt-2 rounded-md border border-bad/40 bg-bad/5 px-3 py-2 text-sm text-bad">
      {said}
    </p>
  )
}

export const RESTART_NOTE =
  'Restart Claude for this to take effect. It reads these directories when it starts.'
export const QUIT_NOTE = 'Quit Claude before switching: a directory it has open cannot be moved.'

export function AccountSharing({
  writesEnabled,
  onChanged,
}: {
  writesEnabled: boolean
  onChanged?: () => void
}) {
  const client = useQueryClient()
  // READ AGAIN when the page comes back and whenever a sibling says something changed. The count
  // was read once on mount, so a sweep or a delete while the page sat open left it stale until a
  // reload. `refetchOnWindowFocus` overrides the app's global false for this one cheap call, and
  // every `client.invalidateQueries()` the other header controls fire reaches this key too.
  const query = useQuery({
    queryKey: ['accounts'],
    queryFn: api.accounts.state,
    refetchOnWindowFocus: true,
    retry: false,
  })
  const state = query.data ?? null
  const [busy, setBusy] = useState<'all' | 'current' | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [restart, setRestart] = useState(false)

  // ONE ACCOUNT IS NOT A CHOICE. With a single pair on the machine there is nothing to share and
  // the control would offer a toggle that changes nothing.
  if (!state || !state.supported || state.pairs < 2) return null

  const mode = state.intended === 'all' ? 'all' : 'current'

  async function choose(next: 'all' | 'current') {
    if (next === mode || busy) return
    setBusy(next)
    setError(null)
    setRestart(false)
    try {
      const report = await api.accounts.share(next)
      client.setQueryData(['accounts'], report.state)
      setRestart(report.restart_required)
      onChanged?.()
    } catch (problem) {
      setError(problem)
    } finally {
      setBusy(null)
    }
  }

  // EVERYTHING A SWITCH NEEDS TO KNOW IS ON THE TWO BUTTONS, one line each: what the side shows,
  // that Claude must be quit before switching, that it must be restarted after. The "ACCOUNT"
  // word that used to carry the restart note is gone, the user's choice: the buttons say what
  // they are.
  const notes = '\n' + QUIT_NOTE + '\n' + RESTART_NOTE
  const hover = {
    all: `Every account's chats: ${state.chats_visible} across ${state.pairs} account directories${notes}`,
    current:
      (state.current_chats === null || state.current_chats === undefined
        ? "Only the signed-in account's own chats: not known while sharing is on"
        : `Only the signed-in account's own chats: ${state.current_chats}`) + notes,
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <div
          role="group"
          aria-label="Account"
          data-restart={restart ? 'true' : 'false'}
          className={`inline-flex overflow-hidden rounded-md border ${
            restart ? 'border-warn/70' : 'border-edge'
          }`}
        >
          {(['all', 'current'] as const).map((option) => (
            <button
              key={option}
              type="button"
              disabled={!writesEnabled || busy !== null}
              aria-pressed={mode === option}
              title={hover[option]}
              onClick={() => void choose(option)}
              className={
                'px-2.5 py-1.5 text-sm transition-colors disabled:opacity-50 ' +
                (mode === option
                  ? 'bg-panel text-ink'
                  : 'bg-page text-ink-dim hover:text-ink')
              }
            >
              {option === 'all' ? 'All' : 'Current'}
            </button>
          ))}
        </div>
      </div>
      {error ? <Problem error={error} /> : null}
    </div>
  )
}
