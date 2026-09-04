/**
 * The pure half of the inspector: what a point carries, which rows are actually about it, and what
 * a link to those rows must say.
 *
 * The defect these pin: the first version took customdata[0] as "the session id". The server puts
 * the TITLE there and the id third, so on a store where many sessions share a title (210 of 1,327
 * have none at all) every point named itself "(untitled)" and claimed all 49 of them as its rows.
 */
import { describe, expect, it } from 'vitest'
import { candidates, describePoint, locate, visibleValue } from './inspect'

const sessions = {
  id: 'tbl-session',
  columns: ['title', 'project', 'peak'],
  rows: [
    { session_id: 's1', title: '(untitled)', project: 'P:\\x', peak: 10 },
    { session_id: 's2', title: '(untitled)', project: 'P:\\y', peak: 20 },
    { session_id: 's3', title: 'the one', project: 'P:\\x', peak: 30 },
  ],
}
const projects = { id: 'tbl-projects', columns: ['project', 'n'], rows: [{ project: 'P:\\x', n: 3 }] }
const meta = [
  {
    id: 'tbl-session', title: 'Sessions', columns: [
      { id: 'session_id', label: 'session', numeric: false, specifier: null, align: 'left' as const, hidden: true, bands: [] },
      { id: 'title', label: 'Title', numeric: false, specifier: null, align: 'left' as const, hidden: false, bands: [] },
      { id: 'project', label: 'Project', numeric: false, specifier: null, align: 'left' as const, hidden: false, bands: [] },
    ], filterable: true, page_size: null,
  },
  { id: 'tbl-projects', title: 'Projects', columns: [], filterable: true, page_size: null },
]
const names = ['Sessions', 'Projects']

// The Sessions chart's real shape: [title, project, compactions, session_id].
const sessionPoint = {
  traceName: 'Projects (269)', x: 12, y: 999, pointIndex: 0,
  customdata: ['(untitled)', 'P:\\x', 0, 's2'],
}

describe('what a point carries', () => {
  it('offers every string in customdata, then the label, then a categorical axis', () => {
    expect(candidates(sessionPoint)).toEqual(['(untitled)', 'P:\\x', 's2'])
    expect(candidates({ traceName: null, x: 'P:\\x', y: 3, label: 'cell', pointIndex: 0 }))
      .toEqual(['cell', 'P:\\x'])
    expect(candidates({ traceName: null, x: 158, y: 5, pointIndex: 0 })).toEqual([])
  })
})

describe('locating the rows a point is about', () => {
  it('prefers the key column, so a session is its own row and not everything sharing its title', () => {
    const found = locate([sessions, projects], candidates(sessionPoint))
    expect(found).toMatchObject({ index: 0, value: 's2', key: 'session_id' })
    expect(found?.rows).toEqual([sessions.rows[1]])
  })

  it('REFUSES a candidate that matches every row of a table with more than one', () => {
    const all = { id: 't', columns: ['title'], rows: [{ title: 'same' }, { title: 'same' }] }
    expect(locate([all], ['same'])).toBeNull()
  })

  it('but a one-row table IS its row, so its only match counts', () => {
    expect(locate([projects], ['P:\\x'])).toMatchObject({ index: 0, value: 'P:\\x', key: null })
  })

  it('falls back to the most specific non-key match', () => {
    expect(locate([sessions, projects], ['P:\\x'])).toMatchObject({ index: 1, value: 'P:\\x', key: null })
  })

  it('gives null for a value nowhere, and for a substring of one (gate can fail)', () => {
    expect(locate([sessions, projects], ['nowhere'])).toBeNull()
    expect(locate([sessions, projects], ['s'])).toBeNull()
  })
})

describe('the value a link can filter by', () => {
  it('is null when the match is on a hidden column, since a text search would never find it', () => {
    const found = locate([sessions], candidates(sessionPoint))!
    expect(visibleValue(found, meta[0], sessions)).toBeNull()
  })

  it('is the value itself when the page draws the column it matched', () => {
    const found = locate([sessions], ['the one'])!
    expect(visibleValue(found, meta[0], sessions)).toBe('the one')
  })
})

describe('describing a point', () => {
  it('names the drawer by what it identified and carries an exact filter for the link', () => {
    const out = describePoint(sessionPoint, 'sessions by peak', [sessions, projects], meta, names)
    expect(out.content.title).toBe('s2')
    expect(out.content.source).toBe('sessions by peak')
    expect(out.tableIndex).toBe(0)
    expect(out.filter).toEqual({ key: 'session_id', value: 's2' })
    expect(out.query).toBeNull()
    expect(out.content.rows?.table.rows).toEqual([sessions.rows[1]])
    expect(out.content.fields).toContainEqual(['y', '999'])
  })

  it('says plainly that nothing matches, rather than showing rows that are not the point\'s', () => {
    const out = describePoint(
      { traceName: 'resident', x: 158, y: 5, pointIndex: 3 }, null, [sessions], meta, names)
    expect(out.content.title).toBe('resident at 158')
    expect(out.content.rows).toBeNull()
    expect(out.content.noRows).toContain('No table on this tab')
    expect(out.tableIndex).toBeNull()
    expect(out.filter).toBeNull()
  })
})
