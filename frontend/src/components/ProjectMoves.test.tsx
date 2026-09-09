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
import type { ImportReport } from '@/api'
import { ProjectMoves, pathOf, slugFor } from './ProjectMoves'
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

/**
 * A whole import report, so a test states only the field it is about.
 *
 * Written out in full rather than partially, because the fields that were missing from the old
 * mocks are exactly the ones the page now renders, and a mock that omits them tests a response
 * shape the server never sends.
 */
function report(over: Partial<ImportReport> = {}): ImportReport {
  return {
    project: PROJECT,
    from: 'PONPON',
    into: [PROJECT],
    mapping: { [PROJECT]: PROJECT },
    not_moved: [],
    still_excluded: false,
    inserted: { turns: 10 },
    already_present: {},
    dropped_columns: {},
    app_state: {
      written: [{ relpath: 'a.jsonl', kind: 'transcript', path: 'x', exists: false }],
      replaced: [], replaced_shorter: [], refused: [],
      desktop: [{ path: 'r.json', cwd: PROJECT }],
      bytes: 1024, dry_run: false,
    },
    mirror: {
      ok: true, missing: [], differs: [], extra: [], unresolved: [],
      into: [PROJECT], not_carried: [],
    },
    ...over,
  }
}

/**
 * Pick a file, then commit. Two steps on purpose: the first is a DRY RUN that writes nothing and
 * reports where every file would land, so a wrong destination is visible before it lands.
 */
async function choose(file = new File([new Uint8Array([1, 2, 3])], 'secdb.db')) {
  fireEvent.change(fileField(), { target: { files: [file] } })
  return screen.findByRole('button', { name: /^Import$/ })
}

describe('import', () => {
  it('shows where the export would land, and writes nothing, before it is confirmed', async () => {
    const call = vi.spyOn(api.project, 'import').mockResolvedValue(report({ dry_run: true }))
    show()
    await choose()
    // The first call is the plan. Its third argument is what makes it a plan.
    expect(call).toHaveBeenCalledTimes(1)
    expect(call.mock.calls[0][2]).toBe(true)
    expect(screen.queryByText(/^Imported/)).toBeNull()
    expect((screen.getByLabelText(/working directory on this machine/i) as HTMLInputElement).value)
      .toBe(PROJECT)
  })

  it('lets the destination be changed and shows the directory it produces', async () => {
    // The requirement: the destination is the user's choice, and it is the CURRENT machine's
    // paths that get rebuilt from it. The slug is shown because a typo in a path is invisible and
    // the directory name it produces is not.
    const call = vi.spyOn(api.project, 'import').mockResolvedValue(report())
    show()
    const button = await choose()
    fireEvent.change(screen.getByLabelText(/working directory on this machine/i),
                     { target: { value: 'D:\\Work\\Alpha' } })
    expect(screen.getByText(/D--Work-Alpha/)).toBeTruthy()
    fireEvent.click(button)
    await screen.findByText(/^Imported/)
    expect(call.mock.calls[1][1]).toBe('D:\\Work\\Alpha')
  })

  it('stops presenting the plan as current once the destination is edited', async () => {
    // The dry run is computed ONCE, for the destination the export came from. Editing the field
    // is the only thing the field is for, and the overwrite count then describes paths under the
    // old slug. Found by an independent sweep of the branch.
    vi.spyOn(api.project, 'import').mockResolvedValue(report({
      dry_run: true,
      app_state: {
        written: [{ relpath: 'a.jsonl', kind: 'transcript', path: 'x', exists: true }],
        replaced: [], replaced_shorter: [], refused: [], desktop: [], bytes: 0, dry_run: true,
      },
    }))
    show()
    await choose()
    expect(screen.getByText(/over something already there/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText(/working directory on this machine/i),
                     { target: { value: 'D:\\Somewhere\\Else' } })
    expect(screen.queryByText(/over something already there/)).toBeNull()
    expect(screen.getByText(/does not apply to the path you typed/)).toBeTruthy()
  })

  it('reports what landed, what was already here, and what was dropped', async () => {
    vi.spyOn(api.project, 'import').mockResolvedValue(report({
      inserted: { turns: 25964, messages: 22416, files: 0 },
      already_present: { turns: 0, sessions: 17 },
      dropped_columns: { sessions: ['from_the_future'] },
    }))
    show()
    fireEvent.click(await choose())
    expect(await screen.findByText(/^Imported/)).toBeTruthy()
    // Counts of zero are left out: a table that gained nothing is noise in a success report.
    expect(screen.getByText(/turns 25,964 · messages 22,416/)).toBeTruthy()
    expect(screen.getByText(/sessions 17/)).toBeTruthy()
    expect(screen.getByText(/from_the_future/)).toBeTruthy()
  })

  it('says the desktop app record was restored, because that is what makes it open there',
     async () => {
       vi.spyOn(api.project, 'import').mockResolvedValue(report())
       show()
       fireEvent.click(await choose())
       await screen.findByText(/^Imported/)
       expect(screen.getByText(/desktop app record/)).toBeTruthy()
     })

  it('does not paint an undone delete red', async () => {
    // `delete` writes its backup with app_state off, so every undo of a delete imports a rows-only
    // export. `missing` and `differs` are both empty when nothing was carried, so reading `ok`
    // alone rendered "NOT a mirror. 0 missing, 0 different:" over a correct restore, naming
    // nothing. Found by an independent reviewer against the page, not the CLI.
    vi.spyOn(api.project, 'import').mockResolvedValue(report({
      mirror: {
        ok: false, missing: [], differs: [], extra: [], unresolved: [],
        into: [PROJECT], not_carried: [], carries_no_files: true,
      },
    }))
    show()
    fireEvent.click(await choose())
    await screen.findByText(/^Imported/)
    expect(screen.queryByText(/NOT a mirror/)).toBeNull()
    expect(screen.getByText(/carries rows only/)).toBeTruthy()
  })

  it('does not claim byte for byte, which is not true of all five kinds', async () => {
    // The CLI was corrected off that wording because a config entry and a desktop record are
    // rewritten by design; the page kept it.
    vi.spyOn(api.project, 'import').mockResolvedValue(report())
    show()
    fireEvent.click(await choose())
    await screen.findByText(/^Imported/)
    expect(screen.queryByText(/[Bb]yte for byte/)).toBeNull()
    expect(screen.getByText(/Every carried file is identical/)).toBeTruthy()
  })

  it('does NOT call a failed mirror an import', async () => {
    // A 200 with a non-empty `differs` means files landed and are not what the export carries.
    // Reporting that as a success is the exact claim this whole change exists to stop.
    vi.spyOn(api.project, 'import').mockResolvedValue(report({
      mirror: {
        ok: false,
        missing: [{ relpath: 'memory/notes.md', kind: 'memory' }],
        differs: [{ relpath: 's0-0.jsonl', kind: 'transcript' }],
        extra: [], unresolved: [], into: [PROJECT], not_carried: [],
      },
    }))
    show()
    fireEvent.click(await choose())
    await screen.findByText(/^Imported/)
    expect(screen.getByText(/NOT a mirror/)).toBeTruthy()
    expect(screen.getByText(/s0-0\.jsonl/)).toBeTruthy()
  })

  it('names a file that was replaced with a shorter one', async () => {
    // A compacted transcript is NEWER and SHORTER, so source-always-wins can replace a longer
    // local record with less conversation. That was the choice; doing it quietly was not.
    vi.spyOn(api.project, 'import').mockResolvedValue(report({
      app_state: {
        written: [], replaced: [], refused: [], desktop: [], bytes: 0, dry_run: false,
        replaced_shorter: [{ relpath: 's0-0.jsonl', kind: 'transcript', was: 900, now: 40 }],
      },
    }))
    show()
    fireEvent.click(await choose())
    await screen.findByText(/^Imported/)
    expect(screen.getByText(/SHORTER/)).toBeTruthy()
  })

  it('offers to resume capturing when the rows are back but the exclusion is not', async () => {
    // Restoring a project and leaving harvest skipping the directory means every session run
    // there since is dropped, with nothing on the page connecting the two facts.
    vi.spyOn(api.project, 'import').mockResolvedValue(report({ still_excluded: true }))
    const lift = vi.spyOn(api.project, 'include').mockResolvedValue({ project: PROJECT, removed: 1 })
    vi.spyOn(api.project, 'excluded').mockResolvedValue({ excluded: [], writes_enabled: true })
    show()
    fireEvent.click(await choose())
    fireEvent.click(await screen.findByRole('button', { name: /resume capturing/i }))
    await vi.waitFor(() =>
      expect(screen.queryByRole('button', { name: /resume capturing/i })).toBeNull())
    expect(lift).toHaveBeenCalledWith(PROJECT)
  })

  it('offers nothing to resume when the project is not excluded', async () => {
    vi.spyOn(api.project, 'import').mockResolvedValue(report())
    show()
    fireEvent.click(await choose())
    await screen.findByText(/^Imported/)
    expect(screen.queryByRole('button', { name: /resume capturing/i })).toBeNull()
  })
})

describe('slugFor', () => {
  // A SECOND COPY of `c4x/appstate.py::slug_for`, pinned on the same cases the Python self-test
  // pins, so the two cannot drift silently.
  it('turns every character that is not a letter or a digit into a hyphen', () => {
    expect(slugFor('P:\\Skills')).toBe('P--Skills')
    expect(slugFor('D:\\Work\\Alpha')).toBe('D--Work-Alpha')
    expect(slugFor('S:\\www.sec.gov\\Archives')).toBe('S--www-sec-gov-Archives')
    expect(slugFor('P:\\cSrc\\dual_skill_package')).toBe('P--cSrc-dual-skill-package')
    expect(slugFor('P:\\VSA Agent GP')).toBe('P--VSA-Agent-GP')
  })

  it('trims a trailing separator the way check_destination does', () => {
    // The preview showed `D--Work-Alpha-` for a path the server would file under `D--Work-Alpha`,
    // on the one input a reader is most likely to paste. Found by an independent sweep.
    expect(slugFor('D:\\Work\\Alpha\\')).toBe('D--Work-Alpha')
    expect(slugFor('D:/Work/Alpha/')).toBe('D--Work-Alpha')
    expect(slugFor('  D:\\Work\\Alpha  ')).toBe('D--Work-Alpha')
  })
})

