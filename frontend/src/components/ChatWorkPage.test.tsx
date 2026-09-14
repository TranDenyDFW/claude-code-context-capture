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
    harvested: { plans: true, agent_runs: true, workflow_runs: true, task_events: true, changes: true,
                 reviews: true },
    plans: [], plans_total: 0,
    agent_runs: [], agent_runs_total: 0,
    workflow_runs: [], workflow_runs_total: 0,
    task_events: [], task_events_total: 0, task_events_unresolved: 0,
    changed_files: [], changed_files_total: 0, changes_total: 0,
    reviews: [], reviews_total: 0,
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

  it('lists each review with its verdict, its round and the prompt it followed', async () => {
    answer(body({
      reviews: [{
        session_id: 'run-1111-2222', ts: '2026-08-02T14:00:00Z', verdict: 'PROBLEMS', round: 2,
        after_prompt_ts: '2026-08-02T13:30:00Z', after_prompt: 'and now the tests',
        calls: 1, input_tokens: 9000, cache_read: 0, cache_creation: 0, output_tokens: 40,
        cost_usd: 0.03, hits: 4, snippets: 8,
      }, {
        session_id: 'run-3333-4444', ts: '2026-08-02T12:00:00Z', verdict: null, round: 1,
        after_prompt_ts: null, after_prompt: null,
        calls: 1, input_tokens: 100, cache_read: 0, cache_creation: 0, output_tokens: 5,
        cost_usd: null, hits: 2, snippets: 3,
      }],
      reviews_total: 2,
    }))
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('PROBLEMS')).toBeTruthy())
    expect(screen.getByText(/after the prompt: and now the tests/)).toBeTruthy()
    expect(screen.getByText('round 2')).toBeTruthy()
    expect(screen.getByText('$0.0300')).toBeTruthy()
    // A run with no verdict and no prompt before it says so rather than showing blanks.
    expect(screen.getByText('no verdict')).toBeTruthy()
    expect(screen.getByText(/before any prompt this store holds/)).toBeTruthy()
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

describe('the changes a chat made', () => {
  const FILE = {
    file: 'C:/p/app.py', edits: 3, ok_edits: 2, additions: 3, deletions: 2, patched: 1,
    by_subagents: 1, first_ts: '2026-08-02T10:10:00Z', last_ts: '2026-08-02T10:30:00Z', kinds: 'edit',
  }
  const EDIT = {
    tool_use_id: 'ch-1', ts: '2026-08-02T10:10:00Z', turn_uuid: 't', tool_name: 'Edit',
    file: 'C:/p/app.py', kind: 'edit', old_lines: 1, new_lines: 3, additions: 3, deletions: 2,
    has_patch: 1, is_sidechain: 0, user_modified: 0, outcome: 'ok', denial_kind: null,
  }
  const AGENT = { ...EDIT, tool_use_id: 'ch-3', kind: null, additions: null, deletions: null,
                  has_patch: 0, is_sidechain: 1 }
  const DETAIL = {
    tool_use_id: 'ch-1', session: 'sess-1', turn_uuid: 't', ts: '2026-08-02T10:10:00Z',
    tool_name: 'Edit', file: 'C:/p/app.py', kind: 'edit', old_text: 'gone',
    new_text: 'one\ntwo\nthree', replace_all: false, old_lines: 1, new_lines: 3,
    hunks: [{ oldStart: 4, oldLines: 3, newStart: 4, newLines: 4,
              lines: [' keep', '-gone', '-also gone', '+one', '+two', '+three'] }],
    additions: 3, deletions: 2, original_chars: 120, user_modified: false, is_sidechain: false,
    outcome: 'ok', denial_kind: null,
  }
  const routes = (list: ChatWork, edits: unknown[], detail: unknown) =>
    vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
      ok: true,
      json: async () => (url.includes('/changes?') ? { changes: edits }
        : url.startsWith('/api/change/') ? detail : list),
    })))
  const chat = body({ changed_files: [FILE], changed_files_total: 1, changes_total: 3 })

  it('says how much of a file the counts cover, and fetches its edits on request', async () => {
    routes(chat, [AGENT, EDIT], DETAIL)
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    // THE QUALIFIER IS THE POINT: +3 -2 covers one edit of three, and the row says so.
    await waitFor(() => expect(screen.getByText(/over 1 of 3 edits/)).toBeTruthy())
    fireEvent.click(screen.getByText('Show the edits'))
    await waitFor(() => expect(screen.getByText(/1 lines to 3, no patch recorded/)).toBeTruthy())
    expect(fetch).toHaveBeenCalledWith('/api/chat/sess-1/changes?file=C%3A%2Fp%2Fapp.py')
    expect(screen.getByText('by a subagent')).toBeTruthy()
  })

  it('draws a recorded patch line by line, added and removed told apart', async () => {
    routes(chat, [EDIT], DETAIL)
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Show the edits')).toBeTruthy())
    fireEvent.click(screen.getByText('Show the edits'))
    await waitFor(() => expect(screen.getByText('Show the diff')).toBeTruthy())
    fireEvent.click(screen.getByText('Show the diff'))
    await waitFor(() => expect(screen.getByText('+one')).toBeTruthy())
    const added = document.querySelectorAll('[data-line="added"]')
    const removed = document.querySelectorAll('[data-line="removed"]')
    const context = document.querySelectorAll('[data-line="context"]')
    expect([added.length, removed.length, context.length]).toEqual([3, 2, 1])
    expect(added[0].className).toContain('text-good')
    expect(removed[0].className).toContain('text-warn')
    expect(screen.getByText('@@ -4,3 +4,4 @@')).toBeTruthy()
  })

  it('shows before and after, and says why that is all, for an edit with no recorded result', async () => {
    const agentDetail = { ...DETAIL, tool_use_id: 'ch-3', hunks: null, old_text: 'two',
                          new_text: 'deux', additions: null, deletions: null, is_sidechain: true }
    routes(chat, [AGENT], agentDetail)
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Show the edits')).toBeTruthy())
    fireEvent.click(screen.getByText('Show the edits'))
    await waitFor(() => expect(screen.getByText('Show the diff')).toBeTruthy())
    fireEvent.click(screen.getByText('Show the diff'))
    await waitFor(() => expect(screen.getByText(/No result was recorded for this edit/)).toBeTruthy())
    expect(screen.getByText('-two')).toBeTruthy()
    expect(screen.getByText('+deux')).toBeTruthy()
    expect(screen.queryByText(/@@/)).toBeNull()
  })

  it('draws a created file as all new, which is the third state', async () => {
    const created = { ...DETAIL, tool_use_id: 'ch-2', kind: 'create', hunks: [], old_text: null,
                      new_text: 'a\nb', additions: 0, deletions: 0 }
    routes(chat, [{ ...EDIT, tool_use_id: 'ch-2', kind: 'create', additions: 0, deletions: 0 }], created)
    render(<ChatWorkPage session="sess-1" onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Show the edits')).toBeTruthy())
    fireEvent.click(screen.getByText('Show the edits'))
    await waitFor(() => expect(screen.getByText('Show the diff')).toBeTruthy())
    fireEvent.click(screen.getByText('Show the diff'))
    await waitFor(() => expect(screen.getByText(/A created file/)).toBeTruthy())
    expect(document.querySelectorAll('[data-line="added"]')).toHaveLength(2)
    expect(document.querySelectorAll('[data-line="removed"]')).toHaveLength(0)
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
      reviews: [{
        session_id: 'run-1', ts: '2026-08-02T14:00:00Z', verdict: 'APPROVED', round: 1,
        after_prompt_ts: '2026-08-02T13:30:00Z', after_prompt: 'and now the tests',
        calls: 1, input_tokens: 9000, cache_read: 0, cache_creation: 0, output_tokens: 40,
        cost_usd: 0.03, hits: 4, snippets: 8,
      }],
      reviews_total: 1,
    }))
    const lines = text.split('\n')
    expect(lines[0]).toBe('kind,id,when,what,status,detail')
    expect(lines).toHaveLength(6)
    expect(lines.map((l) => l.split(',')[0])).toEqual(
      ['kind', '"plan"', '"agent_run"', '"workflow_run"', '"task_event"', '"review"'])
    expect(text).toContain('1m 30s')
    expect(text).toContain('"and now the tests","APPROVED","round 1 9040 tokens 0.03"')
  })

  it('doubles a quote in a value rather than ending the field early', () => {
    const text = csv(body({ plans: [{ ...PLAN, preview: 'a plan he called "done"' }] }))
    expect(text).toContain('"a plan he called ""done"""')
  })
})
