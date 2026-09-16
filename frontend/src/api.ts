/**
 * The one place this frontend knows what the server's answers look like.
 *
 * These types are not invented here. They are `c4x/cli/extract.py::describe()`, which is the shape
 * the Python CLI has always emitted, the shape `tools/parity.py` compares, and the shape the API
 * serves. That is the whole reason a rewrite of the presentation layer is checkable at all: if this
 * file drifts from that contract, the parity gate fails rather than the page quietly rendering
 * something plausible.
 */

/** A table exactly as the dashboard renders it, including the SQL it was built from. */
export interface Table {
  id: string | null
  columns: string[]
  rows: Record<string, unknown>[]
  /** Column id to the explanation shown in its header. */
  tooltips?: Record<string, string>
  sorts?: unknown
}

/** A chart reduced to what can be CHECKED: no series, only extents. Use `plotly` to draw. */
export interface FigureSummary {
  title: string | null
  traces: { name?: string | null; type?: string | null; points?: number }[]
}

/**
 * A collapsible section, most often the SQL that produced the table above it.
 *
 * `table_index` indexes `TabPayload.tables`. It is -1 for a section that appears before any table,
 * and null when the server could not attribute it, in which case it must be rendered on its own
 * rather than guessed at: a query shown under a table that did not produce it would be worse than
 * no query at all.
 */
export interface Section {
  summary: string
  /**
   * The caption beside the title in the same summary line, kept apart from it.
   *
   * `theme.accordion()` writes a title and a caption as two styled spans; joined they became one
   * heading with no hover, which is how the Summary tab drew "What to do about it 6 finding(s),
   * each with an action" over a table whose own name is "Findings".
   */
  summary_note?: string | null
  body: string[]
  table_index: number | null
  /**
   * What the section CONTAINS, which decides whether it is a collapsible at all.
   *
   * `theme.accordion()` takes any children, and on the Summary tab it wraps the findings table, the
   * stat cards and the project chart. `extract.texts()` reads prose only, so all three arrived with
   * an empty body and the page drew a heading over nothing, twice, and printed the cards' own text
   * a second time in the third.
   */
  wraps?: 'text' | 'table' | 'figure' | 'stats'
  /** Which table or figure, when `wraps` names one. */
  wraps_index?: number | null
}

/** One band of rank-based cell shading. LAST match wins; see `_heat_bands` in the API. */
export interface Band {
  op: '>=' | '<='
  at: number
  background: string
  color?: string | null
}

/**
 * What the app declares about a column, which `describe()` drops.
 *
 * `specifier` is a d3 format string (`,`, `.1f`, `.2f`) taken verbatim from the column's Dash
 * `Format`. It is the reason this exists: the browser used to guess a number's precision from the
 * runtime value, so a column declared to one decimal rendered 43.30, 2.40, 1.20 and then a bare 1.
 */
export interface ColumnMeta {
  id: string
  label: string
  numeric: boolean
  specifier: string | null
  align: 'left' | 'right'
  hidden: boolean
  /**
   * The one column in this table long enough to widen it, chosen by the server from the data.
   *
   * Cells do not wrap, so a 200-character title pushes the table into a horizontal scroll. Only
   * this column is capped; every other keeps its natural width, because a cap wide enough for a
   * title means nothing on a date and narrowing all of them is the blunt fix that costs the
   * wrong columns. A table whose columns are all short marks none.
   */
  wide?: boolean
  bands: Band[]
}

export type NoteLevel = 'warn' | null

export interface TableMeta {
  id: string
  /** The heading above the table. Every table has one; the server pairs the heading it wrote. */
  title: string | null
  /** What the table shows, in a sentence or two. Shown on the heading, never in the page body. */
  note?: string | null
  /**
   * How loudly to say it. A note with a level is shown on the PAGE; one without is the hover.
   *
   * The server marks this where the note is written, because Dash says "warning" with an amber
   * colour and a colour does not survive the flatten to text. The Session tab's explanation of
   * why it shows one session when the header says All sessions arrived here indistinguishable
   * from a caption, and was therefore hidden behind a hover.
   */
  note_level?: NoteLevel
  /**
   * The levelled half of the note, which is the half that renders on the page.
   *
   * Kept apart from `note` because joining them made one drag the other into view: the Session
   * chart carries a warning AND a legend caption, and under one level the caption was drawn in
   * warning amber too, which is a paragraph of alarm over an explanation of some shaded bands.
   */
  alert?: string | null
  /** The `text` lines the server folded into `title` and `note`, so the page does not print them. */
  absorbed?: string[]
  columns: ColumnMeta[]
  /**
   * The SQL that produced this table, or null for a table that had no single query behind it.
   *
   * It used to be printed under the table as a collapsible of its own: six of them on the Cost
   * tab, in the reading flow, for something almost nobody opens on a given visit. The server now
   * marks the query on the block that owns the table, so it is attributed by CONTAINMENT rather
   * than by position, and the page puts it behind a button on the table itself.
   */
  query?: string | null
  filterable: boolean
  page_size: number | null
  /**
   * Where the rest of a cut column is. The messages table carries a 220-character preview per
   * row; an export must carry the message. POST `{ [key]: [...] }` to `url` and the answer maps
   * each key to the full text that replaces `column`.
   */
  /**
   * A GET route that returns what this row POINTS AT, keyed by one of its columns.
   *
   * The compactions table records a boundary's token counts; the summary that replaced the
   * dropped context is a separate document the store holds and the row only references.
   */
  detail?: { url: string; key: string }
  /**
   * A document reachable FROM the row, without the click meaning anything different.
   *
   * Separate from `detail` on purpose. `DataTable` decides a row is navigable, meaning a click
   * selects the session it names, from the ABSENCE of `detail`: reusing that field on the Sessions
   * table would have turned every click on the main list into an open, in a one line change on
   * the server. This one puts a control on the row instead and leaves the click alone.
   */
  row_detail?: { url: string; key: string; title?: string }
  full_text?: { url: string; key: string; column: string; as: string }
}

/** A headline figure a tab leads with, built by `theme.stat_card()` and read back by the API. */
export interface Stat {
  label: string
  value: string
  sub: string
}

export interface TabPayload {
  tab: string
  session: string | null
  scope: string
  cohort: string | null
  tables: Table[]
  figures: FigureSummary[]
  text: string[]
  /** Only on /render. Full Plotly figures, in the same order as `figures`. */
  plotly?: PlotlyFigure[]
  /** Only on /render. See `Section`. */
  details?: Section[]
  /** Only on /render. Paired to `tables` BY INDEX, or empty if the server could not pair them. */
  meta?: TableMeta[]
  /** Only on /render. The tab's headline figures, already separated into label/value/sub. */
  stats?: Stat[]
  /** Only on /render. Text under a Dash-only control the page does not draw; never printed. */
  dash_only?: string[]
  /**
   * Only on /render. What this VIEW is, as opposed to what any chart on it shows.
   *
   * Shown on the population chip in the header, which already states what the numbers cover.
   */
  about?: string[]
  /**
   * Only on /render. A block that names what would be here and says why it is not.
   *
   * Drawn as a placeholder where the table would be. "Not answerable with a single session
   * selected" is the most useful thing on that part of the page, and it is the difference between
   * "there are none" and "this question cannot be asked from here".
   */
  empty?: { title: string; note: string | null }[]
  /** Only on /render. The caption each chart answers to, in the same order as `figures`. */
  figure_meta?: {
    note: string | null; absorbed: string[]
    /** The levelled half, shown ON the page. `note` stays the hover. */
    alert?: string | null; note_level?: NoteLevel
  }[]
  /** Whether the header selection changes this tab at all. From the app's SELECTION_SCOPED. */
  scoped?: boolean
  /** The one sentence saying which population this tab describes. */
  population?: string | null
  /**
   * What that sentence says it is describing RIGHT NOW: "store" or "selection".
   *
   * Not the same fact as `scoped`, which says whether the header selection reaches this tab at
   * all. With nothing selected, four scoped tabs describe the whole store, and a chip derived from
   * `scoped` called all four "This Selection".
   */
  population_scope?: 'store' | 'selection' | null
}

/** Deliberately loose. This is handed straight to Plotly, which is the authority on its own shape. */
export interface PlotlyFigure {
  data: Record<string, unknown>[]
  layout?: Record<string, unknown>
}

export interface TabInfo {
  id: string
  label: string
  /** Whether the header selection reaches this tab. From the app's SELECTION_SCOPED. */
  scoped?: boolean
  /** One line saying what the tab answers. A TOOLTIP, not body text. */
  help?: string
}

/**
 * A population to ask a question about: all sessions, a section, or a project.
 *
 * `value` is OPAQUE. It is what `cohort_sessions()` parses (`project::<path>`, `section::<name>`,
 * `__all__`), and it must be passed through untouched. This frontend previously built its own list
 * out of every distinct working directory and sent the bare path, which the store does not
 * recognise, so it applied no filter at all while the header said a project was selected.
 */
export interface Cohort {
  label: string
  value: string
  /**
   * What the hover says: a project's full working directory, a sentence for all and sections.
   * The label names a project by its folder alone. Optional so an older server still answers.
   */
  path?: string
}

export interface SessionRow {
  session_id: string
  title?: string | null
  project?: string | null
  section?: string | null
  turns?: number
  current?: number
  peak?: number
  compactions?: number
  last_ts?: string | null
}

export interface Selection {
  session?: string | null
  scope?: 'main' | 'all'
  cohort?: string | null
  /**
   * Arm B on the Compare tab. Arm A is the rest of this object.
   *
   * The server has always accepted these; the page never sent them, so Compare rendered whatever
   * `default_arm_b()` picked and nothing could steer it. Comparing a session against its own fork,
   * which is the reason the tab exists, was unreachable from the UI.
   */
  compareWith?: string | null
  compareKind?: 'session' | 'cohort'
}

/** A project harvest has been told to stop capturing. */
export interface Exclusion {
  cwd: string
  excluded_at: string | null
  note: string | null
}

/** One file the import wrote, or would write. */
export interface MirrorFile {
  relpath: string
  kind: string
  path?: string
  /** Dry run only: whether something is already at that path and would be replaced. */
  exists?: boolean
  into?: string
  why?: string
  was?: number
  now?: number
}

/**
 * Whether this machine now holds what the export carries.
 *
 * `missing` is carried and absent, `differs` is carried and hashes differently, `extra` is here
 * and not in the export: reported and never deleted, because a slug directory is shared by every
 * session with the same working directory.
 *
 * THE PAGE MUST RENDER THIS AND NOT ONLY THE ROW COUNTS. "Imported" printed above a non-empty
 * `differs` is the exact claim the whole change exists to stop.
 */
export interface MirrorResult {
  ok: boolean
  missing: MirrorFile[]
  differs: MirrorFile[]
  extra: string[]
  unresolved: MirrorFile[]
  into: string[]
  not_carried: { path: string; files: number; why: string }[]
  /**
   * The export carried rows only, so there was nothing to compare and `ok` answers no question.
   *
   * NO LONGER TRUE OF `delete`, which carries the files as of this branch: its backup is
   * written with app_state on, so an undo restores the transcripts and the trust entry too.
   * A rows-only export is what `export(app_state=False)` still writes.
   * Reading `ok` alone painted that correct restore red and named nothing, because `missing` and
   * `differs` are both empty when nothing was carried.
   */
  carries_no_files?: boolean
}

/** The files an import wrote, and what it refused. */
export interface AppStateReport {
  written: MirrorFile[]
  replaced: MirrorFile[]
  replaced_shorter: MirrorFile[]
  refused: MirrorFile[]
  desktop: { path: string; cwd: string }[]
  bytes: number
  dry_run: boolean
}

/** What `/api/project/import` reports back, per table. */
export interface ImportReport {
  project: string | null
  from: string | null
  /** The working directories on THIS machine the import landed in. */
  into: string[]
  /** {source working directory: destination}. What the page shows before it commits. */
  mapping: Record<string, string>
  /** Carried directories that are neither the project's own nor under it, so they did not move. */
  not_moved: string[]
  /** True when nothing was written and this is only a plan. */
  dry_run?: boolean
  app_state?: AppStateReport
  mirror?: MirrorResult
  rebased_rows?: Record<string, number>
  /**
   * The rows are back but harvest is still skipping the directory.
   *
   * Reported rather than fixed by the server: an export from another machine can name a working
   * directory this machine excluded on purpose. The page offers to lift it.
   */
  still_excluded: boolean
  inserted: Record<string, number>
  already_present: Record<string, number>
  dropped_columns: Record<string, string[]>
  /** The desktop app's record was written, which the app reads when it starts. */
  restart_required?: boolean
  restart?: RestartReport | null
}

export interface DeleteReport {
  project: string
  backup: string
  removed: Record<string, number>
  excluded: boolean
  excluded_cwds: string[]
  /** Directories still harvested, because sessions this delete did not take live in them. */
  still_captured: string[]
  removed_files: number
  removed_bytes: number
  /** On disk still, with the reason. A file that changed since the backup is not the backup's. */
  kept_files: { path: string; kind: string; why: string }[]
  /** Named, never removed. `kind` is the layer the row belongs to: transcript, memory, tasks,
   * config or desktop. A directory walk that refuses has its own channel, `prune_refused`. */
  refused_files: { relpath: string; kind: string; why: string }[]
  config_keys_removed: string[]
  config_keys_kept: { key: string; why: string }[]
  /** Memory and trust settings left alone, because they belong to the working directory. */
  shared_with_surviving_sessions: { relpath: string; kind: string }[]
  surviving_sessions: string[]
  /** Sessions in the same slug directory under a different working directory string. */
  sessions_sharing_slug: string[]
  /**
   * What the EXPORT could not carry. These are still on disk and the backup does not hold them,
   * so they are the one thing "removes exactly what the backup contains" does not account for.
   */
  not_carried: { path: string; files: number; why: string }[]
  /** `path`, not `relpath`: this is what `_write_app_state` actually appends. */
  too_large: { path: string; bytes: number }[]
  /** A file the export could not READ. Same class: on disk, and not in the backup. */
  skipped: { path: string; why: string }[]
  /** A directory walk the prune refused because it could not prove where to stop. */
  prune_refused: { path: string; why: string }[]
  /**
   * Transcript files this project shared with another working directory. The harvester abandons a
   * FILE, not a session, so these are why a directory can be left capturing.
   */
  shared_transcripts: { cwd: string; with_cwd: string; transcript: string }[]
  /**
   * Files for a deleted session that arrived AFTER the backup was taken. Left on disk on purpose,
   * because the backup cannot restore what it never held, and named so that is a decision.
   */
  appeared_files: { path: string; cwd: string }[]
  /** No session under this label had a working directory, so no files were purged at all. */
  unlocated: boolean
  snapshots: { files: number; removed: number; bytes: number }
  /**
   * THE ACCEPTANCE TEST. A delete removes exactly what the backup contains, and nothing else, so
   * anything named here is a delete that did not finish. Empty is the only good answer.
   */
  /**
   * `path` is null for a row the purge REFUSED: it could not resolve where the file is, which is
   * "I cannot tell", not "it is gone". The `why` says which.
   */
  still_here: { path: string | null; kind: string; relpath?: string; why?: string }[]
  appeared_since_backup: string[]
}

export class ApiError extends Error {
  // Declared as fields rather than constructor parameter properties: this project builds with
  // `erasableSyntaxOnly`, which rejects the shorthand because it emits runtime code from what looks
  // like a type annotation.
  status: number
  detail?: unknown

  constructor(message: string, status: number, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function get<T>(path: string, params: Record<string, unknown> = {}): Promise<T> {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    // null and undefined mean "not selected" and must not become the strings "null"/"undefined",
    // which the server would take as a session id and answer with an empty pane.
    if (value !== null && value !== undefined && value !== '') query.set(key, String(value))
  }
  const suffix = query.toString() ? `?${query}` : ''
  const response = await fetch(`${path}${suffix}`)
  if (!response.ok) {
    let detail: unknown
    try {
      detail = (await response.json())?.detail
    } catch {
      detail = await response.text().catch(() => undefined)
    }
    // The message says which request failed. "Failed to fetch" with no path is the single least
    // useful thing a data layer can report, and it is the default.
    throw new ApiError(`${response.status} from ${path}${suffix}`, response.status, detail)
  }
  return response.json() as Promise<T>
}

async function post<T>(path: string, body: unknown): Promise<T> {
  // FormData sets its own multipart boundary. Setting Content-Type by hand here strips the
  // boundary and the server reads an empty upload, which it then correctly reports as an invalid
  // export, blaming the file rather than the request.
  const isForm = body instanceof FormData
  const response = await fetch(path, {
    method: 'POST',
    headers: isForm ? undefined : { 'Content-Type': 'application/json' },
    body: isForm ? body : JSON.stringify(body),
  })
  if (!response.ok) {
    let detail: unknown
    try {
      detail = (await response.json())?.detail
    } catch {
      detail = await response.text().catch(() => undefined)
    }
    throw new ApiError(`${response.status} from ${path}`, response.status, detail)
  }
  return response.json() as Promise<T>
}

/** One `<account>/<org>` records directory on this machine, and what it resolves to. */
export interface AccountPair {
  root: string
  account: string
  org: string
  path: string
  /** The directory this one points at, or null when it is an ordinary directory. */
  link_to: string | null
  records: number
  /**
   * What this pair would list on its own (what Current would show it): its files, or under
   * sharing the records the backup manifest filed under it plus, for the shared directory, the
   * ones written since. null when links exist and no manifest says whose is whose.
   */
  own?: number | null
  /**
   * On an `uncovered` entry: why this pair does not read the shared list. `not linked` is a
   * directory the app created since sharing; `points elsewhere` a link the kernel lands on
   * another directory (the Store build's virtual spelling of the target); `dangling` a link to
   * nothing. The server covers all three when Claude next closes.
   */
  why?: 'not linked' | 'points elsewhere' | 'dangling'
}

/**
 * What `/api/accounts` reports.
 *
 * `mode` is what is on disk and `intended` is what was asked for. They disagree after an app
 * update migrates these directories, which is the one failure the arrangement has.
 */
export interface AccountsState {
  supported: boolean
  why_not: string
  app_running: boolean
  mode: 'all' | 'current' | 'mixed'
  intended: 'all' | 'current'
  roots: { root: string; pairs: AccountPair[] }[]
  pairs: number
  linked: number
  /** What All shows: every record in the directories that hold files. */
  chats_visible: number
  /** The pair the desktop app is writing, or null when nothing on the machine says. */
  signed_in?: { account: string; org: string } | null
  /** What Current shows the signed-in account: its own chats, or null when that cannot be told. */
  current_chats: number | null
  /**
   * Where that number came from: harvest's tags (the account each chat was made under), the
   * sharing backup's manifest (where each record sat before sharing), the directories themselves
   * (nothing shared), or null when links exist and nothing can say.
   */
  current_source?: 'tags' | 'manifest' | 'directory' | null
  /** Live records no tag names an account for; only meaningful with `current_source` tags. */
  untagged?: number
  /** Chats this page lists, in all; null without a store. */
  listed?: number | null
  /**
   * Chats this page lists for the signed-in account: the same number the population list's
   * "Signed-in account's chats" carries, from one function. null when nothing is signed in or
   * there is no store. `current_chats` stays what the desktop app lists (records).
   */
  current_listed?: number | null
  /**
   * What says `intended`: the marker a switch wrote, the links on the disk when the marker is
   * gone (a reset of `data/` takes it; the junctions stay), or nothing.
   */
  intended_source: 'marker' | 'disk' | 'none'
  /**
   * Under All: the real directories beside the shared one, which the app created at a sign-in
   * since sharing began. That account reads a list of its own until the server covers them,
   * which it does when Claude next closes, or on `reconcile` now.
   */
  uncovered: AccountPair[]
}

/** What `/api/accounts/sharing` answers. `restart_required` is always true on a change. */
/**
 * What the server did to Claude around a confirmed write (`desktop.with_restart`): whether it
 * was running, whether it was quit and how many processes that was, whether it came back and
 * how (`shell`: the app's own activation; `task`: a scheduled task in the interactive session,
 * the way that worked on the test laptop when the activation did not), and a sentence.
 */
export interface RestartReport {
  was_running: boolean
  quit: boolean
  killed: number
  relaunched: boolean
  how: 'shell' | 'task' | null
  launch: string[] | null
  why: string
}

export interface SharingReport {
  mode: 'all' | 'current'
  dry_run: boolean
  restart_required: boolean
  backup?: string | null
  state: AccountsState
  /** Present when the request carried `restart: true`. */
  restart?: RestartReport | null
}

/** What `/api/accounts/reconcile` answers: the fold of the uncovered pairs, or why it did not run. */
export interface ReconcileReport {
  ran: boolean
  why: string
  app_running: boolean
  pending: AccountPair[]
  backup?: string | null
  marker_written: boolean
  restart_required: boolean
  state: AccountsState
  restart?: RestartReport | null
}

/** One chat the desktop app has no record of. */
export interface AdoptSession {
  session_id: string
  title: string
  title_source: 'user' | 'auto'
  first_ts: string
  last_ts: string
  turns: number
  model: string
  cli: boolean
  cwd: string
}

/** The chats without a record under one working directory, newest first. */
export interface AdoptGroup {
  cwd: string
  project: string
  count: number
  newest: string
  sessions: AdoptSession[]
  /**
   * Headless runs folded under this folder (a harness's `claude -p` children, never offered on
   * their own). Absent from a server that does not derive them; the table shows the column only
   * when it is there.
   */
  runs?: number
}

/**
 * What `/api/adopt` reports: what could be adopted, and where a record would land.
 *
 * `deleted_markers` counts the `deleted_<uuid>` files the app leaves when a chat is deleted on
 * purpose; those chats look like reinstall orphans to the rule, so the page says the number.
 */
export interface AdoptState {
  supported: boolean
  why_not: string
  pair: { account: string; org: string; root: string; source: string } | null
  physical: string | null
  groups: AdoptGroup[]
  candidates: number
  cli_candidates: number
  other_account: number
  deleted_markers: number
  /** Records c4x wrote that carry no name; the app shows each as "General coding session". */
  untitled_adopted: number
  /**
   * Review runs (a hook's `claude -p` reading another chat, tied to it by harvest) among the
   * sessions that would otherwise be offered: folded into the chat they reviewed, never offered.
   */
  review_runs: number
  /** Records c4x wrote for review runs that are still on disk; the drawer offers to take them back. */
  review_records: number
  /**
   * Headless child runs (a harness's `claude -p` one-shots) the store knows: folded under the
   * chats that spawned them (`runs_placed`), under the project above a batch (`runs_batched`),
   * or placed nowhere (`runs_unplaced`); never offered. `run_records` counts the records an
   * earlier build wrote for any of them, taken back with the review records. Absent from a
   * server that does not derive them.
   */
  runs?: number
  runs_placed?: number
  runs_batched?: number
  runs_unplaced?: number
  run_records?: number
  /** Chats deleted in the desktop app whose transcript is still here: hidden, never offered. */
  deleted_in_app?: number
  app_running: boolean
  sharing: 'all' | 'current' | null
}

/** What `POST /api/adopt/retitle` answers. */
export interface RetitleReport {
  renamed: { session_id: string; path: string; title: string }[]
  kept: number
  missing: number
  restart_required: boolean
  restart?: RestartReport | null
}

/** What `POST /api/adopt/unadopt-reviews` answers: the records taken back, and their chats. */
export interface UnadoptReport {
  /**
   * `reviewed` is null for a run the store knows is a review but cannot place, and for a child
   * run; `kind` says which (absent from an older server, which took back reviews only), and
   * `parent` is the chat that spawned a child run, null for a batch.
   */
  removed: { session_id: string; path: string; reviewed: string | null; kind?: 'review' | 'run';
             parent?: string | null }[]
  missing: number
  kept: number
  restart_required: boolean
  restart?: RestartReport | null
}

/** What one startup sweep did: `adopt.sweep_reviews`, read back from its stamp file. */
export interface SweepReport {
  at: string
  epoch: number
  removed: number
  missing: number
  failed: number
  restarted: boolean
  restarted_epoch: number | null
  why: string
  restart: { restarted: boolean; killed: number; launch: string[] | null; why: string } | null
}

/** What `GET /api/adopt/sweep` answers. */
export interface SweepState {
  /** Off under `--no-writes` and `--no-review-sweep`. */
  enabled: boolean
  last: SweepReport | null
}

/** What a POST to `/api/adopt` answers. `note` is set when the bytes live in a shared directory. */
export interface AdoptReport {
  supported: boolean
  why_not: string
  pair: AdoptState['pair']
  physical: string | null
  note: string | null
  written: { session_id: string; path: string; title: string | null }[]
  skipped: { session_id: string; why: string }[]
  selected: number
  restart_required: boolean
  dry_run: boolean
  restart?: RestartReport | null
}

/** One plan a chat proposed. The whole text lives behind `/api/plan/<tool_use_id>`. */
export interface ChatPlan {
  tool_use_id: string
  ts: string | null
  plan_chars: number | null
  plan_file_path: string | null
  preview: string | null
  /** From the CALL, not the plan: the same text is a proposal whether it was accepted or refused. */
  outcome: string | null
  denial_kind: string | null
}

/** One subagent run, reached through the call that spawned it or through the directory it sits in. */
export interface ChatAgentRun {
  agent_id: string
  agent_type: string | null
  name: string | null
  description: string | null
  workflow_run_id: string | null
  tool_use_id: string | null
  called_from: string | null
  spawned_at: string | null
  outcome: string | null
  records: number | null
  output_tokens: number | null
}

/** One workflow run. `agent_count` is what it reported; `agents_on_disk` is what this store holds. */
export interface ChatWorkflowRun {
  run_id: string
  task_id: string | null
  workflow_name: string | null
  status: string | null
  started_at: string | null
  duration_ms: number | null
  agent_count: number | null
  agents_on_disk: number | null
  total_tokens: number | null
  total_tool_calls: number | null
  summary: string | null
}

/** One task notification inside the chat. `resolved_to` is null when nothing on disk matches it. */
export interface ChatTaskEvent {
  uuid: string
  ts: string | null
  task_id: string | null
  task_type: string | null
  status: string | null
  description: string | null
  resolved_to: 'agent' | 'workflow' | null
  ran_under: string | null
}

/**
 * One review of the chat: a one-shot session a hook's headless reviewer left, tied to the chat
 * by quotation (harvest's `review_links`) and listed nowhere on its own. `after_prompt` is the
 * prompt the chat was answering when the run started, which is where it was dispatched; `round`
 * counts the chat's reviews in time order.
 */
export interface ChatReview {
  session_id: string
  ts: string | null
  verdict: 'APPROVED' | 'PROBLEMS' | null
  round: number
  after_prompt_ts: string | null
  after_prompt: string | null
  calls: number
  input_tokens: number | null
  cache_read: number | null
  cache_creation: number | null
  output_tokens: number | null
  cost_usd: number | null
  hits: number
  snippets: number
}

/**
 * What `/api/chat/<session>` answers.
 *
 * `harvested` says which tables this store actually has. Without it an empty list from an older
 * store is indistinguishable from a chat that ran nothing, and the panel would state the second.
 */
/** One file a chat changed, with counts that cover only the edits whose result recorded a patch. */
export interface ChatChangedFile {
  file: string
  edits: number
  ok_edits: number | null
  additions: number | null
  deletions: number | null
  /** How many of `edits` the two sums cover. A subagent edit records no patch. */
  patched: number | null
  by_subagents: number | null
  first_ts: string | null
  last_ts: string | null
  kinds: string | null
}
/** One edit, as the per-file list answers it. */
export interface ChatChange {
  tool_use_id: string
  ts: string | null
  turn_uuid: string | null
  tool_name: string | null
  file: string | null
  kind: string | null
  old_lines: number | null
  new_lines: number | null
  additions: number | null
  deletions: number | null
  has_patch: number | boolean
  is_sidechain: number | boolean | null
  user_modified: number | boolean | null
  outcome: string | null
  denial_kind: string | null
}
/** A unified-diff hunk as the transcript records it: prefixed lines, space, plus or minus. */
export interface DiffHunk {
  oldStart: number
  oldLines: number
  newStart: number
  newLines: number
  lines: string[]
}
/**
 * One change whole. `hunks` has THREE states: a list is a recorded patch, an empty list is a
 * created file whose whole content is `new_text`, and null is an edit whose result was never
 * recorded, which is every subagent edit.
 */
export interface ChangeDetail {
  tool_use_id: string
  session: string | null
  turn_uuid: string | null
  ts: string | null
  tool_name: string | null
  file: string | null
  kind: string | null
  old_text: string | null
  new_text: string | null
  replace_all: boolean
  old_lines: number | null
  new_lines: number | null
  hunks: DiffHunk[] | null
  additions: number | null
  deletions: number | null
  original_chars: number | null
  user_modified: boolean
  is_sidechain: boolean
  outcome: string | null
  denial_kind: string | null
}
/**
 * One headless child run of the chat: a one-shot `claude -p` its shell command spawned (a
 * harness, a script), tied to it by harvest's `run_links` and listed nowhere on its own. `how`
 * is the evidence that tied it (prompt, cwd, quoted, under); `leaf` the last segment of the
 * folder it worked in, which is what a harness names its cases by.
 */
export interface ChatRun {
  session_id: string
  ts: string | null
  how: 'prompt' | 'cwd' | 'quoted' | 'under' | string
  hits: number
  call_id: string | null
  cwd: string | null
  leaf: string
  prompt: string | null
  calls: number
  input_tokens: number | null
  cache_read: number | null
  cache_creation: number | null
  output_tokens: number | null
  cost_usd: number | null
}
export interface ChatWork {
  session: string
  chat: string[]
  harvested: Record<string, boolean>
  plans: ChatPlan[]
  plans_total: number
  agent_runs: ChatAgentRun[]
  agent_runs_total: number
  workflow_runs: ChatWorkflowRun[]
  workflow_runs_total: number
  task_events: ChatTaskEvent[]
  task_events_total: number
  task_events_unresolved: number
  changed_files: ChatChangedFile[]
  changed_files_total: number
  changes_total: number
  reviews: ChatReview[]
  reviews_total: number
  /** Absent from a server that does not derive child runs. */
  runs?: ChatRun[]
  runs_total?: number
}

export const api = {
  tabs: () => get<TabInfo[]>('/api/tabs'),
  cohorts: () => get<Cohort[]>('/api/cohorts'),
  /** The session picker's options, narrowed to the cohort by the SERVER. */
  selector: (cohort?: string | null) =>
    get<Cohort[]>('/api/selector', { cohort }),
  health: () =>
    get<{
      ok: boolean
      db: string
      /** The server process, so a restart can be told apart from the server it replaced. */
      pid: number
      /** This process never harvests. Always true on the API server. */
      read_only: boolean
      /** The project export/import/delete routes will answer. Off with `--no-writes`. */
      writes_enabled: boolean
      cache: Record<string, number>
    }>('/api/health'),
  sessions: (limit = 200) =>
    get<{ rows: SessionRow[]; total: number }>('/api/sessions', { limit }),
  /** The drawing shape: tables, plus every chart as full Plotly JSON. */
  tab: (id: string, selection: Selection = {}) =>
    get<TabPayload>(`/api/tab/${id}/render`, {
      session: selection.session,
      scope: selection.scope ?? 'main',
      cohort: selection.cohort,
      compare_with: selection.compareWith,
      compare_kind: selection.compareWith ? (selection.compareKind ?? 'session') : undefined,
    }),

  /**
   * Moving a project in and out of the store.
   *
   * EVERY ONE OF THESE TAKES THE COHORT VALUE UNCHANGED. It is `project::<path>`, and the server
   * refuses anything that does not name a project, because a bare path resolves to no restriction:
   * for a read that was a wrong session count this app already shipped once, and for a delete it
   * would be the whole store. Do not take the value apart here to make it look nicer.
   */
  /**
   * Whether every account signed into this machine reads one chat list.
   *
   * The separation is a directory per account, so this moves directories: the server refuses while
   * Claude is open, and a change that lands needs it restarted.
   */
  accounts: {
    state: () => get<AccountsState>('/api/accounts'),
    verify: () =>
      get<{ ok: boolean; intended: string; problems: string[] }>('/api/accounts/verify'),
    /**
     * `restart`: the person confirmed, so the server quits Claude first and starts it again
     * after (the report's `restart` says how it went). Without it the server refuses with 409
     * while Claude is open, as it always did.
     */
    share: (mode: 'all' | 'current', restart = false) =>
      post<SharingReport>('/api/accounts/sharing', { mode, restart }),
    /** Cover now: the fold the server runs when Claude closes, on demand. 409 while it is open,
     * unless `restart` has the server quit and start Claude around it. */
    reconcile: (restart = false) => post<ReconcileReport>('/api/accounts/reconcile', { restart }),
  },

  /**
   * The chats the desktop app has no record of, and the write that gives it one per chosen
   * folder. New files only, into the signed-in account's directory; Claude reads them on restart.
   */
  adopt: {
    state: (includeCli = false) =>
      get<AdoptState>('/api/adopt', { include_cli: includeCli ? 'true' : undefined }),
    /** `restart`: the person confirmed, so the server restarts Claude once the records are written. */
    run: (body: { cwds: string[]; include_cli: boolean; dry_run: boolean; restart?: boolean }) =>
      post<AdoptReport>('/api/adopt', body),
    retitle: (restart = false) => post<RetitleReport>('/api/adopt/retitle', { restart }),
    unadoptReviews: (restart = false) =>
      post<UnadoptReport>('/api/adopt/unadopt-reviews', { restart }),
    /** The sweep the server runs at startup: whether it is on here, and what the last one did. */
    sweep: () => get<SweepState>('/api/adopt/sweep'),
  },

  /**
   * The server that serves this page. Stop leaves nothing answering until the next Claude
   * session starts one; restart starts a replacement with the same flags, and the page waits for
   * a different process to answer `health`.
   */
  server: {
    stop: () => post<{ stopped: boolean }>('/api/server/stop', {}),
    restart: () => post<{ restarting: boolean; pid: number; argv: string[] }>('/api/server/restart', {}),
  },

  project: {
    excluded: () =>
      get<{ excluded: Exclusion[]; writes_enabled: boolean }>('/api/project/excluded'),

    /** The download URL, not a fetch. The browser saves the file; nothing passes through JS. */
    exportUrl: (cohort: string) =>
      `/api/project/export?cohort=${encodeURIComponent(cohort)}`,

    /**
     * `into` is the working directory ON THE SERVER to import into, and everything is rebuilt from
     * it: the slug directory, the config key, the desktop app's record, and the cwd in the rows.
     * Leave it out to land where the export came from.
     *
     * `dryRun` names every destination and writes nothing. The page runs that first, so a wrong
     * destination is visible before it lands rather than after.
     */
    import: (file: File, into?: string, dryRun = false, restart = false) => {
      const body = new FormData()
      body.append('file', file)
      if (into) body.append('into', into)
      if (dryRun) body.append('dry_run', 'true')
      // The desktop app's record the import writes is read when the app starts; the person
      // confirmed, so the server restarts Claude once it is written.
      if (restart && !dryRun) body.append('restart', 'true')
      return post<ImportReport>('/api/project/import', body)
    },

    /**
     * `confirm` must be the project path exactly. The server checks it; this does not.
     *
     * `purgeSnapshots` reaches the pre-compaction snapshots, which the backup does NOT carry, so
     * it is the one part of a delete that importing the backup cannot undo.
     */
    delete: (cohort: string, confirm: string, keepCapturing = false, purgeSnapshots = false) =>
      post<DeleteReport>('/api/project/delete', {
        cohort,
        confirm,
        keep_capturing: keepCapturing,
        purge_snapshots: purgeSnapshots,
      }),

    include: (project: string) =>
      post<{ project: string; removed: number }>('/api/project/include', { project }),
  },
}
