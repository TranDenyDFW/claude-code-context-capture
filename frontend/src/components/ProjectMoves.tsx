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
  const [busy, setBusy] = useState<'import' | 'delete' | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [imported, setImported] = useState<ImportReport | null>(null)
  const [deleted, setDeleted] = useState<DeleteReport | null>(null)
  const upload = useRef<HTMLInputElement>(null)

  const project = pathOf(cohort)
  const label = cohorts.find((c) => c.value === cohort)?.label ?? project ?? ''

  const close = () => {
    setOpen(false)
    setTyped('')
    setError(null)
    setImported(null)
    setDeleted(null)
  }

  const doImport = async (file: File) => {
    setBusy('import')
    setError(null)
    setImported(null)
    try {
      setImported(await api.project.import(file))
      onChanged()
    } catch (problem) {
      setError(problem)
    } finally {
      setBusy(null)
      // Cleared so choosing the SAME file again still fires a change event.
      if (upload.current) upload.current.value = ''
    }
  }

  const doDelete = async () => {
    if (!cohort || !project) return
    setBusy('delete')
    setError(null)
    try {
      setDeleted(await api.project.delete(cohort, typed, keepCapturing))
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
            className="w-full max-w-[44rem] overflow-hidden rounded-lg bg-panel shadow-float"
          >
            <div className="border-b border-edge px-5 py-3">
              <h2 className="text-md font-semibold text-ink">Move a project</h2>
              <p className="mt-0.5 text-xs text-ink-faint">
                A project is a working directory. Everything belonging to it travels together:
                sessions, turns, messages, tool calls, compactions and what each compaction kept.
              </p>
            </div>

            <div className="space-y-5 px-5 py-4">
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
                  Verified before a single row is written, and safe to run twice: rows already here
                  are left exactly as they are.
                </p>
                <input
                  ref={upload}
                  type="file"
                  aria-label="Choose an exported project file to import"
                  accept=".db,.sqlite,.sqlite3"
                  disabled={!writesEnabled || busy !== null}
                  onChange={(event) => {
                    const file = event.target.files?.[0]
                    if (file) void doImport(file)
                  }}
                  className="mt-2 block w-full text-sm text-ink-dim file:mr-3 file:rounded-md
                             file:border file:border-edge file:bg-page file:px-2.5 file:py-1.5
                             file:text-sm file:text-ink-dim hover:file:text-ink"
                />
                {busy === 'import' && (
                  <p className="mt-2 text-sm text-ink-dim">Verifying and loading…</p>
                )}
                {imported && (
                  <div className="mt-2 rounded-md border border-good/40 bg-good/5 px-3 py-2
                                  text-sm">
                    <p className="text-good">Imported {imported.project ?? 'the export'}</p>
                    <p className="mt-1 text-xs">
                      Added: <Counts counts={imported.inserted} />
                    </p>
                    <p className="mt-0.5 text-xs">
                      Already here: <Counts counts={imported.already_present} />
                    </p>
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
                      the file it leaves behind. The transcripts on disk are untouched.
                    </p>
                    <label className="mt-2 flex items-center gap-2 text-xs text-ink-dim">
                      <input
                        type="checkbox"
                        checked={keepCapturing}
                        onChange={(event) => setKeepCapturing(event.target.checked)}
                      />
                      Keep capturing this project (it will come back on the next harvest)
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
                    {deleted && (
                      <div className="mt-2 rounded-md border border-good/40 bg-good/5 px-3 py-2
                                      text-sm">
                        <p className="text-good">Deleted {deleted.project}</p>
                        <p className="mt-1 text-xs">
                          Removed: <Counts counts={deleted.removed} />
                        </p>
                        <p className="mt-0.5 break-all text-xs text-ink-dim">
                          Backup: <code>{deleted.backup}</code>
                        </p>
                        <p className="mt-0.5 text-xs text-ink-dim">
                          {deleted.excluded
                            ? 'Harvest will skip it from now on. Diagnostics lists it.'
                            : 'Still being captured, so it returns on the next harvest.'}
                        </p>
                      </div>
                    )}
                  </>
                ) : (
                  <p className="mt-0.5 text-xs text-ink-faint">
                    Choose a project under Population first.
                  </p>
                )}
              </section>

              {error !== null && <Problem error={error} />}
            </div>

            <div className="flex justify-end border-t border-edge px-5 py-3">
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
