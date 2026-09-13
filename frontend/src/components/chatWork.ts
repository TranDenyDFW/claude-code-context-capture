import type { ChatWork, Table, TableMeta } from '@/api'
import type { InspectorContent } from './Inspector'

/**
 * What `/api/chat/<session>` answers, turned into what the drawer draws.
 *
 * Its own file, and a pure function, because it is the one piece of this panel worth testing on
 * its own: the counts, the four lists and the sentence shown when there are none are decisions
 * about what a reader is told, not plumbing.
 *
 * FOUR LISTS, NOT ONE JOINED TABLE. They answer different questions and carry different columns,
 * and a single table would have to drop most of both. A subagent run and a workflow run are not
 * rows of one kind.
 */

/** A table the drawer can draw, built from records the server already shaped. */
function table(id: string, rows: Record<string, unknown>[], columns: string[]): Table {
  return { id, columns, rows }
}

function meta(id: string, title: string, note: string | null = null): TableMeta {
  return {
    id, title, note,
    columns: [], tooltips: {}, note_level: null, alert: null,
  } as unknown as TableMeta
}

/** `927840` becomes `15m 28s`, because a duration in milliseconds is not a readable number. */
export function readableMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return ''
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`
}

export function chatWorkContent(body: ChatWork): Partial<InspectorContent> {
  const groups: { name: string; table: Table; meta?: TableMeta }[] = []

  if (body.plans.length) {
    groups.push({
      name: 'Plans',
      table: table('chat-plans', body.plans.map((p) => ({
        // The verdict comes from the CALL. The same text is a proposal either way, and a panel
        // that showed the plan without saying whether it was accepted would be showing half of it.
        when: p.ts, outcome: p.outcome ?? 'not recorded', chars: p.plan_chars,
        plan: p.preview, file: p.plan_file_path,
      })), ['when', 'outcome', 'chars', 'plan', 'file']),
      meta: meta('chat-plans', 'Plans'),
    })
  }
  if (body.agent_runs.length) {
    groups.push({
      name: 'Subagent runs',
      table: table('chat-agent-runs', body.agent_runs.map((r) => ({
        when: r.spawned_at, type: r.agent_type, description: r.description ?? r.name,
        workflow: r.workflow_run_id, records: r.records, output_tokens: r.output_tokens,
        // Two states a reader must be able to tell apart: a run this chat asked for, and one whose
        // files merely sit under its directory because history was bridged between sessions.
        reached: r.called_from ? 'called here' : 'in this directory',
      })), ['when', 'type', 'description', 'workflow', 'records', 'output_tokens', 'reached']),
      meta: meta('chat-agent-runs', 'Subagent runs'),
    })
  }
  if (body.workflow_runs.length) {
    groups.push({
      name: 'Workflow runs',
      table: table('chat-workflow-runs', body.workflow_runs.map((w) => ({
        when: w.started_at, workflow: w.workflow_name, status: w.status,
        took: readableMs(w.duration_ms),
        // TWO NUMBERS. What the run reported, and how many of those agents this store holds.
        agents: w.agent_count, agents_here: w.agents_on_disk,
        tokens: w.total_tokens, tool_calls: w.total_tool_calls, summary: w.summary,
      })), ['when', 'workflow', 'status', 'took', 'agents', 'agents_here', 'tokens', 'tool_calls',
            'summary']),
      meta: meta('chat-workflow-runs', 'Workflow runs'),
    })
  }
  if (body.task_events.length) {
    groups.push({
      name: 'Task notifications',
      table: table('chat-task-events', body.task_events.map((t) => ({
        when: t.ts, status: t.status, description: t.description,
        resolves_to: t.resolved_to ?? 'nothing this store holds',
        ran_under: t.ran_under && t.ran_under !== body.session ? t.ran_under : '',
      })), ['when', 'status', 'description', 'resolves_to', 'ran_under']),
      meta: meta('chat-task-events', 'Task notifications'),
    })
  }

  const fields: [string, string][] = [
    ['Plans', String(body.plans_total)],
    ['Subagent runs', String(body.agent_runs_total)],
    ['Workflow runs', String(body.workflow_runs_total)],
    ['Task notifications', String(body.task_events_total)],
  ]
  if (body.chat.length > 1) {
    fields.push(['CLI sessions in this chat', String(body.chat.length)])
  }
  if (body.task_events_unresolved) {
    fields.push(['Tasks that resolve to nothing here', String(body.task_events_unresolved)])
  }

  // TWO DIFFERENT SILENCES. A chat that ran nothing and a store that never harvested any of this
  // look identical from the lists alone, and only one of them is about the chat.
  const missing = Object.entries(body.harvested).filter(([, yes]) => !yes).map(([name]) => name)
  const noRows = groups.length
    ? null
    : missing.length
      ? `This store has not harvested ${missing.join(', ')} yet. Run node tools/harvest.mjs, `
        + 'or node tools/harvest.mjs --backfill-sidecars for work that is already on disk.'
      : 'This chat wrote no plan and ran no background work.'

  const newest = body.plans[0]
  return {
    fields,
    rows: groups.length ? groups : null,
    noRows,
    // THE OPENING OF THE NEWEST PLAN, and the note says so in those words. The route sends 400
    // characters per row, and every one of the 280 plans on this machine is longer than that: the
    // shortest is 1,493 characters and the longest 80,428. An earlier note read "the newest is
    // shown", which described half a paragraph as a document.
    text: newest?.preview ?? undefined,
    textNote: newest
      ? `The opening of ${body.plans_total > 1 ? `the newest of ${body.plans_total} plans` : 'this plan'}`
        + `, ${(newest.plan_chars ?? 0).toLocaleString()} characters in all.`
        + ' Open this chat in a window for the whole of each.'
      : null,
  }
}
