import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '@/api'
import type { AdoptReport, AdoptState, RetitleReport } from '@/api'
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
  const reviews = state.review_runs_to_name ?? 0
  const toName = unnamed + reviews
  // STAYS while a result is showing: after the last folder is adopted or the last record named the
  // refreshed state has nothing left, and the restart notice is the one thing the reader needs.
  if (total === 0 && state.cli_candidates === 0 && toName === 0 && !report && !named) return null

  const selected = state.groups
    .filter((g) => chosen.includes(g.cwd))
    .reduce((n, g) => n + g.count, 0)

  function toggle(cwd: string) {
    setChosen((now) => (now.includes(cwd) ? now.filter((c) => c !== cwd) : [...now, cwd]))
  }

  // Records a first build wrote without a name (the app shows each as "General coding session"),
  // and records of review runs still named after the reviewer's own prompt: the store has a real
  // name for every one of them.
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
      : toName > 0
        ? `${plural(toName, 'adopted chat')} ${reviews === 0 ? 'without a name' : 'to name'}`
        : 'Adopted chats'

  return (
    <div className="flex items-center gap-2">
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
            aria-label="Adopted chats"
            tabIndex={-1}
            className="fixed inset-y-0 right-0 z-30 flex w-full max-w-xl flex-col gap-3 overflow-y-auto
                       border-l border-edge bg-panel p-4 shadow-panel outline-none"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="text-md font-semibold text-ink">Adopted chats</h2>
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
            {toName > 0 ? (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-ink-dim">
                  {unnamed > 0
                    ? `${plural(unnamed, 'adopted chat')} ${unnamed === 1 ? 'has' : 'have'} no name ` +
                      `yet and ${unnamed === 1 ? 'shows' : 'show'} as General coding session.`
                    : null}
                  {unnamed > 0 && reviews > 0 ? ' ' : null}
                  {reviews > 0
                    ? `${plural(reviews, 'adopted chat')} ${reviews === 1 ? 'is' : 'are'} a ` +
                      `reviewer's reading of another chat and ${reviews === 1 ? 'takes' : 'take'} ` +
                      `the name Reviewer - <that chat>.`
                    : null}
                </span>
                <button
                  type="button"
                  disabled={!writesEnabled || busy}
                  onClick={() => void nameThem()}
                  className="rounded-md border border-edge bg-page px-2.5 py-1 text-sm text-ink disabled:opacity-50"
                >
                  Name {toName === 1 ? 'it' : 'them'}
                </button>
              </div>
            ) : null}
            {named && named.restart_required ? (
              <p className="rounded-md border border-edge bg-page px-3 py-2 text-sm text-ink-dim">
                Restart Claude to see the {plural(named.renamed.length, 'name')}. It reads these
                records when it starts.
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
