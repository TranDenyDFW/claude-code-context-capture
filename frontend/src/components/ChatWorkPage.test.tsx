/**
 * One chat's work in a window: what it fetches, what it says when a list is capped, and the export.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ChatWork } from '@/api'
import { ChatWorkPage, csv } from './ChatWorkPage'

function body(over: Partial<ChatWork> = {}): ChatWork {
  return {
    session: 'sess-1', chat: ['sess-1', 'sess-0'],
    harvested: { plans: true, agent_runs: true, workflow_runs: true, task_events: true },
    plans: [], plans_total: 0,
    agent_runs: [], agent_runs_total: 0,
    workflow_runs: [], workflow_runs_total: 0,
    task_events: [], task_events_total: 0, task_events_unresolved: 0,
    ...over,
  }
}

const PLAN = {
  tool_use_id: 'tp-1', ts: '2026-08-02T10:00:00Z', plan_chars: 15, plan_file_path: null,
  preview: 'the newest plan', outcome: 'accepted', denial_kind: null,
}

function answer(value: ChatWork) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => value })))
}

afterEach(() => vi.unstubAllGlobals())

describe('the chat work page', () => {
  it('asks the route for the whole chat, up to the ceiling the route enforces', async () => {
    answer(body())
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/chat/sess-1?limit=2000'))
  })

  it('says how many CLI sessions the counts are taken over', async () => {
    answer(body({ plans: [PLAN], plans_total: 1 }))
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() =>
      expect(screen.getByText(/2 CLI sessions are folded into this chat/)).toBeTruthy())
  })

  it('names the cap when a list holds more than it was given (gate can fail)', async () => {
    // THE DIFFERENCE IS THE POINT: 1 fetched of 900 must not read as a chat with one plan.
    answer(body({ plans: [PLAN], plans_total: 900 }))
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('1 of 900, the newest')).toBeTruthy())
    expect(screen.queryByText('900')).toBeNull()
  })

  it('tells an unharvested store apart from a chat that ran nothing', async () => {
    answer(body({ harvested: { plans: false, agent_runs: false, workflow_runs: false, task_events: false } }))
    const { unmount } = render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/has not harvested/)).toBeTruthy())
    unmount()
    answer(body())
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() =>
      expect(screen.getByText('This chat wrote no plan and ran no background work.')).toBeTruthy())
  })

  it('says the fetch failed rather than drawing an empty chat', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404 })))
    render(<ChatWorkPage session="nope" onBack={() => {}} />)
    await waitFor(() => expect(screen.getByRole('alert').textContent)
      .toContain('could not be fetched'))
  })
})

describe('a plan is a document, not its opening', () => {
  it('shows the opening until asked, then the whole text', async () => {
    const list = body({ plans: [PLAN], plans_total: 1 })
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      calls.push(url)
      return {
        ok: true,
        json: async () => (url.startsWith('/api/plan/')
          ? { text: 'the whole plan, every paragraph of it', chars: 6737,
              plan_file_path: 'C:/p/one.md', file_exists: false }
          : list),
      }
    }))
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('the newest plan')).toBeTruthy())
    // THE LIST REQUEST DOES NOT CARRY THE TEXT, which is why the button exists.
    expect(calls).toEqual(['/api/chat/sess-1?limit=2000'])
    fireEvent.click(screen.getByText('Read the whole plan'))
    await waitFor(() =>
      expect(screen.getByText('the whole plan, every paragraph of it')).toBeTruthy())
    expect(calls[1]).toBe('/api/plan/tp-1')
    expect(screen.queryByText('Read the whole plan')).toBeNull()
  })

  it('says whether the file it names is still there, once it knows', async () => {
    const list = body({ plans: [{ ...PLAN, plan_file_path: 'C:/p/one.md' }], plans_total: 1 })
    vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
      ok: true,
      json: async () => (url.startsWith('/api/plan/')
        ? { text: 'x', chars: 1, plan_file_path: 'C:/p/one.md', file_exists: false }
        : list),
    })))
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Read the whole plan')).toBeTruthy())
    // NOT CLAIMED BEFORE IT IS KNOWN: the list carries the path and nothing about the file.
    expect(screen.queryByText(/no longer on disk/)).toBeNull()
    fireEvent.click(screen.getByText('Read the whole plan'))
    await waitFor(() => expect(screen.getByText(/no longer on disk/)).toBeTruthy())
  })

  it('keeps the opening and says so when the fetch fails', async () => {
    const list = body({ plans: [PLAN], plans_total: 1 })
    vi.stubGlobal('fetch', vi.fn(async (url: string) => (url.startsWith('/api/plan/')
      ? { ok: false, status: 404 }
      : { ok: true, json: async () => list })))
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Read the whole plan')).toBeTruthy())
    fireEvent.click(screen.getByText('Read the whole plan'))
    await waitFor(() => expect(screen.getByRole('alert').textContent)
      .toContain('the opening is shown'))
    expect(screen.getByText('the newest plan')).toBeTruthy()
  })
})

describe('the export', () => {
  it('carries all four kinds in one file, each row saying which kind it is', () => {
    const text = csv(body({
      plans: [PLAN], plans_total: 1,
      agent_runs: [{
        agent_id: 'a1', agent_type: 'Explore', name: null, description: 'read the tree',
        workflow_run_id: null, tool_use_id: null, called_from: null,
        spawned_at: '2026-08-02T11:00:00Z', outcome: null, records: 3, output_tokens: 300,
      }],
      agent_runs_total: 1,
      workflow_runs: [{
        run_id: 'wf_1', task_id: 'w1', workflow_name: 'review-changes', status: 'completed',
        started_at: '2026-08-02T12:00:00Z', duration_ms: 90000, agent_count: 4, agents_on_disk: 1,
        total_tokens: 5000, total_tool_calls: 20, summary: 'four findings',
      }],
      workflow_runs_total: 1,
      task_events: [{
        uuid: 'te-1', ts: '2026-08-02T13:00:00Z', task_id: 'a1', task_type: 'local_agent',
        status: 'completed', description: 'the run', resolved_to: 'agent', ran_under: 'sess-1',
      }],
      task_events_total: 1,
    }))
    const lines = text.split('\n')
    expect(lines[0]).toBe('kind,id,when,what,status,detail')
    expect(lines).toHaveLength(5)
    expect(lines.map((l) => l.split(',')[0])).toEqual(
      ['kind', '"plan"', '"agent_run"', '"workflow_run"', '"task_event"'])
    expect(text).toContain('1m 30s')
  })

  it('doubles a quote in a value rather than ending the field early', () => {
    const text = csv(body({ plans: [{ ...PLAN, preview: 'a plan he called "done"' }] }))
    expect(text).toContain('"a plan he called ""done"""')
  })
})
