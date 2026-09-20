import type { HarvestKind, HarvestOutcome, HarvestStatus } from '@/api'

/**
 * The words of the Update data control, apart from the control, the way `restart.ts` keeps the
 * restart sentences: pure functions a test can hold to the letter without drawing anything.
 */

/** What a running job is doing, for the hover. */
export const DOING: Record<HarvestKind, string> = {
  incremental: 'Reading new transcripts',
}

/**
 * How a reader with a store that lacks a table gets one. Said in two places (the chat drawer and
 * the chat window), so it is one constant. It stays true on a server where the button is off, and
 * it is honest that the sidecar backfill is not something the page runs.
 */
export const HOW_TO_HARVEST =
  "Update data, in the dashboard's header, creates what is missing and fills it from then on; " +
  'where that button is off, node tools/harvest.mjs in a terminal does the same. Work already ' +
  'on disk needs node tools/harvest.mjs --backfill-sidecars, which the page does not run.'

export type Tone = 'good' | 'warn' | 'bad' | 'dim'

export interface Note {
  text: string
  tone: Tone
  /** A failure stays until the next run; everything else is said for a moment and goes. */
  stays: boolean
}

function plural(n: number, one: string): string {
  return `${n.toLocaleString('en-US')} ${n === 1 ? one : `${one}s`}`
}

/** How long ago, in words a header can afford: a minute, minutes, hours, then the date. */
export function ago(iso: string, now: number): string {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return iso
  const minutes = Math.floor((now - then) / 60_000)
  if (minutes < 1) return 'less than a minute ago'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  return `on ${iso.slice(0, 16).replace('T', ' ')}`
}

/** How fresh the store is, whoever updated it last. The table does not record who. */
export function freshness(last: HarvestStatus['last_harvest'], now: number): string {
  if (!last) return 'This store has not been updated yet.'
  const took = last.ms >= 60_000
    ? `${Math.round(last.ms / 60_000)} min`
    : last.ms >= 1_000 ? `${(last.ms / 1_000).toFixed(1)} s` : `${last.ms} ms`
  return `Store last updated ${ago(last.ts, now)}: ${plural(last.files_read, 'transcript')} read in ${took}.`
}

/** The hover: what the button does, then how fresh the store is, or why it is off. */
export function hoverFor(status: HarvestStatus, now: number): string {
  if (!status.enabled) {
    return [`Update data is off on this server: ${status.why_not ?? 'it will not run the harvester.'}`,
      status.fix, freshness(status.last_harvest, now)].filter(Boolean).join('\n')
  }
  if (status.running && status.job) {
    return `${DOING[status.job.kind] ?? 'Updating'} for ${Math.round(status.job.elapsed_s)} s. ` +
      'The tables reload when it finishes.'
  }
  const lines = [
    'Read what Claude has written since the last update into the store, then reload everything ' +
    'on this page. Nothing is closed or restarted, so this does not ask first.',
    freshness(status.last_harvest, now),
  ]
  if (status.last && (!status.last.ok || status.last.partial)) {
    lines.push(`The last update from this page: ${status.last.sentence}`)
  }
  return lines.join('\n')
}

/** What to say beside the button when a run this page followed has ended. */
export function noteFor(outcome: HarvestOutcome | null, expected: number | null): Note {
  // THE ID IS THE PROOF. A server restarted mid-run comes back with no last job, or with an
  // older one, and "not running any more" alone would have been reported as a success.
  if (!outcome || (expected !== null && outcome.id !== expected)) {
    return {
      text: 'The update was interrupted (the server restarted); the page has reloaded its data.',
      tone: 'warn',
      stays: false,
    }
  }
  if (!outcome.ok) return { text: outcome.short || `Update failed: ${outcome.error ?? ''}`, tone: 'bad', stays: true }
  if (outcome.partial) return { text: outcome.short, tone: 'warn', stays: true }
  return { text: outcome.short, tone: 'good', stays: false }
}
