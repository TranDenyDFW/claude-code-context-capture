import { useEffect, useState } from 'react'
import { api, ApiError } from '@/api'
import type { AccountsState } from '@/api'

/**
 * Show every account's chats, or only the signed-in account's.
 *
 * The desktop app separates accounts with a directory: `<account>/<org>/local_<uuid>.json`, and the
 * listing IS the session list. All makes every one of those directories on this machine resolve to
 * one of them, so whichever account is signed in reads the same chats. Current puts them back.
 *
 * NOTHING HAPPENS WHILE CLAUDE IS OPEN. A directory the app holds cannot be moved, and half a move
 * leaves an account pointing at an empty directory: the server answers 409 and the message below
 * is the one instruction that resolves it. A change that does land needs Claude restarted, because
 * the app reads these directories when it starts.
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

export function AccountSharing({
  writesEnabled,
  onChanged,
}: {
  writesEnabled: boolean
  onChanged?: () => void
}) {
  const [state, setState] = useState<AccountsState | null>(null)
  const [busy, setBusy] = useState<'all' | 'current' | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [restart, setRestart] = useState(false)

  useEffect(() => {
    let live = true
    api.accounts
      .state()
      .then((answer) => live && setState(answer))
      .catch(() => live && setState(null))
    return () => {
      live = false
    }
  }, [])

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
      setState(report.state)
      setRestart(report.restart_required)
      onChanged?.()
    } catch (problem) {
      setError(problem)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <span className="text-xs uppercase tracking-wide text-ink-faint">Account</span>
        <div className="inline-flex overflow-hidden rounded-md border border-edge">
          {(['all', 'current'] as const).map((option) => (
            <button
              key={option}
              type="button"
              disabled={!writesEnabled || busy !== null}
              aria-pressed={mode === option}
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
        <span className="text-xs text-ink-faint">
          {mode === 'all'
            ? `${state.chats_visible} chat(s) shared across ${state.pairs} account directories`
            : `${state.pairs} account directories, each with its own chats`}
        </span>
      </div>
      {state.app_running && writesEnabled ? (
        <p className="text-xs text-ink-faint">
          Quit Claude before switching: a directory it has open cannot be moved.
        </p>
      ) : null}
      {restart ? (
        <p className="rounded-md border border-edge bg-page px-3 py-2 text-sm text-ink-dim">
          Restart Claude for this to take effect. It reads these directories when it starts.
        </p>
      ) : null}
      {error ? <Problem error={error} /> : null}
    </div>
  )
}
