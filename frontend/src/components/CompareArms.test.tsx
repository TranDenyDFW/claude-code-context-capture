/**
 * The two pickers that decide what Compare is comparing.
 *
 * The case that matters is a chat against its own fork. Before this existed the page sent no
 * `compare_with` at all, so arm B was whatever the server's default picked and the question could
 * not be asked. These check that both arms reach the request, that they cannot be set to the same
 * thing, and that switching what arm B MEANS does not leave the old target behind.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { api, type Selection } from '@/api'
import { CompareArms } from './CompareArms'
import appSource from '../App.tsx?raw'

const PARENT = '928cf7e5-287f-4300-a03f-347d17719ae8'
const FORK = '0b487242-7f79-40f3-a1a6-75bf34771072'
const sessions = [
  { value: PARENT, label: 'P:\\ClaudeExt\\ccxe\\c4x · Status line documentation accuracy · 2026-09-01 02:26' },
  { value: FORK, label: 'P:\\ClaudeExt\\ccxe\\c4x · Status line documentation accuracy (fork) · 2026-08-30 21:08' },
  { value: 'other', label: 'F:\\SecDb · Something else · 2026-07-08 00:37' },
]
const cohorts = [
  { value: '__all__', label: 'All sessions (317)' },
  { value: 'project::F:\\SecDb', label: 'Project: F:\\SecDb (13)' },
]

function show(selection: Selection = {}) {
  const onChange = vi.fn()
  render(
    <CompareArms selection={selection} sessions={sessions} cohorts={cohorts} onChange={onChange} />,
  )
  const [armA, armB] = screen.getAllByRole('combobox') as HTMLSelectElement[]
  return { onChange, armA, armB }
}

describe('choosing the two arms', () => {
  it('offers every chat on both arms', () => {
    const { armA, armB } = show()
    // Three chats plus the "whole store" entry.
    expect(armA.options.length).toBe(4)
    expect(armB.options.length).toBe(4)
  })

  it('sets arm A through the SAME field the header chip shows', () => {
    // Not a second copy of the selection. Two states for one fact drift, and the header would then
    // name a session Compare was not using.
    const { onChange, armA } = show()
    fireEvent.change(armA, { target: { value: PARENT } })
    expect(onChange).toHaveBeenCalledWith({ session: PARENT })
  })

  it('sends arm B with the kind it was picked under', () => {
    const { onChange, armB } = show({ session: PARENT })
    fireEvent.change(armB, { target: { value: FORK } })
    expect(onChange).toHaveBeenCalledWith({ compareWith: FORK, compareKind: 'session' })
  })

  it('keeps the same chat out of the other arm', () => {
    // Comparing something with itself renders a table of 1.0 ratios and no finding.
    const { armB } = show({ session: PARENT })
    expect([...armB.options].map((o) => o.value)).not.toContain(PARENT)
    expect([...armB.options].map((o) => o.value)).toContain(FORK)
  })

  it('says so when arm B is unset, rather than looking chosen', () => {
    show({ session: PARENT })
    // The paragraph explaining an unset arm B is gone: it restated the empty picker beside it.
    // What still has to be true is that the picker does not LOOK chosen.
    expect(screen.queryByText(/Arm B is unset/)).toBeNull()
    expect((screen.getAllByRole('combobox')[1] as HTMLSelectElement).value).toBe('')
  })

  it('warns rather than silently comparing a chat with itself', () => {
    show({ session: PARENT, compareWith: PARENT })
    expect(screen.getByText(/1\.0 ratios/)).toBeTruthy()
  })
})

describe('switching what arm B means', () => {
  it('offers populations instead of chats', () => {
    const { armB } = show({ session: PARENT, compareKind: 'cohort' })
    expect([...armB.options].map((o) => o.value)).toContain('project::F:\\SecDb')
    expect([...armB.options].map((o) => o.value)).not.toContain(FORK)
  })

  it('clears the target with the kind', () => {
    // A session id left behind under compareKind=cohort makes the server resolve a population that
    // does not exist, and the pane comes back describing nothing with no error to explain it.
    const { onChange } = show({ session: PARENT, compareWith: FORK })
    fireEvent.click(screen.getByRole('button', { name: 'Population' }))
    expect(onChange).toHaveBeenCalledWith({ compareKind: 'cohort', compareWith: null })
  })
})

describe('what the request carries', () => {
  it('sends compare_with and compare_kind to the server', async () => {
    const fetched = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
    await api.tab('tab-compare', { session: PARENT, compareWith: FORK, compareKind: 'session' })
    const url = String(fetched.mock.calls[0][0])
    expect(url).toContain(`session=${PARENT}`)
    expect(url).toContain(`compare_with=${FORK}`)
    expect(url).toContain('compare_kind=session')
    fetched.mockRestore()
  })

  it('sends no compare_kind when there is no arm B', () => {
    // The server defaults `compare_kind` to "session"; sending a kind with no target says a choice
    // was made when none was.
    const fetched = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
    void api.tab('tab-compare', { session: PARENT })
    expect(String(fetched.mock.calls[0][0])).not.toContain('compare_kind')
    fetched.mockRestore()
  })
})

describe('how App wires it up', () => {
  it('keys the pane query on the compare arms', () => {
    /**
     * A SOURCE CHECK, because the failure is invisible.
     *
     * react-query caches by key. A key that omits `compareWith` files two different requests under
     * one entry, so the arm-B picker moves, the request is never made, and the pane keeps showing
     * the previous comparison with nothing on screen saying it is stale.
     */
    const at = appSource.indexOf("queryKey: ['tab'")
    expect(at).toBeGreaterThan(-1)
    const key = appSource.slice(at, appSource.indexOf(']', at))
    expect(key).toContain('compareWith')
    expect(key).toContain('compareKind')
  })

  it('renders the arms on the Compare tab only', () => {
    // A chat picker in the header was removed on purpose: nobody recalls a session by name out of
    // 317. It belongs here, where the reader already knows which two they mean.
    expect(appSource).toMatch(/tab === 'tab-compare' && \(/)
  })
})
