import { useEffect, useState } from 'react'
import type { ChangeDetail, ChatChange, ChatWork, DiffHunk } from '@/api'
import { readableMs } from './chatWork'
import { HOW_TO_HARVEST } from './harvest'
import { TextBody } from './Markdown'

/** One plan as `/api/plan/<call>` answers it: the document, not its opening. */
interface PlanText {
  text: string
  chars: number | null
  plan_file_path: string | null
  file_exists: boolean
}

/**
 * One chat's plans and background work, in a window of its own.
 *
 * The drawer beside the Sessions list answers "is there anything here", with the newest plan and
 * the first rows of each kind. This answers "what happened", uncapped to the route's own ceiling,
 * with every plan's text rather than its first line and an export of all four lists.
 *
 * FOUR SECTIONS, NOT ONE TABLE, for the reason the drawer gives: a plan, a subagent run, a workflow
 * run and a task notification carry different columns and answer different questions, and one
 * joined table would have to drop most of each.
 */

/** The ceiling the route itself enforces, named here so the page can say when it was reached. */
const LIMIT = 2000

/** Every list flattened to one file. The `kind` column is what keeps four shapes readable as one. */
export function csv(body: ChatWork): string {
  const cell = (v: unknown) => '"' + String(v ?? '').replace(/"/g, '""') + '"'
  const head = ['kind', 'id', 'when', 'what', 'status', 'detail']
  const rows: unknown[][] = [
    ...body.plans.map((p) => ['plan', p.tool_use_id, p.ts, p.preview, p.outcome,
      `${p.plan_chars ?? ''} chars${p.plan_file_path ? ` ${p.plan_file_path}` : ''}`]),
    ...body.agent_runs.map((r) => ['agent_run', r.agent_id, r.spawned_at,
      r.description ?? r.name, r.agent_type,
      `${r.records ?? ''} records ${r.output_tokens ?? ''} output tokens`]),
    ...body.workflow_runs.map((w) => ['workflow_run', w.run_id, w.started_at, w.workflow_name,
      w.status, `${w.agent_count ?? ''} agents ${readableMs(w.duration_ms)} ${w.summary ?? ''}`]),
    ...body.task_events.map((t) => ['task_event', t.task_id, t.ts, t.description, t.status,
      t.resolved_to ?? 'unresolved']),
    ...body.reviews.map((r) => ['review', r.session_id, r.ts, r.after_prompt ?? '',
      r.verdict ?? 'no verdict',
      `round ${r.round} ${(r.input_tokens ?? 0) + (r.cache_read ?? 0) + (r.cache_creation ?? 0)
        + (r.output_tokens ?? 0)} tokens ${r.cost_usd ?? ''}`]),
    ...(body.runs ?? []).map((r) => ['run', r.session_id, r.ts, r.prompt ?? '', `tied by ${r.how}`,
      `${r.leaf} ${(r.input_tokens ?? 0) + (r.cache_read ?? 0) + (r.cache_creation ?? 0)
        + (r.output_tokens ?? 0)} tokens ${r.cost_usd ?? ''}`]),
    ...body.changed_files.map((f) => ['changed_file', f.file, f.last_ts, `${f.edits} edits`, f.kinds,
      f.additions === null ? 'no patch recorded'
        : `+${f.additions} -${f.deletions} over ${f.patched ?? 0} of ${f.edits} edits`]),
  ]
  return [head.join(','), ...rows.map((r) => r.map(cell).join(','))].join('\n')
}

const LINE_CLASS: Record<string, string> = {
  added: 'text-good', removed: 'text-warn', context: 'text-ink-faint',
}

/**
 * A recorded patch, drawn line by line. `+` reads as added, `-` as removed, the rest as context,
 * and each hunk announces where in the file it sits, since a reader holding the file has nothing
 * else to place it by.
 */
function Diff({ hunks }: { hunks: DiffHunk[] }) {
  return (
    <div className="mt-1 overflow-auto rounded bg-page font-mono text-2xs leading-relaxed">
      {hunks.map((h, i) => (
        <div key={i} data-hunk={i}>
          <div className="px-2 py-0.5 text-ink-faint">
            @@ -{h.oldStart},{h.oldLines} +{h.newStart},{h.newLines} @@
          </div>
          {h.lines.map((line, j) => {
            const kind = line[0] === '+' ? 'added' : line[0] === '-' ? 'removed' : 'context'
            return (
              <div key={j} data-line={kind} className={`whitespace-pre px-2 ${LINE_CLASS[kind]}`}>
                {line}
              </div>
            )
          })}
        </div>
      ))}
    </div>
  )
}

/**
 * Before and after, for a change with no recorded patch. No context lines exist for it and none
 * are invented: the old text is drawn as removed, the new as added, under a line saying why that
 * is all there is.
 */
function BeforeAfter({ oldText, newText, why }: {
  oldText: string | null
  newText: string | null
  why: string
}) {
  const rows = (text: string | null, prefix: string, kind: 'removed' | 'added') =>
    (text === null || text === '' ? [] : text.split('\n')).map((l, i) => (
      <div key={`${kind}-${i}`} data-line={kind} className={`whitespace-pre px-2 ${LINE_CLASS[kind]}`}>
        {prefix}{l}
      </div>
    ))
  return (
    <div className="mt-1 overflow-auto rounded bg-page font-mono text-2xs leading-relaxed">
      <div className="px-2 py-0.5 text-ink-faint">{why}</div>
      {rows(oldText, '-', 'removed')}
      {rows(newText, '+', 'added')}
    </div>
  )
}

function Row({ children }: { children: React.ReactNode }) {
  return (
    <div className="border-b border-edge/40 px-3 py-2 last:border-0">{children}</div>
  )
}

function Meta({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-wrap items-baseline gap-2 text-2xs text-ink-faint">{children}</div>
}

function Section({
  title, count, shown, children,
}: {
  title: string
  count: number
  shown: number
  children: React.ReactNode
}) {
  return (
    <section className="rounded-lg bg-panel p-4 shadow-panel">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-ink-dim">{title}</h2>
        <span className="text-2xs tabular-nums text-ink-faint">
          {/* TWO NUMBERS WHEN THEY DIFFER. The route caps each list, and a page that printed only
              what it drew would report a chat as smaller than it is. */}
          {shown < count
            ? `${shown.toLocaleString()} of ${count.toLocaleString()}, the newest`
            : `${count.toLocaleString()}`}
        </span>
      </div>
      {shown === 0
        ? <p className="text-2xs text-ink-faint">None in this chat.</p>
        : <div className="max-h-[45vh] overflow-auto rounded border border-edge">{children}</div>}
    </section>
  )
}

export function ChatWorkPage({ session, onBack }: { session: string; onBack: () => void }) {
  const [body, setBody] = useState<ChatWork | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  /** Per plan: the whole document, once asked for. `/api/chat` sends only its first 400 characters. */
  const [whole, setWhole] = useState<Record<string, PlanText | 'fetching' | 'failed'>>({})

  // FETCHED PER PLAN, NOT WITH THE LIST. The shortest plan on this machine is 1,493 characters and
  // the longest 80,428, so sending every one of them with the four lists would make the page's own
  // request the largest thing it does, for text most readers will not open.
  const readPlan = (id: string) => {
    setWhole((was) => ({ ...was, [id]: 'fetching' }))
    fetch(`/api/plan/${encodeURIComponent(id)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((got: PlanText) => setWhole((was) => ({ ...was, [id]: got })))
      .catch(() => setWhole((was) => ({ ...was, [id]: 'failed' })))
  }

  /** Per file, its edits once asked for: the busiest chat here has 11,367 and the list is per file. */
  const [edits, setEdits] = useState<Record<string, ChatChange[] | 'fetching' | 'failed'>>({})
  const readEdits = (file: string) => {
    setEdits((was) => ({ ...was, [file]: 'fetching' }))
    fetch(`/api/chat/${encodeURIComponent(session)}/changes?file=${encodeURIComponent(file)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((got: { changes: ChatChange[] }) => setEdits((was) => ({ ...was, [file]: got.changes })))
      .catch(() => setEdits((was) => ({ ...was, [file]: 'failed' })))
  }
  /** Per edit, the patch, or the before and after when no result recorded one. */
  const [diffs, setDiffs] = useState<Record<string, ChangeDetail | 'fetching' | 'failed'>>({})
  const readChange = (id: string) => {
    setDiffs((was) => ({ ...was, [id]: 'fetching' }))
    fetch(`/api/change/${encodeURIComponent(id)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((got: ChangeDetail) => setDiffs((was) => ({ ...was, [id]: got })))
      .catch(() => setDiffs((was) => ({ ...was, [id]: 'failed' })))
  }

  useEffect(() => {
    let live = true
    setBody(null)
    setProblem(null)
    fetch(`/api/chat/${encodeURIComponent(session)}?limit=${LIMIT}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((json) => { if (live) setBody(json) })
      .catch(() => { if (live) setProblem('This chat could not be fetched.') })
    return () => { live = false }
  }, [session])

  useEffect(() => {
    const before = document.title
    document.title = `Work in chat ${session.slice(0, 8)} · C4X`
    return () => { document.title = before }
  }, [session])

  const save = () => {
    if (!body) return
    const a = document.createElement('a')
    a.href = `data:text/csv;charset=utf-8,${encodeURIComponent(csv(body))}`
    a.download = `chat-work-${session.slice(0, 8)}.csv`
    a.click()
  }

  const missing = body
    ? Object.entries(body.harvested).filter(([, yes]) => !yes).map(([name]) => name)
    : []
  const nothing = body
    && !body.plans_total && !body.agent_runs_total && !body.workflow_runs_total
    && !body.task_events_total

  return (
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-4 px-6 py-5">
      <div className="flex items-baseline justify-between gap-4">
        <h1 className="text-md font-semibold text-ink-dim">
          Work in chat {session.slice(0, 8)}
        </h1>
        <div className="flex shrink-0 items-center gap-3">
          {body && !nothing && (
            <button
              onClick={save}
              className="rounded border border-edge px-2 py-0.5 text-2xs text-ink-dim hover:text-ink"
            >
              Export CSV
            </button>
          )}
          <button onClick={onBack} className="text-sm text-accent hover:underline">
            Back to the dashboard
          </button>
        </div>
      </div>

      {problem && <p role="alert" className="text-sm text-ink-dim">{problem}</p>}
      {!body && !problem && <p className="text-sm text-ink-faint">Fetching this chat</p>}

      {body && (
        <>
          <p className="text-2xs text-ink-faint">
            {/* THE CHAT, NOT THE SESSION. A resumed chat is several CLI sessions and work written
                before the last resume belongs to it; saying so here is what makes the counts
                legible when they exceed what the selected session alone could hold. */}
            {body.chat.length > 1
              ? `${body.chat.length} CLI sessions are folded into this chat, and everything below is counted over all of them.`
              : 'One CLI session, never resumed.'}
            {missing.length > 0 && (
              <> This store has not harvested {missing.join(', ')} yet, so those sections are empty
                for a reason that is not about this chat. {HOW_TO_HARVEST} Reload this window
                afterwards.</>
            )}
            {body.task_events_unresolved > 0 && (
              <> {body.task_events_unresolved.toLocaleString()} task notifications name a run this
                store does not hold.</>
            )}
          </p>

          {nothing && !missing.length && (
            <p className="text-sm text-ink-faint">
              This chat wrote no plan and ran no background work.
            </p>
          )}

          <Section title="Plans" count={body.plans_total} shown={body.plans.length}>
            {body.plans.map((p) => {
              const got = whole[p.tool_use_id]
              const full = got && got !== 'fetching' && got !== 'failed' ? got : null
              return (
                <Row key={p.tool_use_id}>
                  <Meta>
                    <span className={p.outcome === 'refused' ? 'font-semibold text-warn' : 'text-good'}>
                      {p.outcome ?? 'outcome not recorded'}
                    </span>
                    {p.denial_kind && <span>{p.denial_kind}</span>}
                    <span className="tabular-nums">
                      {(p.plan_chars ?? 0).toLocaleString()} characters
                    </span>
                    {p.plan_file_path && (
                      <span className="font-mono">
                        {p.plan_file_path}
                        {/* A PATH IS NOT A FILE. 291 plans were written on this machine against 27
                            surviving files, so the page says which it is rather than offering a
                            path that usually leads nowhere. Known only once the plan is fetched. */}
                        {full && (full.file_exists ? ' (on disk)' : ' (no longer on disk)')}
                      </span>
                    )}
                    <span className="ml-auto tabular-nums">{String(p.ts ?? '').slice(0, 19)}</span>
                  </Meta>
                  {/* THE OPENING UNTIL ASKED. `/api/chat` sends 400 characters per plan and every
                      plan here is longer than that, so the button is the difference between a
                      paragraph and the document. */}
                  {/* A PLAN IS MARKDOWN, headings and all, because ExitPlanMode wrote it that
                      way. The opening is markdown too: a prefix of a document still reads better
                      as one, and a fence cut in half by the 400-character cap closes at the end
                      rather than swallowing the rest. The file exports appear only once the whole
                      plan is in hand, so nobody saves a paragraph under the document's name. */}
                  <div className="mt-1">
                    <TextBody
                      source={(full ? full.text : p.preview) ?? ''}
                      name={`plan-${String(p.tool_use_id).slice(0, 8)}`}
                      boxClass="max-h-[24vh]"
                      canExport={Boolean(full)}
                      actions={
                        !full && got !== 'failed' ? (
                          <button
                            onClick={() => readPlan(p.tool_use_id)}
                            disabled={got === 'fetching'}
                            className="rounded border border-edge px-2 py-0.5 text-2xs text-ink-dim
                                       hover:text-ink disabled:text-ink-faint"
                          >
                            {got === 'fetching' ? 'Fetching the whole plan' : 'Read the whole plan'}
                          </button>
                        ) : null
                      }
                    />
                  </div>
                  {got === 'failed' && (
                    <p role="alert" className="mt-1 text-2xs text-warn">
                      This plan could not be fetched; the opening is shown.
                    </p>
                  )}
                </Row>
              )
            })}
          </Section>

          <Section title="Subagent runs" count={body.agent_runs_total} shown={body.agent_runs.length}>
            {body.agent_runs.map((r) => (
              <Row key={r.agent_id}>
                <Meta>
                  <span className="font-semibold text-ink-dim">{r.agent_type ?? 'agent'}</span>
                  {r.workflow_run_id && <span className="font-mono">{r.workflow_run_id}</span>}
                  <span className="tabular-nums">{(r.records ?? 0).toLocaleString()} records</span>
                  <span className="tabular-nums">
                    {(r.output_tokens ?? 0).toLocaleString()} output tokens
                  </span>
                  {/* WHICH STATEMENT THIS IS. A run the chat called is a fact about the chat; a run
                      whose files merely sit under its directory is a fact about the disk. */}
                  <span>{r.called_from ? 'called here' : 'in this directory'}</span>
                  <span className="ml-auto tabular-nums">{String(r.spawned_at ?? '').slice(0, 19)}</span>
                </Meta>
                <p className="mt-0.5 text-2xs text-ink-dim">{r.description ?? r.name ?? r.agent_id}</p>
              </Row>
            ))}
          </Section>

          <Section
            title="Workflow runs" count={body.workflow_runs_total} shown={body.workflow_runs.length}
          >
            {body.workflow_runs.map((w) => (
              <Row key={w.run_id}>
                <Meta>
                  <span className="font-semibold text-ink-dim">{w.workflow_name ?? w.run_id}</span>
                  <span>{w.status ?? 'status not recorded'}</span>
                  <span className="tabular-nums">{readableMs(w.duration_ms)}</span>
                  {/* TWO NUMBERS, ALWAYS. What the run reported, and how many of those agents this
                      store actually holds a transcript for. */}
                  <span className="tabular-nums">
                    {(w.agent_count ?? 0).toLocaleString()} agents,{' '}
                    {(w.agents_on_disk ?? 0).toLocaleString()} here
                  </span>
                  <span className="tabular-nums">
                    {(w.total_tokens ?? 0).toLocaleString()} tokens
                  </span>
                  <span className="tabular-nums">
                    {(w.total_tool_calls ?? 0).toLocaleString()} tool calls
                  </span>
                  <span className="ml-auto tabular-nums">{String(w.started_at ?? '').slice(0, 19)}</span>
                </Meta>
                {w.summary && <p className="mt-0.5 text-2xs text-ink-dim">{w.summary}</p>}
              </Row>
            ))}
          </Section>

          <Section
            title="Task notifications" count={body.task_events_total} shown={body.task_events.length}
          >
            {body.task_events.map((t) => (
              <Row key={t.uuid}>
                <Meta>
                  <span className="font-semibold text-ink-dim">{t.status ?? 'status not recorded'}</span>
                  <span className="font-mono">{t.task_id}</span>
                  <span>{t.resolved_to ?? 'resolves to nothing this store holds'}</span>
                  {t.ran_under && t.ran_under !== body.session && (
                    <span className="font-mono">ran under {t.ran_under.slice(0, 8)}</span>
                  )}
                  <span className="ml-auto tabular-nums">{String(t.ts ?? '').slice(0, 19)}</span>
                </Meta>
                <p className="mt-0.5 text-2xs text-ink-dim">{t.description}</p>
              </Row>
            ))}
          </Section>

          {/* THE REVIEWS THIS CHAT RECEIVED: a hook's headless reviewer read it after a turn and
              left a one-prompt session of its own, tied to this chat by harvest and listed
              nowhere else. When, what it decided, which prompt the chat was answering, its cost. */}
          <Section title="Reviews" count={body.reviews_total ?? 0} shown={body.reviews.length}>
            {body.reviews.map((r) => (
              <Row key={r.session_id}>
                <Meta>
                  <span className={r.verdict === 'PROBLEMS' ? 'font-semibold text-warn'
                    : r.verdict === 'APPROVED' ? 'font-semibold text-good' : 'text-ink-dim'}>
                    {r.verdict ?? 'no verdict'}
                  </span>
                  <span>round {r.round}</span>
                  <span className="tabular-nums">
                    {((r.input_tokens ?? 0) + (r.cache_read ?? 0) + (r.cache_creation ?? 0)
                      + (r.output_tokens ?? 0)).toLocaleString()} tokens
                  </span>
                  {r.cost_usd !== null && <span className="tabular-nums">${r.cost_usd.toFixed(4)}</span>}
                  <span className="font-mono">{r.session_id.slice(0, 8)}</span>
                  <span className="ml-auto tabular-nums">{String(r.ts ?? '').slice(0, 19)}</span>
                </Meta>
                <p className="mt-0.5 text-2xs text-ink-dim">
                  {r.after_prompt
                    ? `after the prompt: ${r.after_prompt}`
                    : 'before any prompt this store holds for the chat'}
                </p>
              </Row>
            ))}
          </Section>

          {/* THE CHILD RUNS THIS CHAT SPAWNED: one-shot claude -p sessions its shell commands
              started (a harness, a script), tied to it by harvest and listed nowhere on their
              own. How the tie was made, the folder each worked in, its one prompt, its cost.
              Absent from a server that does not derive them. */}
          {body.runs ? (
            <Section title="Runs" count={body.runs_total ?? 0} shown={body.runs.length}>
              {body.runs.map((r) => (
                <Row key={r.session_id}>
                  <Meta>
                    <span className="font-mono font-semibold text-ink-dim">
                      {r.leaf || '(no folder)'}
                    </span>
                    <span>tied by {r.how}</span>
                    <span className="tabular-nums">
                      {((r.input_tokens ?? 0) + (r.cache_read ?? 0) + (r.cache_creation ?? 0)
                        + (r.output_tokens ?? 0)).toLocaleString()} tokens
                    </span>
                    {r.cost_usd !== null && <span className="tabular-nums">${r.cost_usd.toFixed(4)}</span>}
                    <span className="font-mono">{r.session_id.slice(0, 8)}</span>
                    <span className="ml-auto tabular-nums">{String(r.ts ?? '').slice(0, 19)}</span>
                  </Meta>
                  <p className="mt-0.5 text-2xs text-ink-dim">
                    {r.prompt ? r.prompt : 'no prompt recorded'}
                  </p>
                </Row>
              ))}
            </Section>
          ) : null}

          <Section title="Changes" count={body.changed_files_total} shown={body.changed_files.length}>
            {body.changed_files.map((f) => {
              const got = edits[f.file]
              const list = Array.isArray(got) ? got : null
              return (
                <Row key={f.file}>
                  <Meta>
                    <span className="font-mono font-semibold text-ink-dim">{f.file}</span>
                    <span className="tabular-nums">
                      {f.edits.toLocaleString()} {f.edits === 1 ? 'edit' : 'edits'}
                    </span>
                    {f.additions !== null && f.deletions !== null ? (
                      <span className="tabular-nums">
                        <span className="text-good">+{f.additions.toLocaleString()}</span>{' '}
                        <span className="text-warn">-{f.deletions.toLocaleString()}</span>
                        {/* HOW MUCH OF THE FILE'S HISTORY THE COUNTS COVER. A subagent edit records
                            no patch, so a file it touched six times and Claude touched once sums one
                            edit, and the sums alone would present that edit as the whole history. */}
                        {f.patched !== f.edits && ` over ${f.patched ?? 0} of ${f.edits} edits`}
                      </span>
                    ) : (
                      <span>no patch recorded for any edit</span>
                    )}
                    {(f.by_subagents ?? 0) > 0 && <span>{f.by_subagents} by subagents</span>}
                    <span className="ml-auto tabular-nums">{String(f.last_ts ?? '').slice(0, 19)}</span>
                  </Meta>
                  {!list && got !== 'failed' && (
                    <button
                      onClick={() => readEdits(f.file)}
                      disabled={got === 'fetching'}
                      className="mt-1 rounded border border-edge px-2 py-0.5 text-2xs text-ink-dim
                                 hover:text-ink disabled:text-ink-faint"
                    >
                      {got === 'fetching' ? 'Fetching the edits' : 'Show the edits'}
                    </button>
                  )}
                  {got === 'failed' && (
                    <p role="alert" className="mt-1 text-2xs text-warn">
                      The edits of this file could not be fetched.
                    </p>
                  )}
                  {list && list.map((e) => {
                    const d = diffs[e.tool_use_id]
                    const whole = d && d !== 'fetching' && d !== 'failed' ? d : null
                    return (
                      <div key={e.tool_use_id} className="mt-1 border-l-2 border-edge pl-2">
                        <Meta>
                          <span className={e.outcome === 'refused' ? 'font-semibold text-warn' : 'text-ink-dim'}>
                            {e.outcome ?? 'outcome not recorded'}
                          </span>
                          <span>{e.tool_name}{e.kind ? ` (${e.kind})` : ''}</span>
                          {e.additions !== null && e.deletions !== null ? (
                            <span className="tabular-nums">
                              <span className="text-good">+{e.additions}</span>{' '}
                              <span className="text-warn">-{e.deletions}</span>
                            </span>
                          ) : (
                            <span>{e.old_lines ?? 0} lines to {e.new_lines ?? 0}, no patch recorded</span>
                          )}
                          {e.is_sidechain ? <span>by a subagent</span> : null}
                          <span className="ml-auto tabular-nums">{String(e.ts ?? '').slice(0, 19)}</span>
                        </Meta>
                        {/* THREE STATES. A list of hunks is a recorded patch; an empty list is a
                            created file whose whole content is new; null is an edit whose result
                            was never recorded, which is every subagent edit on the author's store. */}
                        {whole && (
                          whole.hunks && whole.hunks.length
                            ? <Diff hunks={whole.hunks} />
                            : whole.hunks
                              ? <BeforeAfter oldText={null} newText={whole.new_text}
                                  why="A created file: every line of it is new." />
                              : <BeforeAfter oldText={whole.old_text} newText={whole.new_text}
                                  why="Before and after only. No result was recorded for this edit, which is so for every edit a subagent makes, so there are no context lines." />
                        )}
                        {d === 'failed' && (
                          <p role="alert" className="mt-1 text-2xs text-warn">
                            This change could not be fetched.
                          </p>
                        )}
                        {!whole && d !== 'failed' && (
                          <button
                            onClick={() => readChange(e.tool_use_id)}
                            disabled={d === 'fetching'}
                            className="mt-1 rounded border border-edge px-2 py-0.5 text-2xs text-ink-dim
                                       hover:text-ink disabled:text-ink-faint"
                          >
                            {d === 'fetching' ? 'Fetching the diff' : 'Show the diff'}
                          </button>
                        )}
                      </div>
                    )
                  })}
                </Row>
              )
            })}
          </Section>
        </>
      )}
    </main>
  )
}
