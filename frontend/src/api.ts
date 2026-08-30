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
}

/** Deliberately loose. This is handed straight to Plotly, which is the authority on its own shape. */
export interface PlotlyFigure {
  data: Record<string, unknown>[]
  layout?: Record<string, unknown>
}

export interface TabInfo {
  id: string
  label: string
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

export const api = {
  tabs: () => get<TabInfo[]>('/api/tabs'),
  health: () => get<{ ok: boolean; db: string; read_only: boolean; cache: Record<string, number> }>(
    '/api/health',
  ),
  sessions: (limit = 200) =>
    get<{ rows: SessionRow[]; total: number }>('/api/sessions', { limit }),
  /** The drawing shape: tables, plus every chart as full Plotly JSON. */
  tab: (id: string, selection: Selection = {}) =>
    get<TabPayload>(`/api/tab/${id}/render`, {
      session: selection.session,
      scope: selection.scope ?? 'main',
      cohort: selection.cohort,
    }),
}
