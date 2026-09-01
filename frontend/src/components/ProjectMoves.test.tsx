/**
 * The controls that can delete a project.
 *
 * Everything here is about one character. The confirmation must match the path EXACTLY, the cohort
 * must reach the server as `project::<path>` and not as a bare path, and a server with writes off
 * must refuse before anything is attempted rather than fail on click.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { api } from '@/api'
import { ProjectMoves, pathOf } from './ProjectMoves'
// Vite's ?raw import, not node:fs. The app tsconfig types `vite/client` and NOT `node`, so
// readFileSync/process do not type-check here at all: `npm run typecheck` reported three
// TS2591s that a bare `tsc --noEmit` never ran.
import appSource from '../App.tsx?raw'

const PROJECT = 'F:\\SecDb'
const COHORT = `project::${PROJECT}`
const cohorts = [
  { value: '__all__', label: 'All sessions (317)' },
  { value: COHORT, label: 'Project: F:\\SecDb (12)' },
]

function show(props: Partial<Parameters<typeof ProjectMoves>[0]> = {}) {
  const onChanged = vi.fn()
  render(
    <ProjectMoves cohort={COHORT} cohorts={cohorts} writesEnabled onChanged={onChanged} {...props} />,
  )
  fireEvent.click(screen.getByRole('button', { name: /project/i }))
  return { onChanged }
}

const confirmField = () => screen.getByLabelText(/type the project path to confirm/i)
// `.disabled` rather than toBeDisabled(): this project does not load @testing-library/jest-dom,
// and an absent matcher throws a bare 'not a function' that reads like a component fault.
const deleteButton = () => screen.getByRole('button', { name: /^delete/i }) as HTMLButtonElement
const fileField = () => screen.getByLabelText(/choose an exported project file/i)

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('reading a cohort', () => {
  it('gives the path back for a project cohort', () => {
    expect(pathOf(COHORT)).toBe(PROJECT)
  })

  it('refuses a bare path, which the store reads as NO restriction', () => {
    // The bug this app already shipped once: an unprefixed path filters nothing, so treating it
    // as a project here would offer to delete something the server would refuse to identify.
    expect(pathOf(PROJECT)).toBeNull()
  })

  it('refuses a section cohort and an empty one', () => {
    expect(pathOf('section::Engineering')).toBeNull()
    expect(pathOf('')).toBeNull()
    expect(pathOf(null)).toBeNull()
  })

  it('keeps a path that itself contains a colon pair', () => {
    expect(pathOf('project::C:\\a::b')).toBe('C:\\a::b')
  })
})

describe('delete', () => {
  it('stays disabled until the typed path matches exactly', () => {
    show()
    expect(deleteButton().disabled).toBe(true)

    fireEvent.change(confirmField(), { target: { value: 'F:\\SecD' } })
    expect(deleteButton().disabled).toBe(true)

    // One character of case. This is the whole point of typing it rather than clicking Yes.
    fireEvent.change(confirmField(), { target: { value: 'f:\\SecDb' } })
    expect(deleteButton().disabled).toBe(true)

    // A trailing space is not the path either.
    fireEvent.change(confirmField(), { target: { value: 'F:\\SecDb ' } })
    expect(deleteButton().disabled).toBe(true)

    fireEvent.change(confirmField(), { target: { value: PROJECT } })
    expect(deleteButton().disabled).toBe(false)
  })

  it('sends the cohort untouched, not the bare path', async () => {
    const sent = vi.spyOn(api.project, 'delete').mockResolvedValue({
      project: PROJECT, backup: 'tmp/x.db', removed: { turns: 3 }, excluded: true,
    })
    const { onChanged } = show()
    fireEvent.change(confirmField(), { target: { value: PROJECT } })
    fireEvent.click(deleteButton())
    await screen.findByText(/Deleted/)
    expect(sent).toHaveBeenCalledWith(COHORT, PROJECT, false)
    expect(onChanged).toHaveBeenCalled()
  })

  it('passes keep-capturing through when it is ticked', async () => {
    const sent = vi.spyOn(api.project, 'delete').mockResolvedValue({
      project: PROJECT, backup: 'tmp/x.db', removed: {}, excluded: false,
    })
    show()
    fireEvent.click(screen.getByLabelText(/keep capturing/i))
    fireEvent.change(confirmField(), { target: { value: PROJECT } })
    fireEvent.click(deleteButton())
    await screen.findByText(/Deleted/)
    expect(sent).toHaveBeenCalledWith(COHORT, PROJECT, true)
  })

  it('shows what the server refused instead of failing silently', async () => {
    const { ApiError } = await import('@/api')
    vi.spyOn(api.project, 'delete').mockRejectedValue(
      new ApiError('409 from /api/project/delete', 409, {
        error: 'confirmation does not match the project path; nothing was deleted',
      }),
    )
    show()
    fireEvent.change(confirmField(), { target: { value: PROJECT } })
    fireEvent.click(deleteButton())
    expect(await screen.findByText(/nothing was deleted/)).toBeTruthy()
  })

  it('says the project is still being captured when it was kept', async () => {
    vi.spyOn(api.project, 'delete').mockResolvedValue({
      project: PROJECT, backup: 'tmp/x.db', removed: {}, excluded: false,
    })
    show()
    fireEvent.click(screen.getByLabelText(/keep capturing/i))
    fireEvent.change(confirmField(), { target: { value: PROJECT } })
    fireEvent.click(deleteButton())
    expect(await screen.findByText(/returns on the next harvest/)).toBeTruthy()
  })
})

describe('export', () => {
  it('links to the cohort, encoded, with nothing taken apart', () => {
    show()
    const link = screen.getByRole('link', { name: /export/i })
    expect(link.getAttribute('href')).toBe(
      `/api/project/export?cohort=${encodeURIComponent(COHORT)}`,
    )
  })

  it('offers nothing to export when the population is not a project', () => {
    show({ cohort: '__all__' })
    expect(screen.queryByRole('link', { name: /export/i })).toBeNull()
    expect(screen.getAllByText(/Choose a project under Population first/).length).toBe(2)
  })
})

describe('a server started with --no-writes', () => {
  it('says so and offers no working control', () => {
    show({ writesEnabled: false })
    expect(screen.getByText(/--no-writes/)).toBeTruthy()
    // Not a link at all: an anchor with its href removed is announced as plain text, so the
    // control becomes a real disabled button instead.
    expect(screen.queryByRole('link', { name: /export/i })).toBeNull()
    const exportButton = screen.getByRole('button', { name: /^export/i }) as HTMLButtonElement
    expect(exportButton.disabled).toBe(true)
    fireEvent.change(confirmField(), { target: { value: PROJECT } })
    expect(deleteButton().disabled).toBe(true)
  })
})

describe('how App wires it up', () => {
  /**
   * A SOURCE CHECK, because types cannot catch this one.
   *
   * `/api/health` returns two booleans: `read_only`, which is always true on the API server, and
   * `writes_enabled`, which is what these controls depend on. Passing the wrong one compiles
   * cleanly and disables every control on every server, or enables them on a server that will
   * refuse. Nothing in the type system can tell one boolean from the other.
   */
  it('passes writes_enabled, not read_only', () => {
    const at = appSource.indexOf('<ProjectMoves')
    expect(at).toBeGreaterThan(-1)
    const element = appSource.slice(at, appSource.indexOf('/>', at))
    // The ASSIGNMENT, not the whole element: the comment beside it names both fields on purpose,
    // and a substring check over the block failed on its own explanation.
    expect(element).toMatch(/writesEnabled=\{[^}]*writes_enabled/)
    expect(element).not.toMatch(/writesEnabled=\{[^}]*read_only/)
  })
})

describe('import', () => {
  it('reports what landed, what was already here, and what was dropped', async () => {
    vi.spyOn(api.project, 'import').mockResolvedValue({
      project: PROJECT,
      from: 'PONPON',
      still_excluded: false,
      inserted: { turns: 25964, messages: 22416, files: 0 },
      already_present: { turns: 0, sessions: 17 },
      dropped_columns: { sessions: ['from_the_future'] },
    })
    show()
    const file = new File([new Uint8Array([1, 2, 3])], 'secdb.db')
    fireEvent.change(fileField(), { target: { files: [file] } })
    expect(await screen.findByText(/Imported/)).toBeTruthy()
    // Counts of zero are left out: a table that gained nothing is noise in a success report.
    expect(screen.getByText(/turns 25,964 · messages 22,416/)).toBeTruthy()
    expect(screen.getByText(/sessions 17/)).toBeTruthy()
    expect(screen.getByText(/from_the_future/)).toBeTruthy()
  })

  it('offers to resume capturing when the rows are back but the exclusion is not', async () => {
    // Restoring a project and leaving harvest skipping the directory means every session run
    // there since is dropped, with nothing on the page connecting the two facts.
    vi.spyOn(api.project, 'import').mockResolvedValue({
      project: PROJECT, from: 'PONPON', still_excluded: true,
      inserted: { turns: 10 }, already_present: {}, dropped_columns: {},
    })
    const lift = vi.spyOn(api.project, 'include').mockResolvedValue({ project: PROJECT, removed: 1 })
    vi.spyOn(api.project, 'excluded').mockResolvedValue({ excluded: [], writes_enabled: true })
    show()
    fireEvent.change(fileField(), { target: { files: [new File(['x'], 'x.db')] } })
    fireEvent.click(await screen.findByRole('button', { name: /resume capturing/i }))
    await vi.waitFor(() =>
      expect(screen.queryByRole('button', { name: /resume capturing/i })).toBeNull())
    expect(lift).toHaveBeenCalledWith(PROJECT)
  })

  it('offers nothing to resume when the project is not excluded', async () => {
    vi.spyOn(api.project, 'import').mockResolvedValue({
      project: PROJECT, from: 'PONPON', still_excluded: false,
      inserted: { turns: 10 }, already_present: {}, dropped_columns: {},
    })
    show()
    fireEvent.change(fileField(), { target: { files: [new File(['x'], 'x.db')] } })
    await screen.findByText(/Imported/)
    expect(screen.queryByRole('button', { name: /resume capturing/i })).toBeNull()
  })
})

