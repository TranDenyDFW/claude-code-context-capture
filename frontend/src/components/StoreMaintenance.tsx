import { useRef, useState } from 'react'
import { api, type HarvestOutcome } from '@/api'
import { Confirm } from './Confirm'
import { DOING, OUTCOMES_QUESTION, runsQuestion } from './harvest'
import type { Outcome } from './restart'
import { useHarvest } from './useHarvest'

/**
 * Store maintenance: the two one-off passes the harvester has, from the page.
 *
 * ON THE DIAGNOSTICS TAB, NOT IN THE HEADER. The header's rule is that it says less, and these
 * are things done once per store, after an upgrade. This is the tab about whether the capture is
 * healthy, and it already shows what the harvester did.
 *
 * THESE ASK FIRST, unlike Update data. Not because anything is closed or restarted (nothing is,
 * and the questions say so) but because one re-reads every transcript and the other changes what
 * the lists show. The fold runs its dry run before it asks, so the question carries the real
 * numbers, including the runs it can place nowhere.
 *
 * IN ORDER, AND NUMBERED. The fold matches a run to the shell call whose span it began inside,
 * and a span ends at the call's result time, which the first pass fills. A link made on a loose
 * span stays, so the order matters; when the dry run finds calls with no result time the
 * question says to go back.
 *
 * THE HEADER OWNS THE RELOAD. Update data's instance of `useHarvest` is always mounted and
 * reloads the page whenever the server names a finished job it had not accounted for, which is
 * how a pass started here reaches it (even one that ends before anything saw it running). So
 * this instance hands `onChanged` nothing: two owners would reload the page twice.
 */

function told(outcome: HarvestOutcome | null): Outcome {
  if (!outcome) return { ok: false, text: 'The outcome is not known: the server did not report this run back.' }
  return { ok: outcome.ok, text: outcome.sentence || outcome.short }
}

type Asking =
  | { kind: 'tool-outcomes' }
  | { kind: 'runs'; question: string; detail?: string }
  | null

export function StoreMaintenance({ pollMs }: { pollMs?: number }) {
  const { status, busy, start } = useHarvest({ pollMs })
  const [asking, setAsking] = useState<Asking>(null)
  const [checking, setChecking] = useState(false)
  const [said, setSaid] = useState<Outcome | null>(null)
  const outcomesButton = useRef<HTMLButtonElement>(null)
  const runsButton = useRef<HTMLButtonElement>(null)

  if (!status) return null
  const off = !status.enabled
  const inert = off || busy || checking

  async function preview() {
    if (inert) return
    setChecking(true)
    setSaid(null)
    const dry = await start('runs', true)
    setChecking(false)
    if (!dry || !dry.ok) {
      setSaid(told(dry))
      return
    }
    setAsking({ kind: 'runs', ...runsQuestion(dry.summary) })
  }

  const progress = () =>
    api.harvest.state().then((now) =>
      now.running && now.job
        ? `${DOING[now.job.kind] ?? 'Working'}: ${Math.round(now.job.elapsed_s)} s so far`
        : 'Finishing')

  const button =
    'rounded-md border border-edge bg-page px-2.5 py-1.5 text-sm text-ink-dim transition-colors ' +
    (inert ? 'cursor-not-allowed opacity-50' : 'hover:text-ink')

  return (
    <section
      aria-label="Store maintenance"
      className="mb-4 rounded-lg border border-edge bg-panel/50 p-4"
    >
      <h2 className="text-sm font-semibold text-ink">Store maintenance</h2>
      <p className="mt-1 text-xs text-ink-faint">
        Two passes a store needs once, after an upgrade that taught the harvester something new.
        Update data, in the header, is the everyday one. Each asks before it runs.
      </p>
      {off && (
        <p className="mt-2 text-xs text-warn" data-testid="maintenance-off">
          Off on this server: {status.why_not} {status.fix}
        </p>
      )}
      <ol className="mt-3 flex flex-col gap-2 text-sm">
        <li className="flex flex-wrap items-center gap-2">
          <span className="text-ink-faint">1.</span>
          <button
            ref={outcomesButton}
            type="button"
            aria-disabled={inert}
            onClick={() => {
              if (!inert) {
                setSaid(null)
                setAsking({ kind: 'tool-outcomes' })
              }
            }}
            className={button}
          >
            Record tool outcomes…
          </button>
          <span className="min-w-0 text-xs text-ink-faint">
            How each tool call ended and when, for rows from before the store kept those.
          </span>
        </li>
        <li className="flex flex-wrap items-center gap-2">
          <span className="text-ink-faint">2.</span>
          <button
            ref={runsButton}
            type="button"
            aria-disabled={inert}
            aria-busy={checking}
            onClick={() => void preview()}
            className={button}
          >
            {checking ? 'Checking…' : 'Fold headless runs…'}
          </button>
          <span className="min-w-0 text-xs text-ink-faint">
            One-shots a chat's command or a harness started, folded under that chat or its project.
          </span>
        </li>
      </ol>
      <p
        role="status"
        data-testid="maintenance-note"
        className={said ? `mt-2 text-xs ${said.ok ? 'text-good' : 'text-bad'}` : 'sr-only'}
      >
        {said ? (said.ok ? said.text : `Failed: ${said.text}`) : ''}
      </p>
      {asking?.kind === 'tool-outcomes' && (
        <Confirm
          label="Record tool outcomes"
          question={OUTCOMES_QUESTION}
          actionLabel="Record"
          opener={outcomesButton}
          progress={progress}
          action={async () => told(await start('tool-outcomes'))}
          onClose={(outcome) => {
            setAsking(null)
            if (outcome) setSaid(outcome)
          }}
          z="z-50"
        />
      )}
      {asking?.kind === 'runs' && (
        <Confirm
          label="Fold headless runs"
          question={asking.question}
          detail={asking.detail}
          actionLabel="Fold"
          opener={runsButton}
          progress={progress}
          action={async () => told(await start('runs'))}
          onClose={(outcome) => {
            setAsking(null)
            if (outcome) setSaid(outcome)
          }}
          z="z-50"
        />
      )}
    </section>
  )
}
