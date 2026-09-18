import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '@/api'
import type { AdoptGroup, AdoptReport, AdoptState, RetitleReport, SweepState, UnadoptReport } from '@/api'
import { Confirm } from './Confirm'
import { Dialog } from './Dialog'
import { matches } from './Palette'
import { ResizeHandle } from './ResizeHandle'
import { COLUMN } from './columnWidths'
import { restartOutcome, restartQuestion } from './restart'
import type { Outcome } from './restart'

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
 * A BUTTON IN THE HEADER, A WINDOW FOR THE REST. The header keeps one button saying what there
 * is to do; everything else opens in a window over the page (`Dialog.tsx`, the project dialog's
 * chrome: the page dimmed behind it, Escape or the backdrop to close, focus in and back). The
 * user's choice: the side drawer this used to be "looks so stuffy on the side". The folders are
 * a TABLE, one row each, with a search box that narrows it by folder name, path and chat title
 * as you type; a folder whose chat title matched opens to show its chats, so the row says why
 * it is there.
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

/** What the search box reads: the folder's name, its full path, and every chat title in it. */
const haystack = (g: AdoptGroup) =>
  [g.project, g.cwd, ...g.sessions.map((s) => s.title)].join(' ')

/**
 * What each column starts at, and the reader can drag any of them.
 *
 * WRITTEN DOWN RATHER THAN MEASURED, unlike the main table's: this one has six declared columns
 * whose content is known (a folder name, two counts, a date, a path), so an estimate would be
 * arithmetic over a shape that never changes. Not remembered either: this is a window somebody
 * opens to adopt a few folders, not a table they live in.
 */
const ADOPT_WIDTHS: Record<string, number> = {
  adopt: 56, project: 260, chats: 72, newest: 132, runs: 72, path: 320,
}

const cell = 'px-2 py-1.5 align-top'
const head = 'border-b border-edge px-2 py-2 text-left text-xs font-medium text-ink-faint'

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
  const [widths, setWidths] = useState<Record<string, number>>(ADOPT_WIDTHS)

  // The columns this window draws, in order. `Runs` appears only when the server sends the field,
  // so the list is built once and the header, the colgroup and the width map all read it.
  const adoptColumns = (runs: boolean) => [
    { id: 'adopt', label: 'Adopt', right: false },
    { id: 'project', label: 'Folder', right: false },
    { id: 'chats', label: 'Chats', right: true },
    { id: 'newest', label: 'Newest', right: false },
    ...(runs ? [{ id: 'runs', label: 'Runs', right: true }] : []),
    { id: 'path', label: 'Path', right: false },
  ]
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [report, setReport] = useState<AdoptReport | null>(null)
  const [named, setNamed] = useState<RetitleReport | null>(null)
  const [unadopted, setUnadopted] = useState<UnadoptReport | null>(null)
  const [sweep, setSweep] = useState<SweepState | null>(null)
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<string[]>([])
  // WHICH WRITE IS BEING ASKED ABOUT. Each of the three writes a file Claude reads when it
  // starts, so each asks first and the server restarts Claude after (the user's rule).
  const [asking, setAsking] = useState<'adopt' | 'name' | 'take' | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const search = useRef<HTMLInputElement>(null)

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

  // What the server did at startup about review runs: one line in the window, read once. A
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

  // NOTHING TO OFFER, NOTHING SHOWN. A machine whose every chat has a record, or one with no
  // account to file a record under, gets no control at all.
  if (!state || !state.supported) return null
  const total = state.groups.reduce((n, g) => n + g.count, 0)
  const unnamed = state.untitled_adopted ?? 0
  const reviewRuns = state.review_runs ?? 0
  // THE RECORDS TO TAKE BACK: an earlier build's records for review runs and for child runs,
  // one number, since one button takes both back.
  const runRecords = state.run_records ?? 0
  const reviewRecords = (state.review_records ?? 0) + runRecords
  // "review run" while only reviews are there to take back, the wording the page has had; "run"
  // once child runs are among them, since a review run is a run too.
  const recordWord = runRecords > 0 ? 'run' : 'review run'
  const runsPlaced = state.runs_placed ?? 0
  const runsBatched = state.runs_batched ?? 0
  const runsUnplaced = state.runs_unplaced ?? 0
  // STAYS while a result is showing: after the last folder is adopted or the last record named the
  // refreshed state has nothing left, and the restart notice is the one thing the reader needs.
  if (total === 0 && state.cli_candidates === 0 && unnamed === 0 && reviewRecords === 0
      && !report && !named && !unadopted) return null

  const selected = state.groups
    .filter((g) => chosen.includes(g.cwd))
    .reduce((n, g) => n + g.count, 0)

  // THE SEARCH. Every term must appear somewhere in the folder's name, path or chat titles (the
  // palette's rule, so "secdb 08-31" works here too). A folder that is in the table only because
  // a chat title matched is opened, so the row shows the title that put it there.
  const needle = query.trim()
  const shown = needle ? state.groups.filter((g) => matches(haystack(g), needle)) : state.groups
  const titleHit = (g: AdoptGroup) =>
    needle !== '' && !matches(`${g.project} ${g.cwd}`, needle)
      && g.sessions.some((s) => matches(s.title, needle))
  const isOpen = (g: AdoptGroup) => expanded.includes(g.cwd) || titleHit(g)
  const hasRuns = state.groups.some((g) => typeof g.runs === 'number')
  const shownColumns = adoptColumns(hasRuns)
  const columns = hasRuns ? 6 : 5

  function toggle(cwd: string) {
    setChosen((now) => (now.includes(cwd) ? now.filter((c) => c !== cwd) : [...now, cwd]))
  }

  function toggleOpen(cwd: string) {
    setExpanded((now) => (now.includes(cwd) ? now.filter((c) => c !== cwd) : [...now, cwd]))
  }

  function close() {
    setOpen(false)
    setQuery('')
  }

  // Records a first build wrote without a name: the app shows each as "General coding session",
  // and the store has a real name for every one of them.
  async function nameThem(): Promise<Outcome> {
    setBusy(true)
    setError(null)
    setNamed(null)
    try {
      const answer = await api.adopt.retitle(true)
      setNamed(answer)
      await refresh(includeCli)
      onChanged?.()
      return restartOutcome(answer.restart, `${plural(answer.renamed.length, 'record')} named.`)
    } catch (problem) {
      setError(problem)
      throw problem
    } finally {
      setBusy(false)
    }
  }

  // Records a first build wrote for review runs and child runs. A run folds into the chat it
  // reviewed or that spawned it and is no chat of its own; the app listed 58 of them on the
  // test laptop as if they were.
  async function takeBackReviews(): Promise<Outcome> {
    setBusy(true)
    setError(null)
    setUnadopted(null)
    try {
      const answer = await api.adopt.unadoptReviews(true)
      setUnadopted(answer)
      await refresh(includeCli)
      onChanged?.()
      return restartOutcome(answer.restart,
                            `${plural(answer.removed.length, 'record')} taken back.`)
    } catch (problem) {
      setError(problem)
      throw problem
    } finally {
      setBusy(false)
    }
  }

  async function adoptNow(): Promise<Outcome> {
    setBusy(true)
    setError(null)
    setReport(null)
    try {
      const answer = await api.adopt.run({ cwds: chosen, include_cli: includeCli, dry_run: false,
                                           restart: true })
      setReport(answer)
      setChosen([])
      await refresh(includeCli)
      onChanged?.()
      return restartOutcome(answer.restart,
                            `${plural(answer.written.length, 'record')} written.`)
    } catch (problem) {
      setError(problem)
      throw problem
    } finally {
      setBusy(false)
    }
  }

  const ask = {
    adopt: {
      label: 'Adopt chats',
      does: `writes ${plural(selected, 'record')} for the ${plural(selected, 'chat')} ticked`,
      action: adoptNow,
    },
    name: {
      label: 'Name the records',
      does: `names the ${plural(unnamed, 'record')} that ${unnamed === 1 ? 'has' : 'have'} no name`,
      action: nameThem,
    },
    take: {
      label: 'Remove the runs from Claude',
      does: `takes back the ${plural(reviewRecords, 'record')} written for runs`,
      action: takeBackReviews,
    },
  }

  // THE BUTTON SAYS "Adopt (N)" AND THE HOVER SAYS WHY, the user's choice: the sentence that was
  // the button's text ran to 60 characters and moved the whole header row. N is the number of
  // chats the window offers; what else the window can do is the hover.
  const hint =
    total > 0
      ? `${plural(total, 'chat')} on this machine that Claude has no record of`
      : unnamed > 0
        ? `${plural(unnamed, 'adopted chat')} without a name`
        : reviewRecords > 0
          ? `${plural(reviewRecords, recordWord)} still in Claude`
          : 'Adopted Chats'

  const link = 'text-ink-dim underline hover:text-ink'
  const button =
    'rounded-md border border-edge bg-page px-2.5 py-1.5 text-sm text-ink disabled:opacity-50'

  return (
    <div className="ml-1 flex items-center gap-2 border-l border-edge pl-3">
      <button
        ref={trigger}
        type="button"
        aria-expanded={open}
        aria-controls="adopt-window"
        title={hint}
        onClick={() => (open ? close() : setOpen(true))}
        className="rounded-md border border-edge bg-page px-2.5 py-1.5 text-sm text-ink-dim hover:text-ink"
      >
        Adopt ({total})
      </button>
      {open ? (
        <Dialog
          id="adopt-window"
          label="Adopted Chats"
          width="max-w-[60rem]"
          onClose={close}
          busy={busy}
          opener={trigger}
          initialFocus={search}
          header={
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="text-md font-semibold text-ink">Adopted Chats</h2>
                <p className="mt-0.5 text-xs text-ink-faint">
                  Records for the chats Claude has no record of, and names for the ones c4x wrote.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  ref={search}
                  type="search"
                  value={query}
                  aria-label="Search folders and chats"
                  placeholder="Search folders, paths and chat titles"
                  onChange={(event) => setQuery(event.target.value)}
                  // Escape clears the search first; the window closes only when there is
                  // nothing to clear (the key then reaches the window's own handler).
                  onKeyDown={(event) => {
                    if (event.key === 'Escape' && query !== '') {
                      event.stopPropagation()
                      setQuery('')
                    }
                  }}
                  className="w-64 rounded-md border border-edge bg-page px-2.5 py-1.5 text-sm text-ink
                             outline-none placeholder:text-ink-faint focus:border-edge-bright"
                />
                <span className="text-xs text-ink-faint" aria-live="polite">
                  {needle ? `${shown.length} of ${plural(state.groups.length, 'folder')} match` : ''}
                </span>
              </div>
            </div>
          }
          footer={
            <>
              {state.other_account > 0 ? (
                <span className="mr-auto text-xs text-ink-faint">
                  {plural(state.other_account, 'chat')} belong to another account and are left alone
                </span>
              ) : null}
              <button
                type="button"
                disabled={!writesEnabled || busy || selected === 0}
                onClick={() => setAsking('adopt')}
                className={button}
              >
                Adopt {plural(selected, 'chat')}
              </button>
              <button type="button" onClick={close} className={`${button} text-ink-dim hover:text-ink`}>
                Close
              </button>
            </>
          }
        >
          {asking ? (
            <Confirm
              label={ask[asking].label}
              question={restartQuestion(state.app_running, ask[asking].does, false)}
              action={ask[asking].action}
              progress={state.app_running
                ? () => api.adopt.state(includeCli)
                    .then((now) => (now.app_running ? 'Claude is running' : 'Claude is closed'))
                : undefined}
              opener={search}
              onClose={() => setAsking(null)}
            />
          ) : null}
          <div className="flex flex-col gap-3">
            {unnamed > 0 ? (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-ink-dim">
                  {`${plural(unnamed, 'adopted chat')} ${unnamed === 1 ? 'has' : 'have'} no name ` +
                    `yet and ${unnamed === 1 ? 'shows' : 'show'} as General coding session.`}
                </span>
                <button
                  type="button"
                  disabled={!writesEnabled || busy}
                  onClick={() => setAsking('name')}
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
                  {`${plural(reviewRecords, recordWord)} ${reviewRecords === 1 ? 'is' : 'are'} ` +
                    'listed in Claude as a chat. Each is a reviewer’s reading of another chat' +
                    (runRecords > 0 ? ', or a one-shot a chat’s command spawned,' : '') +
                    ' and belongs under it, not beside it.'}
                </span>
                <button
                  type="button"
                  disabled={!writesEnabled || busy}
                  onClick={() => setAsking('take')}
                  className="rounded-md border border-edge bg-page px-2.5 py-1 text-sm text-ink disabled:opacity-50"
                >
                  Remove {reviewRecords === 1 ? 'it' : 'them'} from Claude
                </button>
              </div>
            ) : null}
            {unadopted && unadopted.restart_required ? (
              <p className="rounded-md border border-edge bg-page px-3 py-2 text-sm text-ink-dim">
                Restart Claude to drop the{' '}
                {plural(unadopted.removed.length,
                        unadopted.removed.some((r) => r.kind === 'run') ? 'run' : 'review run')}{' '}
                from its list. It reads these records when it starts.
              </p>
            ) : null}
            {runsPlaced + runsBatched + runsUnplaced > 0 ? (
              <p className="text-xs text-ink-faint" data-testid="runs-folded">
                {[
                  runsPlaced > 0
                    ? `${plural(runsPlaced, 'run')} on this machine ${runsPlaced === 1 ? 'is' : 'are'} ` +
                      `folded under the ${runsPlaced === 1 ? 'chat' : 'chats'} that spawned ` +
                      `${runsPlaced === 1 ? 'it' : 'them'}`
                    : '',
                  runsBatched > 0
                    ? `${plural(runsBatched, 'run')} with no chat behind ${runsBatched === 1 ? 'it' : 'them'} ` +
                      `${runsBatched === 1 ? 'is' : 'are'} folded under the folder above ` +
                      `${runsBatched === 1 ? 'it' : 'them'} (the Runs column)`
                    : '',
                  runsUnplaced > 0
                    ? `${plural(runsUnplaced, 'run')} could be placed nowhere and ${runsUnplaced === 1 ? 'is' : 'are'} left out`
                    : '',
                ].filter(Boolean).join('; ')}
                ; none is offered.
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
                className={link}
                onClick={() => setChosen(shown.map((g) => g.cwd))}
              >
                Select all
              </button>
              <button type="button" className={link} onClick={() => setChosen([])}>
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
            {report && report.restart_required ? (
              <p className="rounded-md border border-edge bg-page px-3 py-2 text-sm text-ink-dim">
                Restart Claude to see the {plural(report.written.length, 'chat')} adopted. It reads
                these records when it starts.
              </p>
            ) : null}
            {report?.note ? <p className="text-xs text-ink-faint">{report.note}</p> : null}
            {error ? <Problem error={error} /> : null}
            {state.groups.length > 0 ? (
              // FIXED LAYOUT, the same rule the main table follows: the columns are declared
              // rather than measured from whichever rows the search left on screen, so typing in
              // the box above cannot move them.
              <table className="table-fixed border-collapse text-sm"
                     style={{ width: shownColumns.reduce((n, c) => n + widths[c.id], 0) }}>
                <colgroup>
                  {shownColumns.map((column) => (
                    <col key={column.id} style={{ width: widths[column.id] }} />
                  ))}
                </colgroup>
                <thead className="sticky top-0 z-10 bg-panel">
                  <tr>
                    {shownColumns.map((column) => (
                      <th
                        key={column.id}
                        scope="col"
                        className={`relative ${head} ${column.right ? 'text-right' : ''}`}
                      >
                        {column.label}
                        <ResizeHandle
                          label={`Resize the ${column.label} column`}
                          size={widths[column.id]}
                          min={COLUMN.MIN}
                          max={COLUMN.MAX}
                          onSize={(px) => setWidths((was) => ({ ...was, [column.id]: px }))}
                          onReset={() =>
                            setWidths((was) => ({ ...was, [column.id]: ADOPT_WIDTHS[column.id] }))}
                          className="absolute inset-y-0 -right-1 z-20 w-2"
                        />
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {shown.map((g) => (
                    <Fragment key={g.cwd}>
                      <tr className="border-b border-edge/60 hover:bg-panel-raised/40">
                        <td className={`${cell} text-center`}>
                          <input
                            type="checkbox"
                            aria-label={`Adopt ${g.project}`}
                            checked={chosen.includes(g.cwd)}
                            onChange={() => toggle(g.cwd)}
                          />
                        </td>
                        <td className={cell}>
                          <div className="flex items-center gap-1.5">
                            {g.sessions.length > 0 ? (
                              <button
                                type="button"
                                aria-expanded={isOpen(g)}
                                aria-label={`${isOpen(g) ? 'Hide' : 'Show'} the chats in ${g.project}`}
                                onClick={() => toggleOpen(g.cwd)}
                                className="w-4 text-ink-faint hover:text-ink"
                              >
                                {isOpen(g) ? '▾' : '▸'}
                              </button>
                            ) : (
                              <span className="w-4" />
                            )}
                            <span className="text-ink">{g.project}</span>
                          </div>
                          {g.count === 1 && g.sessions[0]?.title ? (
                            <div className="pl-[1.375rem] text-xs text-ink-faint">
                              {g.sessions[0].title}
                            </div>
                          ) : null}
                        </td>
                        <td className={`${cell} text-right tabular-nums text-ink-dim`}>{g.count}</td>
                        <td className={`${cell} whitespace-nowrap text-ink-dim`}>{g.newest.slice(0, 10)}</td>
                        {hasRuns ? (
                          <td className={`${cell} text-right tabular-nums text-ink-dim`}>
                            {g.runs ?? 0}
                          </td>
                        ) : null}
                        <td
                          className={`${cell} max-w-[22rem] truncate font-mono text-xs text-ink-faint`}
                          title={g.cwd}
                        >
                          {g.cwd}
                        </td>
                      </tr>
                      {isOpen(g) ? (
                        <tr className="border-b border-edge/60">
                          <td />
                          <td colSpan={columns - 1} className="px-2 pb-2 pt-0">
                            <ul className="flex flex-col gap-0.5 pl-[1.375rem] text-xs">
                              {g.sessions.map((s) => (
                                <li key={s.session_id} className="flex flex-wrap gap-x-3">
                                  <span className="text-ink">{s.title}</span>
                                  <span className="text-ink-faint">
                                    {plural(s.turns, 'turn')}, {s.last_ts.slice(0, 10)}
                                    {s.cli ? ', CLI' : ''}
                                  </span>
                                </li>
                              ))}
                            </ul>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  ))}
                  {shown.length === 0 ? (
                    <tr>
                      <td colSpan={columns} className="px-2 py-4 text-center text-sm text-ink-dim">
                        No folder matches that.
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            ) : null}
          </div>
        </Dialog>
      ) : null}
    </div>
  )
}
