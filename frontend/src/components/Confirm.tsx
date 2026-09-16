import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { ApiError } from '@/api'
import { Dialog } from './Dialog'
import type { Outcome } from './restart'

/**
 * Ask before doing something that closes or restarts Claude, or this server; then do it, show
 * what happened, and hand the outcome back.
 *
 * THE USER'S RULE: "prompt the user to continue; for example, if changing to all from current,
 * it requires a reboot, prompt the user when they click all and only continue if they confirm."
 * So every control that needs Claude quit or restarted, or C4X restarted, opens this: the
 * question names exactly what will happen, Cancel does nothing at all, and only Continue runs
 * the action. While it runs the dialog stays (the backdrop click is ignored), a progress line
 * follows what `progress` reports each second (Claude running, closed, back), and the outcome
 * stays on screen until Close, so a relaunch that failed is read rather than missed.
 *
 * Built on `Dialog`, which the Adopt window uses too; opened from inside that window it sits
 * above it (`z-[60]`), and Escape inside it closes it alone (Dialog scopes Escape by DOM
 * containment).
 */
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

type Phase = 'ask' | 'running' | 'done'

export function Confirm({
  label,
  question,
  detail,
  actionLabel = 'Continue',
  action,
  progress,
  opener,
  onClose,
  z = 'z-[60]',
}: {
  /** The dialog's accessible name. */
  label: string
  /** What will happen, as a sentence ending in "Continue?". */
  question: string
  detail?: string
  actionLabel?: string
  /** The write, run only on Continue; resolves to what to show. A throw is shown as its message. */
  action: () => Promise<Outcome>
  /** Polled every second while the action runs; its answer is the progress line. */
  progress?: () => Promise<string>
  opener?: RefObject<HTMLElement | null>
  /** Called once: with the outcome after Close, with null after Cancel. */
  onClose: (outcome: Outcome | null) => void
  z?: string
}) {
  const [phase, setPhase] = useState<Phase>('ask')
  const [line, setLine] = useState<string>('')
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const go = useRef<HTMLButtonElement>(null)
  const closer = useRef<HTMLButtonElement>(null)

  // The progress line, while running.
  useEffect(() => {
    if (phase !== 'running' || !progress) return
    let live = true
    const ask = () =>
      progress()
        .then((text) => live && setLine(text))
        .catch(() => {})
    void ask()
    const timer = setInterval(() => void ask(), 1000)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [phase, progress])

  async function run() {
    setPhase('running')
    setLine('')
    let result: Outcome
    try {
      result = await action()
    } catch (problem) {
      result = { ok: false, text: said(problem) }
    }
    setOutcome(result)
    setPhase('done')
  }

  function close() {
    if (phase === 'running') return
    onClose(phase === 'done' ? outcome : null)
  }

  const button =
    'rounded-md border border-edge bg-page px-2.5 py-1.5 text-sm text-ink transition-colors ' +
    'hover:bg-edge/20 disabled:opacity-50'

  return (
    <Dialog
      label={label}
      onClose={close}
      busy={phase === 'running'}
      opener={opener}
      initialFocus={phase === 'done' ? closer : go}
      z={z}
      header={<h2 className="text-md font-semibold text-ink">{label}</h2>}
      footer={
        phase === 'done' ? (
          <button ref={closer} type="button" onClick={close} className={button}>
            Close
          </button>
        ) : (
          <>
            <button
              ref={go}
              type="button"
              disabled={phase === 'running'}
              onClick={() => void run()}
              className={button}
            >
              {phase === 'running' ? `${actionLabel}…` : actionLabel}
            </button>
            <button
              type="button"
              disabled={phase === 'running'}
              onClick={close}
              className={`${button} text-ink-dim hover:text-ink`}
            >
              Cancel
            </button>
          </>
        )
      }
    >
      <div className="flex flex-col gap-2 text-sm">
        {phase === 'done' && outcome ? (
          <p className={outcome.ok ? 'text-ink' : 'text-warn'} data-testid="confirm-outcome">
            {outcome.text}
          </p>
        ) : (
          <>
            <p className="text-ink">{question}</p>
            {detail ? <p className="text-xs text-ink-faint">{detail}</p> : null}
            {phase === 'running' ? (
              <p className="text-xs text-ink-dim" aria-live="polite" data-testid="confirm-progress">
                {line || 'Working…'}
              </p>
            ) : null}
          </>
        )}
      </div>
    </Dialog>
  )
}
