import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '@/api'
import type { AdoptReport, AdoptState, RetitleReport } from '@/api'

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

  // NOTHING TO OFFER, NOTHING SHOWN. A machine whose every chat has a record, or one with no
  // account to file a record under, gets no control at all.
  if (!state || !state.supported) return null
  const total = state.groups.reduce((n, g) => n + g.count, 0)
  const unnamed = state.untitled_adopted ?? 0
  // STAYS while a result is showing: after the last folder is adopted or the last record named the
  // refreshed state has nothing left, and the restart notice is the one thing the reader needs.
  if (total === 0 && state.cli_candidates === 0 && unnamed === 0 && !report && !named) return null

  const selected = state.groups
    .filter((g) => chosen.includes(g.cwd))
    .reduce((n, g) => n + g.count, 0)

  function toggle(cwd: string) {
    setChosen((now) => (now.includes(cwd) ? now.filter((c) => c !== cwd) : [...now, cwd]))
  }

  // Records a first build wrote without a name. The app shows each as "General coding session",
  // and the store has always had a real name for them.
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

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <span className="text-xs uppercase tracking-wide text-ink-faint">Adopt</span>
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((now) => !now)}
          className="rounded-md border border-edge bg-page px-2.5 py-1.5 text-sm text-ink-dim hover:text-ink"
        >
          {total > 0
            ? `${plural(total, 'chat')} on this machine that Claude has no record of`
            : unnamed > 0
              ? `${plural(unnamed, 'adopted chat')} without a name`
              : 'Adopted chats'}
        </button>
      </div>
      {open ? (
        <div className="flex flex-col gap-2 rounded-md border border-edge bg-page px-3 py-2">
          {unnamed > 0 ? (
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-ink-dim">
                {plural(unnamed, 'adopted chat')} {unnamed === 1 ? 'has' : 'have'} no name yet and{' '}
                {unnamed === 1 ? 'shows' : 'show'} as General coding session.
              </span>
              <button
                type="button"
                disabled={!writesEnabled || busy}
                onClick={() => void nameThem()}
                className="rounded-md border border-edge bg-panel px-2.5 py-1 text-sm text-ink disabled:opacity-50"
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
          <ul className="flex max-h-64 flex-col gap-1 overflow-y-auto">
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
              className="rounded-md border border-edge bg-panel px-2.5 py-1.5 text-sm text-ink disabled:opacity-50"
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
      ) : null}
    </div>
  )
}
