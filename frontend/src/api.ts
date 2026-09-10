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
   * `delete` writes its backup this way, which makes every undo of a delete a rows-only import.
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
  /** Named, never removed. `kind` is the layer, or "prune" for a directory walk that refused. */
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
  too_large: { relpath: string; bytes: number }[]
  snapshots: { files: number; removed: number; bytes: number }
  /**
   * THE ACCEPTANCE TEST. A delete removes exactly what the backup contains, and nothing else, so
   * anything named here is a delete that did not finish. Empty is the only good answer.
   */
  still_here: { path: string; kind: string }[]
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
    import: (file: File, into?: string, dryRun = false) => {
      const body = new FormData()
      body.append('file', file)
      if (into) body.append('into', into)
      if (dryRun) body.append('dry_run', 'true')
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
