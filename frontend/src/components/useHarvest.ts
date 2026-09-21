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
 *
 * A FINISHED JOB IS NEWS WHOEVER RAN IT. The reload was first keyed on watching a job go from
 * running to not running, which misses any job that ends before this instance sees it running:
 * a quick fold started by the maintenance strip, an update run in another tab. So the last
 * finished job's id is remembered, and a new one the instance was not following reloads the
 * page all the same (once, and never for a dry run, which wrote nothing). What the page found
 * already finished when it opened is history, not news.
 *
 * EVERY RUN THIS FOLLOWS IS HELD TO ITS ID, whoever started it: the run this page posted, a run
 * it found already under way, a run a 409 pointed it at. The id names the server process too, so
 * a run that a restart killed is never reported with the result of some later run.
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
  const expected = useRef<string | null>(null)
  // The status stopped answering while a run was being followed. NOT the end of following it:
  // one failed poll (a sleep, a network change) while the server and the harvester carry on is
  // the common case, and dropping the run there left the page saying the outcome was unknown
  // for ever and never reloading data the run did write.
  const lost = useRef(false)
  // The id of the last finished job this instance has accounted for; undefined until the
  // first status arrives.
  const seen = useRef<string | null | undefined>(undefined)
  const waiter = useRef<((outcome: HarvestOutcome | null) => void) | null>(null)
  const changed = useRef(onChanged)
  useEffect(() => { changed.current = onChanged }, [onChanged])

  const query = useQuery({
    queryKey: ['harvest'],
    // An arrow, so a test that forgot to mock this gets a query error, not a render crash.
    queryFn: () => api.harvest.state(),
    retry: false,
    // A HIDDEN TAB DOES NOT POLL: the query library skips interval fetches while the document
    // is hidden, by design, and this leaves that default alone (no work for a page nobody is
    // looking at). Seen on the first end-to-end run of this: the run ended while the pane was
    // hidden and the page still said Updating. It catches up by itself when shown again,
    // because the same interval resumes on its next tick; the focus refetch below is for the
    // IDLE case, where the interval is thirty seconds and the freshness line would otherwise
    // be stale, and a job started in another tab unnoticed, for that long after a return.
    refetchOnWindowFocus: true,
    refetchInterval: (q) => {
      if (q.state.status === 'error') return lost.current ? Math.max(pollMs, 2_000) : idleMs
      return q.state.data?.running ? pollMs : idleMs
    },
  })
  const status: HarvestStatus | undefined = query.data

  const finish = useCallback((outcome: HarvestOutcome | null) => {
    const result = noteFor(outcome, expected.current)
    expected.current = null
    if (outcome) seen.current = outcome.id
    lost.current = false
    setFollowing(false)
    setNote(result)
    // A dry run wrote nothing. Everything else may have, a failure included: what a run stored
    // before it failed is in the store, and an unchanged store costs one cheap refetch.
    if (!outcome?.dry_run) changed.current?.()
    waiter.current?.(outcome)
    waiter.current = null
  }, [])

  useEffect(() => {
    if (!status || query.isError) return
    const now = Boolean(status.running)
    if (lost.current) {
      // The server answers again. Either the run is still going, and following resumes, or it
      // ended while nobody could see, and its id says whether that was this run.
      lost.current = false
      was.current = now
      if (now) {
        // Busy again through `status.running`; only the red note has to go.
        setNote(null)
      } else {
        finish(status.last ?? null)
      }
      return
    }
    if (now && !was.current && expected.current === null) {
      // A job this page did not start: another tab, or a reload mid-run. Follow it, by its id.
      expected.current = status.job?.id ?? null
      setNote({ text: 'An update is running.', tone: 'dim', stays: true })
    }
    if (was.current && !now) finish(status.last ?? null)
    was.current = now
    const lastId = status.last?.id ?? null
    if (seen.current === undefined) {
      seen.current = lastId
    } else if (lastId !== seen.current) {
      seen.current = lastId
      if (status.last && !status.last.dry_run) changed.current?.()
    }
  }, [status, query.dataUpdatedAt, query.isError, finish])

  // The server stopped answering while a job ran: say so and stop looking busy, but keep the
  // run's id and keep asking (the interval above shortens while `lost`).
  useEffect(() => {
    if (!query.isError || !was.current) return
    was.current = false
    lost.current = true
    setFollowing(false)
    setNote({ text: "C4X stopped answering; the update's outcome is not known yet.", tone: 'bad', stays: true })
  }, [query.isError])

  // A success is said for a moment; a note that stayed would read as a state.
  useEffect(() => {
    if (!note || note.stays) return
    const timer = setTimeout(() => setNote(null), noteMs)
    return () => clearTimeout(timer)
  }, [note, noteMs])

  // Nothing is left waiting on a page that went away.
  useEffect(() => () => {
    waiter.current?.(null)
    waiter.current = null
  }, [])

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
      const detail = problem instanceof ApiError
        ? (problem.detail as { reason?: string; job?: { id?: string } | null } | undefined)
        : undefined
      const busy = problem instanceof ApiError && problem.status === 409 && detail?.reason === 'busy'
      const named = detail?.job?.id ?? null
      const now = (await query.refetch()).data
      if (busy && now?.running) {
        // A JOB ALREADY RUNNING IS NOT AN ERROR: follow it, held to the id the refusal named.
        expected.current = now.job?.id ?? named
        was.current = true
        setFollowing(true)
        setNote({ text: 'An update was already running; waiting for it.', tone: 'dim', stays: true })
      } else if (busy) {
        // It ended between the refusal and this look. That is a finished run, not a failed
        // click: say how it went (by its id, when the refusal named one) and reload.
        expected.current = named
        was.current = false
        setStarting(false)
        finish(now?.last ?? null)
        return done
      } else {
        waiter.current = null
        setNote({ text: `Update failed: ${said(problem)}`, tone: 'bad', stays: true })
        setStarting(false)
        return null
      }
    }
    setStarting(false)
    return done
  }, [query, starting, finish])

  const busy = (starting || following || Boolean(status?.running)) && !query.isError
  return { status, busy, note, start, now: query.dataUpdatedAt }
}
