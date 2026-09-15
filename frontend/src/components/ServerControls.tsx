import { useState } from 'react'
import { api, ApiError } from '@/api'

/**
 * Stop C4X and Restart C4X: the server that serves this page, from the page.
 *
 * STOP ASKS FIRST. Once the server is gone this page answers nothing, and the SessionStart hook
 * starts a server again only with the next Claude session, so the line saying so is shown before
 * the click that does it. Restart does not ask: the page is back within seconds.
 *
 * RESTART WAITS FOR A DIFFERENT PROCESS. The server answers the restart, starts its replacement
 * and exits a quarter of a second later; polling `health` until it answers would be satisfied
 * by the old server on the first poll. `health` carries the process id, and the page waits for
 * one that is not the id it read before asking. A reply cut on its way out (the fetch rejecting
 * with no status) is not a failure, it is the restart working; a refusal with a status is shown.
 */
type Phase = 'idle' | 'confirm' | 'stopping' | 'stopped' | 'restarting' | 'back' | 'failed'

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

function said(problem: unknown): string {
  if (problem instanceof ApiError) {
    const detail = problem.detail
    if (detail && typeof detail === 'object' && 'error' in detail) {
      return String((detail as { error: unknown }).error)
    }
    return problem.message
  }
  return problem instanceof Error ? problem.message : String(problem)
}

export function ServerControls({
  onRestarted,
  pollMs = 1_000,
  waitMs = 60_000,
}: {
  onRestarted?: () => void
  /** How often the page asks `health` while a restart is under way. */
  pollMs?: number
  /** How long a restart may take before the page gives up waiting. */
  waitMs?: number
}) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [why, setWhy] = useState<string | null>(null)
  const busy = phase === 'stopping' || phase === 'restarting'

  async function stop() {
    setPhase('stopping')
    setWhy(null)
    try {
      await api.server.stop()
      setPhase('stopped')
    } catch (problem) {
      setWhy(said(problem))
      setPhase('failed')
    }
  }

  async function restart() {
    setPhase('restarting')
    setWhy(null)
    let before: number | null = null
    try {
      before = (await api.health()).pid ?? null
    } catch {
      before = null
    }
    try {
      await api.server.restart()
    } catch (problem) {
      if (problem instanceof ApiError) {
        setWhy(said(problem))
        setPhase('failed')
        return
      }
      // No status: the reply was cut by the exit. The replacement is on its way.
    }
    const deadline = Date.now() + waitMs
    while (Date.now() < deadline) {
      await sleep(pollMs)
      try {
        const now = await api.health()
        if (now.ok && now.pid !== before) {
          setPhase('back')
          onRestarted?.()
          return
        }
      } catch {
        // Not back yet.
      }
    }
    setWhy('C4X did not come back in time; the next Claude session starts it again')
    setPhase('failed')
  }

  const button =
    'rounded-md border border-edge bg-panel px-2.5 py-1.5 text-sm text-ink-dim transition-colors ' +
    'hover:text-ink disabled:opacity-50'

  if (phase === 'confirm') {
    return (
      <div role="group" aria-label="C4X server" className="ml-1 border-l border-edge pl-3 flex flex-wrap items-center gap-2 text-sm">
        <span className="text-ink-dim">
          Stop C4X? This page stops answering until the next Claude session starts it again.
        </span>
        <button type="button" onClick={() => void stop()} className={button}>
          Stop
        </button>
        <button type="button" onClick={() => setPhase('idle')} className={button}>
          Cancel
        </button>
      </div>
    )
  }
  if (phase === 'stopped') {
    return (
      <div role="group" aria-label="C4X server" className="ml-1 border-l border-edge pl-3 text-sm text-ink-dim">
        C4X stopped. The next Claude session starts it again.
      </div>
    )
  }
  return (
    <div role="group" aria-label="C4X server" className="ml-1 border-l border-edge pl-3 flex flex-wrap items-center gap-2">
      <button
        type="button"
        disabled={busy}
        onClick={() => setPhase('confirm')}
        title="Stop the server that serves this page. The next Claude session starts it again."
        className={button}
      >
        Stop C4X
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => void restart()}
        title="Start a fresh server with the same settings, then reload this page's data."
        className={button}
      >
        {phase === 'restarting' ? 'Restarting C4X…' : 'Restart C4X'}
      </button>
      {phase === 'back' ? <span className="text-xs text-good">C4X is back.</span> : null}
      {phase === 'failed' && why ? <span className="text-xs text-bad">{why}</span> : null}
    </div>
  )
}
