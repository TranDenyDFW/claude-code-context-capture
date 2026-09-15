import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '@/api'
import type { AdoptReport, AdoptState, RetitleReport, SweepState, UnadoptReport } from '@/api'
import { Portal } from './Portal'

/**
 * Give Claude a record for the chats it has no record of.
 *
 * The desktop app lists a chat because a `local_<uuid>.json` names it under the signed-in
 * account's directory, and it writes that file itself. A reinstall leaves every transcript and
 * none of the records, so the sidebar shows a cloud list pointing at a device that no longer
 * exists. This writes the record from the store, the same write an import makes, for the folders
 * chosen here and nowhere else.
 *
 * NOTHING IS PRESELECTED. On the machine this was written on 923 chats qualified, and the app
 * leaves a `deleted_<uuid>` marker when a chat is deleted on purpose: a deleted chat and a
 * reinstall orphan look the same to the rule, so the number of markers is said out loud and the
 * choice is per folder. Claude may stay open: these are new files, read when it next starts.
 *
 * A BUTTON IN THE HEADER, A DRAWER FOR THE REST. The header keeps one button saying what there is
 * to do; everything else opens beside the page, in a non-modal dialog like the Inspector (Escape,
 * a close button, focus in on open and back on the button on close; no backdrop and no Tab trap,
 * for the reasons in `Inspector.tsx`). Rendered through a portal, because the header's backdrop
 * filter would clip a fixed drawer to the header's own box (see `Portal.tsx`).
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

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

export function AdoptSessions({
  writesEnabled,
  onChanged,
}: {
  writesEnabled: boolean
  onChanged?: () => void
}) {
  const [state, setState] = useState<AdoptState | null>(null)
  const [includeCli, setIncludeCli] = useState(false)
  const [chosen, setChosen] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [report, setReport] = useState<AdoptReport | null>(null)
  const [named, setNamed] = useState<RetitleReport | null>(null)
  const [unadopted, setUnadopted] = useState<UnadoptReport | null>(null)
  const [sweep, setSweep] = useState<SweepState | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)

  const refresh = useCallback(
    (cli: boolean) =>
      api.adopt
        .state(cli)
        .then((answer) => setState(answer))
        .catch(() => setState(null)),
    [],
  )

  useEffect(() => {
    let live = true
    api.adopt
      .state(includeCli)
      .then((answer) => live && setState(answer))
      .catch(() => live && setState(null))
    return () => {
      live = false
    }
  }, [includeCli])

  // What the server did at startup about review runs: one line in the drawer, read once. A
  // server that cannot say (an older one, a failed read) shows no line rather than a wrong one.
  useEffect(() => {
    let live = true
    api.adopt
      .sweep()
      .then((answer) => live && setSweep(answer))
      .catch(() => live && setSweep(null))
    return () => {
      live = false
    }
  }, [])

  // Focus goes into the drawer when it opens and back to the button when it closes, which is what
  // opening and closing a dialog mean. Escape only: a non-modal dialog must not swallow Tab.
  useEffect(() => {
    if (!open) return
    panel.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      trigger.current?.focus()
    }
  }, [open])

  // NOTHING TO OFFER, NOTHING SHOWN. A machine whose every chat has a record, or one with no
  // account to file a record under, gets no control at all.
  if (!state || !state.supported) return null
  const total = state.groups.reduce((n, g) => n + g.count, 0)
  const unnamed = state.untitled_adopted ?? 0
  const reviewRecords = state.review_records ?? 0
  const reviewRuns = state.review_runs ?? 0
  // STAYS while a result is showing: after the last folder is adopted or the last record named the
  // refreshed state has nothing left, and the restart notice is the one thing the reader needs.
  if (total === 0 && state.cli_candidates === 0 && unnamed === 0 && reviewRecords === 0
      && !report && !named && !unadopted) return null

  const selected = state.groups
    .filter((g) => chosen.includes(g.cwd))
    .reduce((n, g) => n + g.count, 0)

  function toggle(cwd: string) {
    setChosen((now) => (now.includes(cwd) ? now.filter((c) => c !== cwd) : [...now, cwd]))
  }

  // Records a first build wrote without a name: the app shows each as "General coding session",
  // and the store has a real name for every one of them.
  async function nameThem() {
    if (busy) return
    setBusy(true)
    setError(null)
    setNamed(null)
    try {
      const answer = await api.adopt.retitle()
      setNamed(answer)
      await refresh(includeCli)
      onChanged?.()
    } catch (problem) {
      setError(problem)
    } finally {
      setBusy(false)
    }
  }

  // Records a first build wrote for review runs. A run folds into the chat it reviewed and is
  // no chat of its own; the app listed 58 of them on the test laptop as if they were.
  async function takeBackReviews() {
    if (busy) return
    setBusy(true)
    setError(null)
    setUnadopted(null)
    try {
      const answer = await api.adopt.unadoptReviews()
      setUnadopted(answer)
      await refresh(includeCli)
      onChanged?.()
    } catch (problem) {
      setError(problem)
    } finally {
      setBusy(false)
    }
  }

  async function adoptNow() {
    if (!selected || busy) return
    setBusy(true)
    setError(null)
    setReport(null)
    try {
      const answer = await api.adopt.run({ cwds: chosen, include_cli: includeCli, dry_run: false })
      setReport(answer)
      setChosen([])
      await refresh(includeCli)
      onChanged?.()
    } catch (problem) {
      setError(problem)
    } finally {
      setBusy(false)
    }
  }

  const label =
    total > 0
      ? `${plural(total, 'chat')} on this machine that Claude has no record of`
      : unnamed > 0
        ? `${plural(unnamed, 'adopted chat')} without a name`
        : reviewRecords > 0
          ? `${plural(reviewRecords, 'review run')} still in Claude`
          : 'Adopted Chats'

  return (
    <div className="ml-1 flex items-center gap-2 border-l border-edge pl-3">
      <span className="text-xs uppercase tracking-wide text-ink-faint">Adopt</span>
      <button
        ref={trigger}
        type="button"
        aria-expanded={open}
        aria-controls="adopt-drawer"
        onClick={() => setOpen((now) => !now)}
        className="rounded-md border border-edge bg-page px-2.5 py-1.5 text-sm text-ink-dim hover:text-ink"
      >
        {label}
      </button>
      {open ? (
        <Portal>
          <div
            ref={panel}
            id="adopt-drawer"
            role="dialog"
            aria-label="Adopted Chats"
            tabIndex={-1}
            className="fixed inset-y-0 right-0 z-30 flex w-full max-w-xl flex-col gap-3 overflow-y-auto
                       border-l border-edge bg-panel p-4 shadow-panel outline-none"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="text-md font-semibold text-ink">Adopted Chats</h2>
                <p className="text-2xs text-ink-faint">
                  Records for the chats Claude has no record of, and names for the ones c4x wrote.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close"
                title="Close (Esc)"
                className="rounded border border-edge px-2 py-1 text-xs text-ink-dim hover:text-ink"
              >
                ×
              </button>
            </div>
            {unnamed > 0 ? (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-ink-dim">
                  {`${plural(unnamed, 'adopted chat')} ${unnamed === 1 ? 'has' : 'have'} no name ` +
                    `yet and ${unnamed === 1 ? 'shows' : 'show'} as General coding session.`}
                </span>
                <button
                  type="button"
                  disabled={!writesEnabled || busy}
                  onClick={() => void nameThem()}
                  className="rounded-md border border-edge bg-page px-2.5 py-1 text-sm text-ink disabled:opacity-50"
                >
                  Name {unnamed === 1 ? 'it' : 'them'}
                </button>
              </div>
            ) : null}
            {named && named.restart_required ? (
              <p className="rounded-md border border-edge bg-page px-3 py-2 text-sm text-ink-dim">
                Restart Claude to see the {plural(named.renamed.length, 'name')}. It reads these
                records when it starts.
              </p>
            ) : null}
            {reviewRecords > 0 ? (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-ink-dim">
                  {`${plural(reviewRecords, 'review run')} ${reviewRecords === 1 ? 'is' : 'are'} ` +
                    'listed in Claude as a chat. Each is a reviewer’s reading of another chat and ' +
                    'belongs under it, not beside it.'}
                </span>
                <button
                  type="button"
                  disabled={!writesEnabled || busy}
                  onClick={() => void takeBackReviews()}
                  className="rounded-md border border-edge bg-page px-2.5 py-1 text-sm text-ink disabled:opacity-50"
                >
                  Remove {reviewRecords === 1 ? 'it' : 'them'} from Claude
                </button>
              </div>
            ) : null}
            {unadopted && unadopted.restart_required ? (
              <p className="rounded-md border border-edge bg-page px-3 py-2 text-sm text-ink-dim">
                Restart Claude to drop the {plural(unadopted.removed.length, 'review run')} from
                its list. It reads these records when it starts.
              </p>
            ) : null}
            {reviewRuns > 0 ? (
              <p className="text-xs text-ink-faint">
                {plural(reviewRuns, 'review run')} on this machine {reviewRuns === 1 ? 'is' : 'are'}{' '}
                folded into the {reviewRuns === 1 ? 'chat it' : 'chats they'} reviewed and{' '}
                {reviewRuns === 1 ? 'is' : 'are'} not offered.
              </p>
            ) : null}
            {sweep ? (
              <p className="text-xs text-ink-faint" data-testid="startup-sweep">
                {!sweep.enabled
                  ? 'The startup sweep is off on this server: review-run records are taken back ' +
                    'only from here, and Claude is never restarted by c4x.'
                  : sweep.last
                    ? `Startup sweep at ${sweep.last.at.replace('T', ' ').replace('Z', ' UTC')}: ` +
                      `${sweep.last.why}.`
                    : 'No startup sweep has run on this store yet. Each server started with ' +
                      'Claude takes back review-run records and restarts Claude when it removed any.'}
              </p>
            ) : null}
            {(state.deleted_in_app ?? 0) > 0 ? (
              <p className="text-xs text-ink-faint">
                {plural(state.deleted_in_app ?? 0, 'chat')} you deleted in Claude{' '}
                {state.deleted_in_app === 1 ? 'is' : 'are'} left out of every list here;{' '}
                {state.deleted_in_app === 1 ? 'its transcript stays' : 'their transcripts stay'} on
                disk.
              </p>
            ) : null}
            {state.deleted_markers > 0 ? (
              <p className="text-xs text-ink-faint">
                Claude has deleted {plural(state.deleted_markers, 'chat')} from this account; their
                records are gone and they cannot be told from the chats below, so adopt only folders
                you recognise.
              </p>
            ) : null}
            <div className="flex flex-wrap items-center gap-3 text-xs">
              <button
                type="button"
                className="text-ink-dim underline hover:text-ink"
                onClick={() => setChosen(state.groups.map((g) => g.cwd))}
              >
                Select all
              </button>
              <button
                type="button"
                className="text-ink-dim underline hover:text-ink"
                onClick={() => setChosen([])}
              >
                None
              </button>
              <label className="flex items-center gap-1 text-ink-dim">
                <input
                  type="checkbox"
                  checked={includeCli}
                  onChange={(e) => setIncludeCli(e.target.checked)}
                />
                Include CLI and SDK sessions ({state.cli_candidates})
              </label>
            </div>
            <ul className="flex flex-col gap-1">
              {state.groups.map((g) => (
                <li key={g.cwd}>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={chosen.includes(g.cwd)}
                      onChange={() => toggle(g.cwd)}
                    />
                    <span className="text-ink">{g.project}</span>
                    <span className="text-xs text-ink-faint">
                      {plural(g.count, 'chat')}, newest {g.newest.slice(0, 10)}
                    </span>
                    <span className="truncate text-xs text-ink-faint" title={g.cwd}>
                      {g.cwd}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                disabled={!writesEnabled || busy || selected === 0}
                onClick={() => void adoptNow()}
                className="rounded-md border border-edge bg-page px-2.5 py-1.5 text-sm text-ink disabled:opacity-50"
              >
                Adopt {plural(selected, 'chat')}
              </button>
              {state.other_account > 0 ? (
                <span className="text-xs text-ink-faint">
                  {plural(state.other_account, 'chat')} belong to another account and are left alone
                </span>
              ) : null}
            </div>
            {report && report.restart_required ? (
              <p className="rounded-md border border-edge bg-page px-3 py-2 text-sm text-ink-dim">
                Restart Claude to see the {plural(report.written.length, 'chat')} adopted. It reads
                these records when it starts.
              </p>
            ) : null}
            {report?.note ? <p className="text-xs text-ink-faint">{report.note}</p> : null}
            {error ? <Problem error={error} /> : null}
          </div>
        </Portal>
      ) : null}
    </div>
  )
}
