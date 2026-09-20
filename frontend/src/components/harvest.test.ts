import { describe, expect, it } from 'vitest'
import type { HarvestOutcome, HarvestStatus } from '@/api'
import { HOW_TO_HARVEST, ago, freshness, hoverFor, noteFor } from './harvest'

/**
 * The words of the Update data control. What matters: the freshness line never claims to know
 * WHO updated the store (the table does not record it), a failure is said in words and not only
 * in red, and a run the page started is only a success when the server names that same run.
 */

const NOW = Date.parse('2026-09-20T18:00:00.000Z')

const status = (over: Partial<HarvestStatus> = {}): HarvestStatus => ({
  enabled: true, reason: null, why_not: null, fix: null, running: false, job: null, last: null,
  last_harvest: { ts: '2026-09-20T17:57:00.000Z', mode: 'incremental', files_seen: 9599, files_read: 2, ms: 1412 },
  ...over,
})

const outcome = (over: Partial<HarvestOutcome> = {}): HarvestOutcome => ({
  id: 7, kind: 'incremental', dry_run: false, ok: true, partial: false,
  sentence: 'Read 3 of 9,599 transcripts.', short: 'Updated: 3 transcripts read.', seconds: 1.2,
  error: null, started_at: '2026-09-20T17:59:58.000Z', finished_at: '2026-09-20T17:59:59.200Z',
  ...over,
})

describe('ago', () => {
  it('says under a minute, minutes, hours, then the date', () => {
    expect(ago('2026-09-20T17:59:40.000Z', NOW)).toBe('less than a minute ago')
    expect(ago('2026-09-20T17:57:00.000Z', NOW)).toBe('3 min ago')
    expect(ago('2026-09-20T13:00:00.000Z', NOW)).toBe('5 h ago')
    expect(ago('2026-09-15T03:41:00.000Z', NOW)).toBe('on 2026-09-15 03:41')
  })

  it('hands back what it cannot read rather than inventing a time', () => {
    expect(ago('not a time', NOW)).toBe('not a time')
  })
})

describe('freshness', () => {
  it('says when, how many and how long, and never who', () => {
    const line = freshness(status().last_harvest, NOW)
    expect(line).toBe('Store last updated 3 min ago: 2 transcripts read in 1.4 s.')
    expect(line).not.toMatch(/hook|you|page/i)
  })

  it('one transcript is singular, and a long run is said in minutes', () => {
    expect(freshness({ ts: '2026-09-20T17:57:00.000Z', mode: 'incremental', files_seen: 9, files_read: 1, ms: 40 }, NOW))
      .toBe('Store last updated 3 min ago: 1 transcript read in 40 ms.')
    expect(freshness({ ts: '2026-09-20T17:57:00.000Z', mode: 'incremental', files_seen: 9599, files_read: 9599, ms: 3_223_801 }, NOW))
      .toContain('9,599 transcripts read in 54 min.')
  })

  it('says so when the store has never been updated', () => {
    expect(freshness(null, NOW)).toBe('This store has not been updated yet.')
  })
})

describe('hoverFor', () => {
  it('says what it does, that it does not ask, and how fresh the store is', () => {
    const text = hoverFor(status(), NOW)
    expect(text).toContain('Nothing is closed or restarted, so this does not ask first.')
    expect(text).toContain('Store last updated 3 min ago')
  })

  it('an off server leads with why, then the remedy, and still says how fresh the store is', () => {
    const text = hoverFor(status({
      enabled: false, reason: 'not-own-store',
      why_not: 'This server is serving a copy.', fix: 'Start the server without --db.',
    }), NOW)
    expect(text.split('\n')).toEqual([
      'Update data is off on this server: This server is serving a copy.',
      'Start the server without --db.',
      'Store last updated 3 min ago: 2 transcripts read in 1.4 s.',
    ])
  })

  it('a running job says what it is doing and for how long', () => {
    const text = hoverFor(status({
      running: true,
      job: { id: 3, kind: 'incremental', dry_run: false, started_at: '', elapsed_s: 41.6 },
    }), NOW)
    expect(text).toBe('Reading new transcripts for 42 s. The tables reload when it finishes.')
  })

  it('keeps a failed or partial last run in the hover, where its whole sentence fits', () => {
    const text = hoverFor(status({ last: outcome({ ok: false, sentence: 'The update failed: exited 1.' }) }), NOW)
    expect(text).toContain('The last update from this page: The update failed: exited 1.')
    expect(hoverFor(status({ last: outcome() }), NOW)).not.toContain('The last update from this page')
  })
})

describe('noteFor', () => {
  it('a success is the server’s short line, and goes after a moment', () => {
    expect(noteFor(outcome(), 7)).toEqual({ text: 'Updated: 3 transcripts read.', tone: 'good', stays: false })
  })

  it('a failure is said in words, in red, and stays', () => {
    const note = noteFor(outcome({ ok: false, short: 'Update failed: node would not start.' }), 7)
    expect(note).toEqual({ text: 'Update failed: node would not start.', tone: 'bad', stays: true })
  })

  it('a partial run is its own tone and stays, since part of it still needs doing', () => {
    const note = noteFor(outcome({ partial: true, short: 'Updated in part: 3 transcripts read, 1 pass failed.' }), 7)
    expect(note.tone).toBe('warn')
    expect(note.stays).toBe(true)
  })

  it('a run the server does not name is interrupted, never a success', () => {
    expect(noteFor(null, 7).text).toContain('interrupted')
    expect(noteFor(outcome({ id: 6 }), 7).text).toContain('interrupted')
    expect(noteFor(outcome({ id: 6 }), 7).tone).toBe('warn')
  })

  it('an adopted run has no id to hold it to, and is reported as it ended', () => {
    expect(noteFor(outcome({ id: 12 }), null).text).toBe('Updated: 3 transcripts read.')
  })
})

describe('HOW_TO_HARVEST', () => {
  it('names the button, the terminal for where it is off, and what the page does not run', () => {
    expect(HOW_TO_HARVEST).toContain('Update data')
    expect(HOW_TO_HARVEST).toContain('node tools/harvest.mjs')
    expect(HOW_TO_HARVEST).toContain('--backfill-sidecars, which the page does not run')
  })
})
