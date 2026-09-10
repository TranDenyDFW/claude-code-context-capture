import { useRef, useState } from 'react'
import { api, ApiError } from '@/api'
import type { Cohort, DeleteReport, ImportReport } from '@/api'

/**
 * Move a whole project in or out of the store.
 *
 * THE COHORT VALUE GOES THROUGH UNTOUCHED. It is `project::<path>` and the server refuses anything
 * else, because a bare path resolves to no restriction: for a read that was a wrong session count
 * this app already shipped once, and for a delete it would be the entire store. Nothing here takes
 * the value apart, and the path shown to the reader is derived for DISPLAY only, never sent back.
 *
 * Delete asks for the path to be typed. Not a yes/no dialog: the risk is deleting the wrong
 * project, and a checkbox cannot tell those apart. The server checks it again on arrival, so this
 * field is a speed bump rather than the guard.
 */

/** The path a `project::<path>` cohort names. For DISPLAY. Never send this back to the server. */
export function pathOf(cohort: string | null | undefined): string | null {
  if (!cohort) return null
  const at = cohort.indexOf('::')
  if (at < 0 || cohort.slice(0, at) !== 'project') return null
  const value = cohort.slice(at + 2)
  return value || null
}

/**
 * The directory name Claude Code gives a working directory under `~/.claude/projects`.
 *
 * Every character that is not a letter or a digit becomes a hyphen, so `P:\Skills` is `P--Skills`.
 * Shown live beside the destination field: a typo in the path is invisible, and the slug it
 * produces is not.
 *
 * A SECOND COPY OF `c4x/appstate.py::slug_for`, which is a real cost and the cheaper of two. The
 * alternative is a round trip per keystroke to render a label. `ProjectMoves.test.tsx` pins the
 * cases the Python side pins, so the two cannot drift silently.
 */
export function slugFor(cwd: string): string {
  // TRIMMED THE WAY THE SERVER TRIMS. `check_destination` strips a trailing separator before
  // anything is derived from the path, so `D:\Work\Alpha\` becomes `D--Work-Alpha` there and
  // showed as `D--Work-Alpha-` here: a preview of a directory the import would not create, on the
  // one input a reader is most likely to paste.
  return cwd.trim().replace(/[\\/]+$/, '').replace(/[^A-Za-z0-9]/g, '-')
}

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

function Counts({ counts }: { counts: Record<string, number> }) {
  const rows = Object.entries(counts).filter(([, n]) => n > 0)
  if (rows.length === 0) return <span className="text-ink-faint">nothing</span>
  return (
    <span className="text-ink-dim">
      {rows.map(([table, n]) => `${table} ${n.toLocaleString()}`).join(' · ')}
    </span>
  )
}

export function ProjectMoves({
  cohort,
  cohorts,
  writesEnabled,
  onChanged,
}: {
  cohort: string | null
  cohorts: Cohort[]
  writesEnabled: boolean
  onChanged: () => void
}) {
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const [keepCapturing, setKeepCapturing] = useState(false)
  const [purgeSnapshots, setPurgeSnapshots] = useState(false)
  const [busy, setBusy] = useState<'import' | 'delete' | 'include' | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [imported, setImported] = useState<ImportReport | null>(null)
  const [deleted, setDeleted] = useState<DeleteReport | null>(null)
  // The chosen file is HELD, not imported on sight. Picking one runs a dry run, which reports
  // where every file would land; the import only happens once the destination has been seen.
  const [staged, setStaged] = useState<{ file: File; plan: ImportReport } | null>(null)
  const [into, setInto] = useState('')
  const upload = useRef<HTMLInputElement>(null)

  const project = pathOf(cohort)
  const label = cohorts.find((c) => c.value === cohort)?.label ?? project ?? ''

  const close = () => {
    setOpen(false)
    setTyped('')
    setError(null)
    setImported(null)
    setDeleted(null)
    setStaged(null)
    setInto('')
  }

  /**
   * Step one: ask the server where this export WOULD land, and write nothing.
   *
   * Uploaded twice on purpose. A browser cannot enumerate directories on the server, so the only
   * honest way to show the destination before committing to it is to let the server answer, and
   * the alternative, importing on sight and reporting afterwards, is what made a wrong destination
   * something you find out about by looking at the filesystem.
   */
  const stage = async (file: File) => {
    setBusy('import')
    setError(null)
    setImported(null)
    setStaged(null)
    try {
      const plan = await api.project.import(file, undefined, true)
      setStaged({ file, plan })
      setInto(plan.into[0] ?? '')
    } catch (problem) {
      setError(problem)
    } finally {
      setBusy(null)
      // Cleared so choosing the SAME file again still fires a change event.
      if (upload.current) upload.current.value = ''
    }
  }

  const doImport = async () => {
    if (!staged) return
    setBusy('import')
    setError(null)
    try {
      setImported(await api.project.import(staged.file, into.trim() || undefined))
      setStaged(null)
      onChanged()
    } catch (problem) {
      setError(problem)
    } finally {
      setBusy(null)
    }
  }

  const doInclude = async (project: string) => {
    setBusy('include')
    setError(null)
    try {
      await api.project.include(project)
      // Re-read rather than assume. The button disappears because the SERVER says the exclusion is
      // gone, not because this component decided it should be.
      const listed = await api.project.excluded()
      setImported((was) =>
        was ? { ...was, still_excluded: listed.excluded.some((e) => e.cwd === project) } : was)
      onChanged()
    } catch (problem) {
      setError(problem)
    } finally {
      setBusy(null)
    }
  }

  const doDelete = async () => {
    if (!cohort || !project) return
    setBusy('delete')
    setError(null)
    try {
      setDeleted(await api.project.delete(cohort, typed, keepCapturing, purgeSnapshots))
      setTyped('')
      onChanged()
    } catch (problem) {
      setError(problem)
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        title="Export, import or delete a whole project"
        className="rounded-md border border-edge bg-panel px-2.5 py-1.5 text-sm text-ink-dim
                   transition-colors hover:text-ink"
      >
        Project…
      </button>

      {open && (
        <div
          onMouseDown={close}
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 p-4 pt-[10vh]
                     backdrop-blur-sm"
          role="presentation"
        >
          <div
            onMouseDown={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.key === 'Escape' && close()}
            role="dialog"
            aria-modal="true"
            aria-label="Move a project in or out of this store"
            // A COLUMN WITH A HEIGHT CAP, so the middle scrolls and the header and footer do not.
            // Without the cap the dialog simply ran off the bottom of a 720px viewport: the delete
            // report, which is the only place the backup path is shown, was rendered where nobody
            // could read it and nothing scrolled to reach it.
            className="flex max-h-[80vh] w-full max-w-[44rem] flex-col overflow-hidden rounded-lg
                       bg-panel shadow-float"
          >
            <div className="shrink-0 border-b border-edge px-5 py-3">
              <h2 className="text-md font-semibold text-ink">Move a project</h2>
              <p className="mt-0.5 text-xs text-ink-faint">
                A project is a working directory. Everything belonging to it travels together:
                sessions, turns, messages, tool calls, compactions and what each compaction kept.
              </p>
            </div>

            <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4">
              {!writesEnabled && (
                <p className="rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-sm
                              text-warn">
                  This server was started with <code>--no-writes</code>. Restart it without that
                  flag to export, import or delete.
                </p>
              )}

              {/* Export */}
              <section>
                <h3 className="text-sm font-semibold text-ink">Export</h3>
                {project ? (
                  <>
                    <p className="mt-0.5 text-xs text-ink-faint">
                      One SQLite file, with its own manifest inside it. It opens in any SQLite tool
                      and is what Import reads back.
                    </p>
                    {/*
                      A LINK when it works and a DISABLED BUTTON when it does not, rather than one
                      anchor with the href removed. An <a> without an href is not a link to the
                      accessibility tree, so the disabled state would be announced as plain text
                      with no hint that it is a control at all.

                      A plain link, not a fetch: the browser saves the file, nothing passes through
                      this app, and a project too large to hold in memory still downloads.
                    */}
                    {writesEnabled ? (
                      <a
                        href={api.project.exportUrl(cohort!)}
                        className="mt-2 inline-block rounded-md border border-accent/60
                                   bg-accent/10 px-2.5 py-1.5 text-sm text-accent
                                   hover:bg-accent/20"
                      >
                        Export {label}
                      </a>
                    ) : (
                      <button
                        disabled
                        className="mt-2 inline-block cursor-not-allowed rounded-md border
                                   border-edge bg-panel px-2.5 py-1.5 text-sm text-ink-faint"
                      >
                        Export {label}
                      </button>
                    )}
                  </>
                ) : (
                  <p className="mt-0.5 text-xs text-ink-faint">
                    Choose a project under Population first. Sections and “No restriction” are not
                    projects and cannot be moved.
                  </p>
                )}
              </section>

              {/* Import */}
              <section>
                <h3 className="text-sm font-semibold text-ink">Import</h3>
                <p className="mt-0.5 text-xs text-ink-faint">
                  Carries the conversations too, not only the rows: the transcripts, the project
                  memory, the trust setting, and the desktop app's own record, so the project opens
                  in the app afterwards. Every carried file is re-hashed after writing, and the
                  import is safe to run twice.
                </p>
                <input
                  ref={upload}
                  type="file"
                  aria-label="Choose an exported project file to import"
                  accept=".db,.sqlite,.sqlite3"
                  disabled={!writesEnabled || busy !== null}
                  onChange={(event) => {
                    const file = event.target.files?.[0]
                    if (file) void stage(file)
                  }}
                  className="mt-2 block w-full text-sm text-ink-dim file:mr-3 file:rounded-md
                             file:border file:border-edge file:bg-page file:px-2.5 file:py-1.5
                             file:text-sm file:text-ink-dim hover:file:text-ink"
                />
                {busy === 'import' && (
                  <p className="mt-2 text-sm text-ink-dim">Verifying and loading…</p>
                )}
                {staged && (
                  // WHERE IT WILL LAND, BEFORE IT LANDS. The server has read the file and written
                  // nothing; this is its plan.
                  <div className="mt-2 rounded-md border border-edge bg-page px-3 py-2 text-sm">
                    <p className="text-ink">
                      {staged.plan.project ?? 'The export'}, from{' '}
                      {staged.plan.from ?? 'another machine'}
                    </p>
                    <label className="mt-2 block text-xs text-ink-dim" htmlFor="import-into">
                      Import into this working directory on this machine
                    </label>
                    <input
                      id="import-into"
                      type="text"
                      value={into}
                      spellCheck={false}
                      onChange={(event) => setInto(event.target.value)}
                      className="mt-1 w-full rounded-md border border-edge bg-page px-2 py-1
                                 font-mono text-xs text-ink"
                    />
                    <p className="mt-1 text-xs text-ink-faint">
                      Transcripts go to <code>~/.claude/projects/{slugFor(into.trim())}</code>. The
                      trust setting, the desktop record and the rows all follow this path, so the
                      app and this page agree on one location.
                    </p>
                    {/*
                      THE PLAN BELONGS TO THE DESTINATION IT WAS COMPUTED FOR.

                      The dry run runs once, when the file is chosen, against the destination the
                      export came from. Editing the field is the only thing the field is FOR, and
                      the counts and the "over something already there" both stop applying the
                      moment it is edited: they describe paths under the old slug. Re-running the
                      dry run per keystroke would re-upload the file, so the honest move is to say
                      the plan is stale rather than to keep showing it as though it were not.

                      The slug line above stays live, because it is derived from the field itself.
                    */}
                    {into.trim() === (staged.plan.into[0] ?? '') ? (
                      <p className="mt-1 text-xs text-ink-faint">
                        {staged.plan.app_state?.written.length ?? 0} file(s) would be written,{' '}
                        {staged.plan.app_state?.written.filter((w) => w.exists).length ?? 0} of them
                        over something already there.
                      </p>
                    ) : (
                      <p className="mt-1 text-xs text-warn">
                        {staged.plan.app_state?.written.length ?? 0} file(s) will be written. The
                        earlier count of what they would overwrite was worked out for{' '}
                        <code>{staged.plan.into[0]}</code> and does not apply to the path you typed.
                      </p>
                    )}
                    {(staged.plan.app_state?.refused.length ?? 0) > 0 && (
                      <p className="mt-1 text-xs text-warn">
                        Refused:{' '}
                        {staged.plan.app_state!.refused
                          .map((r) => `${r.relpath} (${r.why})`)
                          .join(' · ')}
                      </p>
                    )}
                    {staged.plan.not_moved.length > 0 && (
                      <p className="mt-1 text-xs text-warn">
                        Left where they were, being neither this project's directory nor under it:{' '}
                        {staged.plan.not_moved.join(' · ')}
                      </p>
                    )}
                    <div className="mt-2 flex gap-2">
                      <button
                        onClick={() => void doImport()}
                        disabled={busy !== null || !into.trim()}
                        className="rounded-md border border-edge bg-page px-2.5 py-1 text-sm
                                   text-ink hover:bg-edge/20 disabled:opacity-50"
                      >
                        Import
                      </button>
                      <button
                        onClick={() => setStaged(null)}
                        disabled={busy !== null}
                        className="rounded-md px-2.5 py-1 text-sm text-ink-dim hover:text-ink"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}
                {imported && (
                  // THE BORDER FOLLOWS THE MIRROR, NOT THE HTTP STATUS. A 200 with a non-empty
                  // `differs` means files landed and are not what the export carries, and showing
                  // that in green is the exact claim this change exists to stop.
                  // A rows-only export carried no files, so `ok` answers no question and the two
                  // lists are empty. `delete` writes its backup that way, so reading `ok` alone
                  // painted the documented undo-a-delete path red while naming nothing.
                  <div
                    className={`mt-2 rounded-md border px-3 py-2 text-sm ${
                      imported.mirror && !imported.mirror.ok && !imported.mirror.carries_no_files
                        ? 'border-bad/40 bg-bad/5'
                        : 'border-good/40 bg-good/5'
                    }`}
                  >
                    <p
                      className={
                        imported.mirror && !imported.mirror.ok && !imported.mirror.carries_no_files
                          ? 'text-bad'
                          : 'text-good'
                      }
                    >
                      Imported {imported.project ?? 'the export'} into{' '}
                      <code>{imported.into.join(', ')}</code>
                    </p>
                    {imported.mirror && (
                      <p className="mt-1 text-xs">
                        {imported.mirror.carries_no_files ? (
                          <span className="text-ink-dim">
                            This export carries rows only, so there were no files to compare. The
                            rows are back; the transcripts were never in the file.
                          </span>
                        ) : imported.mirror.ok ? (
                          // NOT "byte for byte", which was corrected in the CLI and left here:
                          // a config entry and a desktop record are rewritten by design.
                          <span className="text-good">
                            Every carried file is identical, re-read and re-hashed after writing.
                            The config entry and the desktop record are the same but for the
                            working directory, which this import rebased.
                          </span>
                        ) : (
                          <span className="text-bad">
                            NOT a mirror. {imported.mirror.missing.length} missing,{' '}
                            {imported.mirror.differs.length} different:{' '}
                            {[...imported.mirror.missing, ...imported.mirror.differs]
                              .slice(0, 4)
                              .map((f) => f.relpath)
                              .join(' · ')}
                          </span>
                        )}
                      </p>
                    )}
                    {imported.app_state && (
                      <p className="mt-1 text-xs">
                        Files restored: {imported.app_state.written.length} (
                        {(imported.app_state.bytes / 1048576).toFixed(1)} MB)
                        {imported.app_state.desktop.length > 0 &&
                          `, including ${imported.app_state.desktop.length} desktop app record(s), so it opens in the app`}
                      </p>
                    )}
                    {(imported.app_state?.replaced_shorter.length ?? 0) > 0 && (
                      // A compacted transcript is NEWER and SHORTER, so a true mirror can replace
                      // a longer local record with less conversation. That was the choice; doing
                      // it quietly was never part of it.
                      <p className="mt-1 text-xs text-warn">
                        Replaced with a SHORTER file, so a longer local copy is gone:{' '}
                        {imported.app_state!.replaced_shorter.map((f) => f.relpath).join(' · ')}
                      </p>
                    )}
                    {(imported.app_state?.refused.length ?? 0) > 0 && (
                      <p className="mt-1 text-xs text-warn">
                        Refused:{' '}
                        {imported.app_state!.refused
                          .map((r) => `${r.relpath} (${r.why})`)
                          .join(' · ')}
                      </p>
                    )}
                    {(imported.mirror?.not_carried.length ?? 0) > 0 && (
                      <p className="mt-1 text-xs text-ink-faint">
                        The export itself did not carry{' '}
                        {imported.mirror!.not_carried.reduce((n, e) => n + e.files, 0)} file(s) in
                        that directory, belonging to sessions it has no rows for. They are still on
                        the machine it came from.
                      </p>
                    )}
                    <p className="mt-1 text-xs">
                      Added: <Counts counts={imported.inserted} />
                    </p>
                    <p className="mt-0.5 text-xs">
                      Already here: <Counts counts={imported.already_present} />
                    </p>
                    {imported.still_excluded && imported.project && (
                      // The rows are back and harvest is still skipping the directory, so every
                      // session run there since is being dropped. Offered rather than done for
                      // them: an export from another machine can name a directory this machine
                      // excluded on purpose.
                      <div className="mt-2 rounded-md border border-warn/40 bg-warn/5 px-3 py-2
                                      text-xs text-warn">
                        <p>
                          The rows are back, but this project is still excluded, so nothing new in{' '}
                          <code>{imported.project}</code> will be captured.
                        </p>
                        <button
                          onClick={() => void doInclude(imported.project!)}
                          disabled={busy !== null}
                          className="mt-1.5 rounded-md border border-warn/60 bg-warn/10 px-2 py-1
                                     text-xs text-warn hover:bg-warn/20 disabled:opacity-50"
                        >
                          {busy === 'include' ? 'Resuming…' : 'Resume capturing it'}
                        </button>
                      </div>
                    )}
                    {Object.keys(imported.dropped_columns).length > 0 && (
                      <p className="mt-1 text-xs text-warn">
                        Columns this build does not have, so their values were not loaded:{' '}
                        {Object.entries(imported.dropped_columns)
                          .map(([table, cols]) => `${table}: ${cols.join(', ')}`)
                          .join(' · ')}
                      </p>
                    )}
                  </div>
                )}
              </section>

              {/* Delete */}
              <section>
                <h3 className="text-sm font-semibold text-bad">Delete</h3>
                {project ? (
                  <>
                    <p className="mt-0.5 text-xs text-ink-faint">
                      An export is written and read back first, so this is undoable by importing
                      the file it leaves behind. It removes exactly what that backup holds and
                      nothing else: the rows, the transcripts, the tasks, the memory files and
                      trust entry, and the chat in the desktop app. Memory and the trust entry are
                      kept when another session is still using this working directory, and the
                      report below says so when that happens.
                    </p>
                    <label className="mt-2 flex items-center gap-2 text-xs text-ink-dim">
                      <input
                        type="checkbox"
                        checked={keepCapturing}
                        onChange={(event) => setKeepCapturing(event.target.checked)}
                      />
                      Keep capturing this project (it will come back on the next harvest)
                    </label>
                    <label className="mt-1 flex items-center gap-2 text-xs text-ink-dim">
                      <input
                        type="checkbox"
                        checked={purgeSnapshots}
                        onChange={(event) => setPurgeSnapshots(event.target.checked)}
                      />
                      Also remove its pre-compaction snapshots (the backup does not carry them, so
                      this part cannot be undone)
                    </label>
                    <label className="mt-2 block text-xs text-ink-dim">
                      Type the project path to confirm:
                      <code className="ml-1 select-all text-ink">{project}</code>
                    </label>
                    <div className="mt-1.5 flex items-center gap-2">
                      <input
                        value={typed}
                        onChange={(event) => setTyped(event.target.value)}
                        spellCheck={false}
                        aria-label="Type the project path to confirm deletion"
                        className="min-w-0 flex-1 rounded-md border border-edge bg-page px-2 py-1.5
                                   font-mono text-sm text-ink"
                      />
                      <button
                        onClick={() => void doDelete()}
                        // Disabled until it matches EXACTLY. The server checks again; this only
                        // saves a round trip and makes the requirement visible while typing.
                        disabled={!writesEnabled || typed !== project || busy !== null}
                        className="rounded-md border border-bad/60 bg-bad/10 px-2.5 py-1.5 text-sm
                                   text-bad transition-colors enabled:hover:bg-bad/20
                                   disabled:cursor-not-allowed disabled:border-edge
                                   disabled:bg-panel disabled:text-ink-faint"
                      >
                        {busy === 'delete' ? 'Deleting…' : 'Delete'}
                      </button>
                    </div>
                  </>
                ) : (
                  <p className="mt-0.5 text-xs text-ink-faint">
                    Choose a project under Population first.
                  </p>
                )}
                {deleted && (
                  // THE VERDICT IS `still_here`, NOT THE ABSENCE OF AN EXCEPTION. A delete
                  // removes exactly what the backup holds and nothing else, so anything left
                  // behind is the claim failing and is painted as such.
                  <div
                    className={`mt-2 rounded-md border px-3 py-2 text-sm ${
                      deleted.still_here.length > 0
                        ? 'border-bad/40 bg-bad/5'
                        : 'border-good/40 bg-good/5'
                    }`}
                  >
                    <p className={deleted.still_here.length > 0 ? 'text-bad' : 'text-good'}>
                      {deleted.still_here.length > 0
                        ? `Deleted ${deleted.project}, and ${deleted.still_here.length} file(s) are still here`
                        : `Deleted ${deleted.project}`}
                    </p>
                    <p className="mt-1 text-xs">
                      Removed: <Counts counts={deleted.removed} />
                    </p>
                    <p className="mt-0.5 text-xs text-ink-dim">
                      {deleted.removed_files.toLocaleString()} file(s),{' '}
                      {(deleted.removed_bytes / 1048576).toFixed(1)} MB
                      {deleted.config_keys_removed.length > 0 &&
                        ', and the trust and settings entry'}
                    </p>
                    <p className="mt-0.5 break-all text-xs text-ink-dim">
                      Backup: <code>{deleted.backup}</code>
                    </p>
                    {deleted.shared_with_surviving_sessions.length > 0 && (
                      <p className="mt-0.5 text-xs text-warn">
                        Left alone:{' '}
                        {deleted.shared_with_surviving_sessions.length.toLocaleString()} memory
                        file(s) and settings shared with{' '}
                        {deleted.surviving_sessions.length} session(s) still in this working
                        directory.
                      </p>
                    )}
                    {deleted.kept_files.map((entry) => (
                      <p key={entry.path} className="mt-0.5 break-all text-xs text-warn">
                        Kept <code>{entry.path}</code>: {entry.why}
                      </p>
                    ))}
                    {deleted.refused_files.map((entry) => (
                      <p key={entry.relpath} className="mt-0.5 break-all text-xs text-warn">
                        Refused <code>{entry.relpath}</code>: {entry.why}
                      </p>
                    ))}
                    {deleted.not_carried.map((entry) => (
                      <p key={entry.path} className="mt-0.5 break-all text-xs text-warn">
                        Left on disk, the backup does not hold it:{' '}
                        <code>{entry.path}</code> ({entry.files.toLocaleString()} file(s)){' '}
                        {entry.why}
                      </p>
                    ))}
                    {deleted.still_here.map((entry) => (
                      <p key={entry.path} className="mt-0.5 break-all text-xs text-bad">
                        Still here: <code>{entry.path}</code>
                      </p>
                    ))}
                    {deleted.snapshots.files > 0 && (
                      <p className="mt-0.5 text-xs text-ink-dim">
                        {deleted.snapshots.removed > 0
                          ? `${deleted.snapshots.removed} pre-compaction snapshot(s) removed, `
                          : `${deleted.snapshots.files} pre-compaction snapshot(s) kept, `}
                        {(deleted.snapshots.bytes / 1048576).toFixed(1)} MB. The backup does
                        not carry them.
                      </p>
                    )}
                    {deleted.still_captured.map((cwd) => (
                      <p key={cwd} className="mt-0.5 break-all text-xs text-ink-dim">
                        Still captured: <code>{cwd}</code> has sessions this delete did not
                        take.
                      </p>
                    ))}
                    <p className="mt-0.5 text-xs text-ink-dim">
                      {deleted.excluded
                        ? 'Harvest will skip it from now on. Diagnostics lists it.'
                        : 'Still being captured, so it returns on the next harvest.'}
                    </p>
                  </div>
                )}
              </section>

              {error !== null && <Problem error={error} />}
            </div>

            <div className="flex shrink-0 justify-end border-t border-edge px-5 py-3">
              <button
                onClick={close}
                className="rounded-md border border-edge bg-page px-2.5 py-1.5 text-sm text-ink-dim
                           hover:text-ink"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
