/**
 * The Sessions row keeps its click, and a control beside it opens what the chat planned and ran.
 *
 * THE FIRST CASE IS THE REGRESSION GUARD, not a formality. `DataTable` decides whether a row click
 * selects a session or opens the row from the ABSENCE of a detail meta, so attaching this panel the
 * obvious way would have silently turned every click on the Sessions list from select into open,
 * with nothing red to say so. The case fails if `row_detail` is ever folded into `reads`.
 */
import { describe, expect, it, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ChatWork, TabPayload } from '@/api'
import { Pane } from './Pane'

const SESSIONS: TabPayload = {
  tab: 'tab-sessions', session: null, scope: 'main', cohort: null,
  tables: [{
    id: 'tbl-session', columns: ['session_id', 'title'],
    rows: [{ session_id: 'sess-1', title: 'the one' }, { session_id: 'sess-2', title: 'another' }],
  }],
  figures: [], text: [], plotly: [],
  meta: [{
    id: 'tbl-session', title: 'Sessions', columns: [], filterable: true, page_size: null,
    row_detail: { url: '/api/chat', key: 'session_id', title: 'plans and background work' },
  }],
}

/** Only the fields the panel reads; the route answers every key of `ChatWork`. */
function work(over: Partial<ChatWork> = {}): ChatWork {
  return {
    session: 'sess-1', chat: ['sess-1'],
    harvested: { plans: true, agent_runs: true, workflow_runs: true, task_events: true, changes: true },
    plans: [], plans_total: 0,
    agent_runs: [], agent_runs_total: 0,
    workflow_runs: [], workflow_runs_total: 0,
    task_events: [], task_events_total: 0, task_events_unresolved: 0,
    changed_files: [], changed_files_total: 0, changes_total: 0,
    ...over,
  }
}

function answer(body: ChatWork) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => body })))
}

afterEach(() => vi.unstubAllGlobals())

describe('a row of the Sessions list', () => {
  it('still selects the session when the row itself is clicked', () => {
    const select = vi.fn()
    answer(work())
    render(<Pane payload={SESSIONS} onRowClick={select} />)
    fireEvent.click(screen.getByRole('table').querySelector('tbody tr')!)
    expect(select).toHaveBeenCalledWith({ session_id: 'sess-1', title: 'the one' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('opens the panel from the control beside it, without selecting anything', async () => {
    const select = vi.fn()
    answer(work({
      plans: [{
        tool_use_id: 'tp-1', ts: '2026-08-02T10:00:00Z', plan_chars: 15,
        plan_file_path: null, preview: 'the newest plan', outcome: 'accepted', denial_kind: null,
      }],
      plans_total: 1,
    }))
    render(<Pane payload={SESSIONS} onRowClick={select} />)
    fireEvent.click(screen.getAllByLabelText('plans and background work')[0])
    expect(select).not.toHaveBeenCalled()
    // TWICE, deliberately: once as a row of the plans table, once as the text below it.
    await waitFor(() => expect(screen.getAllByText('the newest plan')).toHaveLength(2))
    expect(fetch).toHaveBeenCalledWith('/api/chat/sess-1')
    expect(screen.getByRole('dialog').textContent).toContain('accepted')
  })

  it('calls the plan text an opening, since the route sends 400 characters of it', async () => {
    answer(work({
      plans: [{
        tool_use_id: 'tp-1', ts: '2026-08-02T10:00:00Z', plan_chars: 6737,
        plan_file_path: null, preview: 'the opening of it', outcome: 'accepted',
        denial_kind: null,
      }],
      plans_total: 1,
    }))
    render(<Pane payload={SESSIONS} onRowClick={vi.fn()} />)
    fireEvent.click(screen.getAllByLabelText('plans and background work')[0])
    // THE NUMBER IS THE POINT. Every plan in this store is longer than the preview, so a note
    // saying "the newest is shown" over 400 of 6,737 characters is a false statement.
    await waitFor(() => expect(screen.getByText(/The opening of this plan, 6,737 characters/))
      .toBeTruthy())
  })

  it('draws one table per kind of work, not one joined list', async () => {
    answer(work({
      plans: [{
        tool_use_id: 'tp-1', ts: '2026-08-02T10:00:00Z', plan_chars: 15,
        plan_file_path: null, preview: 'the newest plan', outcome: 'accepted', denial_kind: null,
      }],
      plans_total: 1,
      agent_runs: [{
        agent_id: 'a1', agent_type: 'general-purpose', name: null, description: 'did a thing',
        workflow_run_id: null, tool_use_id: 'tc-agent', called_from: 'sess-1',
        spawned_at: '2026-08-02T11:00:00Z', outcome: 'ok', records: 3, output_tokens: 300,
      }],
      agent_runs_total: 1,
    }))
    render(<Pane payload={SESSIONS} onRowClick={vi.fn()} />)
    fireEvent.click(screen.getAllByLabelText('plans and background work')[0])
    // Two of them, each with its own columns. The Sessions table itself is the third on the page.
    await waitFor(() => expect(screen.getAllByRole('table')).toHaveLength(3))
    const dialog = screen.getByRole('dialog')
    expect(dialog.textContent).toContain('Plans')
    expect(dialog.textContent).toContain('Subagent runs')
    expect(dialog.textContent).toContain('did a thing')
  })

  it('says which silence it is when a chat has none of this', async () => {
    answer(work({ harvested: { plans: false, agent_runs: false, workflow_runs: false, task_events: false } }))
    render(<Pane payload={SESSIONS} onRowClick={vi.fn()} />)
    fireEvent.click(screen.getAllByLabelText('plans and background work')[0])
    await waitFor(() =>
      expect(screen.getByRole('dialog').textContent).toContain('has not harvested'))
  })

  it('lists the files a chat changed as a fifth table, saying how much of each the counts cover', async () => {
    answer(work({
      changed_files: [{
        file: 'C:/p/app.py', edits: 3, ok_edits: 2, additions: 3, deletions: 2, patched: 1,
        by_subagents: 1, first_ts: null, last_ts: '2026-08-02T10:30:00Z', kinds: 'edit',
      }],
      changed_files_total: 1, changes_total: 3,
    }))
    render(<Pane payload={SESSIONS} onRowClick={vi.fn()} />)
    fireEvent.click(screen.getAllByLabelText('plans and background work')[0])
    const dialog = screen.getByRole('dialog')
    await waitFor(() => expect(dialog.textContent).toContain('1 of 3 edits'))
    expect(dialog.textContent).toContain('Files changed')
    expect(dialog.textContent).toContain('Changes')
  })
})
