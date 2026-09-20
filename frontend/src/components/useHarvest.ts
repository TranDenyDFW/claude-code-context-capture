import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ApiError, api, said, type HarvestKind, type HarvestOutcome, type HarvestStatus } from '@/api'
import { noteFor, type Note } from './harvest'

/**
 * Follow the server's one harvest job: whether there can be one, the one running, the one that
 * just ended. Shared by the header's Update data and anything else that starts a job, through
 * one query key, so two places never disagree about whether an update is running.
 *
 * THE SERVER IS THE CLOCK. The control is busy exactly as long as the server says a job runs;
 * there is no deadline here that could flip it back while node is still writing. A catch-up has
 * taken 54 minutes on a real machine, and a job that truly hung is the server's to time out and
 * report. A page reloaded mid-run therefore comes up already busy, and a run started in another
 * tab is followed the same way.
 *
 * COMPLETION IS ONE PATH: `running` going from true to false. `start` marks that a job exists
 * the moment the POST is accepted, because a harvest usually finishes (p50 under a second)
 * before the first poll could ever see it running.
 */
export function useHarvest({
  onChanged,
  pollMs = 500,
  idleMs = 30_000,
  noteMs = 4_000,
}: {
  /** Called once per finished run that may have written, success or not; the parent refetches. */
  onChanged?: () => void
  pollMs?: number
  idleMs?: number
  noteMs?: number
} = {}) {
  const [note, setNote] = useState<Note | null>(null)
  const [starting, setStarting] = useState(false)
  // A job this page is following, as STATE, because `busy` is read while rendering and a ref
  // read there is a value React cannot see change. `was` below is the same fact for the
  // effects, which need it without waiting for a render.
  const [following, setFollowing] = useState(false)
  const was = useRef(false)
  const expected = useRef<number | null>(null)
  const waiter = useRef<((outcome: HarvestOutcome | null) => void) | null>(null)
  const changed = useRef(onChanged)
  useEffect(() => { changed.current = onChanged }, [onChanged])

  const query = useQuery({
    queryKey: ['harvest'],
    // An arrow, so a test that forgot to mock this gets a query error, not a render crash.
    queryFn: () => api.harvest.state(),
    retry: false,
    refetchOnWindowFocus: true,
    refetchInterval: (q) =>
      q.state.status === 'error' ? idleMs : q.state.data?.running ? pollMs : idleMs,
  })
  const status: HarvestStatus | undefined = query.data

  const finish = useCallback((outcome: HarvestOutcome | null) => {
    const result = noteFor(outcome, expected.current)
    expected.current = null
    setFollowing(false)
    setNote(result)
    // A dry run wrote nothing. Everything else may have, a failure included: what a run stored
    // before it failed is in the store, and an unchanged store costs one cheap refetch.
    if (!outcome?.dry_run) changed.current?.()
    waiter.current?.(outcome)
    waiter.current = null
  }, [])

  useEffect(() => {
    const now = Boolean(status?.running)
    if (now && !was.current && expected.current === null) {
      // A job this page did not start: another tab, or a reload mid-run. Follow it.
      setNote({ text: 'An update is running.', tone: 'dim', stays: true })
    }
    if (was.current && !now) finish(status?.last ?? null)
    was.current = now
  }, [status, query.dataUpdatedAt, finish])

  // The server stopped answering while a job ran: say so, and do not stay busy for ever.
  useEffect(() => {
    if (!query.isError || !was.current) return
    was.current = false
    expected.current = null
    setFollowing(false)
    setNote({ text: "C4X stopped answering; the update's outcome is not known.", tone: 'bad', stays: true })
    waiter.current?.(null)
    waiter.current = null
  }, [query.isError])

  // A success is said for a moment; a note that stayed would read as a state.
  useEffect(() => {
    if (!note || note.stays) return
    const timer = setTimeout(() => setNote(null), noteMs)
    return () => clearTimeout(timer)
  }, [note, noteMs])

  const start = useCallback(async (kind: HarvestKind = 'incremental', dryRun = false) => {
    if (starting || was.current) return null
    setStarting(true)
    setNote(null)
    const done = new Promise<HarvestOutcome | null>((resolve) => { waiter.current = resolve })
    try {
      const accepted = await api.harvest.run(kind, dryRun)
      expected.current = accepted.id
      was.current = true
      setFollowing(true)
      await query.refetch()
    } catch (problem) {
      // 409 IS TWO THINGS: a job already running, which is not an error (follow it), and a
      // refusal. The status says which.
      const now = (await query.refetch()).data
      if (problem instanceof ApiError && problem.status === 409 && now?.running) {
        was.current = true
        setFollowing(true)
        setNote({ text: 'An update was already running; waiting for it.', tone: 'dim', stays: true })
      } else {
        waiter.current = null
        setNote({ text: `Update failed: ${said(problem)}`, tone: 'bad', stays: true })
        setStarting(false)
        return null
      }
    }
    setStarting(false)
    return done
  }, [query, starting])

  const busy = (starting || following || Boolean(status?.running)) && !query.isError
  return { status, busy, note, start, now: query.dataUpdatedAt }
}
