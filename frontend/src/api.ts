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
  bands: Band[]
}

export interface TableMeta {
  id: string
  title: string | null
  columns: ColumnMeta[]
  filterable: boolean
  page_size: number | null
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
  /** Whether the header selection changes this tab at all. From the app's SELECTION_SCOPED. */
  scoped?: boolean
  /** The one sentence saying which population this tab describes. */
  population?: string | null
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
}

/** A project harvest has been told to stop capturing. */
export interface Exclusion {
  cwd: string
  excluded_at: string | null
  note: string | null
}

/** What `/api/project/import` reports back, per table. */
export interface ImportReport {
  project: string | null
  from: string | null
  inserted: Record<string, number>
  already_present: Record<string, number>
  dropped_columns: Record<string, string[]>
}

export interface DeleteReport {
  project: string
  backup: string
  removed: Record<string, number>
  excluded: boolean
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

    import: (file: File) => {
      const body = new FormData()
      body.append('file', file)
      return post<ImportReport>('/api/project/import', body)
    },

    /** `confirm` must be the project path exactly. The server checks it; this does not. */
    delete: (cohort: string, confirm: string, keepCapturing = false) =>
      post<DeleteReport>('/api/project/delete', {
        cohort,
        confirm,
        keep_capturing: keepCapturing,
      }),

    include: (project: string) =>
      post<{ project: string; removed: number }>('/api/project/include', { project }),
  },
}
