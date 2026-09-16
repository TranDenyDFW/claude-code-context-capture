import { useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '@/api'
import { Confirm } from './Confirm'
import { restartOutcome, restartQuestion } from './restart'

/**
 * Show every account's chats, or only the signed-in account's.
 *
 * The desktop app separates accounts with a directory: `<account>/<org>/local_<uuid>.json`, and the
 * listing IS the session list. All makes every one of those directories on this machine resolve to
 * one of them, so whichever account is signed in reads the same chats. Current puts them back.
 *
 * THE NUMBERS ARE ON HOVER, not in the row. "33 chat(s) shared across 2 account directories" sat
 * beside the switch as a sentence; the user asked for the row to say less. All's hover carries
 * what All shows (every record in the shared directory), Current's what Current would show the
 * signed-in account (its own chats, derived on the server from the sharing backup), and the
 * label's hover carries the one thing a switch needs to know: it closes Claude and starts it
 * again, after asking.
 *
 * EVERY SWITCH ASKS, THEN THE SERVER DOES THE REST. A directory the app holds cannot be moved, so
 * the page used to refuse while Claude was open and tell the person to quit it; the user's rule
 * now: "prompt the user to continue ... and only continue if they confirm". A click on All,
 * Current or Cover now opens a confirm naming what will happen (every Claude window closed, the
 * directories moved, Claude started again); Continue sends `restart: true` and the server quits,
 * acts and relaunches; the outcome says whether Claude came back.
 */
function Problem({ error }: { error: unknown }) {
  const detail = error instanceof ApiError ? error.detail : undefined
  const said =
    detail && typeof detail === 'object' && 'error' in detail
      ? String((detail as { error: unknown }).error)
      : error instanceof Error
        ? error.message
        : String(error)
  return (
    <p className="mt-2 rounded-md border border-bad/40 bg-bad/5 px-3 py-2 text-sm text-bad">
      {said}
    </p>
  )
}

export const RESTART_NOTE =
  'Claude is started again afterwards; it reads these directories when it starts.'
export const QUIT_NOTE = 'Switching closes every Claude window and starts Claude again; you are asked first.'
export const COVER_NOTE = 'covered when Claude next closes'
export const COVER_HOVER =
  'A pair the app created at a sign-in since sharing began. That account reads a list of its ' +
  'own until it is covered. The server covers it a minute after Claude closes, or now, after ' +
  'asking, with Claude closed and started again.'

type Ask = 'all' | 'current' | 'cover'

export function AccountSharing({
  writesEnabled,
  onChanged,
}: {
  writesEnabled: boolean
  onChanged?: () => void
}) {
  const client = useQueryClient()
  // READ AGAIN when the page comes back and whenever a sibling says something changed. The count
  // was read once on mount, so a sweep or a delete while the page sat open left it stale until a
  // reload. `refetchOnWindowFocus` overrides the app's global false for this one cheap call, and
  // every `client.invalidateQueries()` the other header controls fire reaches this key too.
  const query = useQuery({
    queryKey: ['accounts'],
    queryFn: api.accounts.state,
    refetchOnWindowFocus: true,
    retry: false,
  })
  const state = query.data ?? null
  const [error, setError] = useState<unknown>(null)
  const [restart, setRestart] = useState(false)
  // WHICH SWITCH IS BEING ASKED ABOUT. Nothing moves until Continue.
  const [asking, setAsking] = useState<Ask | null>(null)
  const group = useRef<HTMLDivElement>(null)

  // ONE ACCOUNT IS NOT A CHOICE. With a single pair on the machine there is nothing to share and
  // the control would offer a toggle that changes nothing.
  if (!state || !state.supported || state.pairs < 2) return null

  const mode = state.intended === 'all' ? 'all' : 'current'
  // THE PAIRS SHARING DOES NOT COVER YET. The app creates `<account>/<org>` at a sign-in with an
  // organisation the links never named, and that account reads its own list from then on; at the
  // next switch the app folds it into the shared directory and the account comes back to nothing
  // (measured: fifteen chats). The server covers such a pair a minute after Claude closes; the
  // line says so, and the button does it now, after asking.
  const uncovered = mode === 'all' ? (state.uncovered ?? []) : []

  const does: Record<Ask, string> = {
    all: 'links the account directories so every account reads the same chats',
    current: 'un-links the account directories so each account goes back to its own chats',
    cover: 'moves the uncovered account directories into the shared one and links them',
  }
  const did: Record<Ask, string> = {
    all: 'The directories are linked.',
    current: 'The directories are separate again.',
    cover: 'The pairs are covered.',
  }

  async function run(which: Ask) {
    setError(null)
    setRestart(false)
    const report =
      which === 'cover' ? await api.accounts.reconcile(true) : await api.accounts.share(which, true)
    client.setQueryData(['accounts'], report.state)
    setRestart(report.restart_required)
    onChanged?.()
    return restartOutcome(report.restart, did[which])
  }

  const progress = () =>
    api.accounts.state().then((now) => (now.app_running ? 'Claude is running' : 'Claude is closed'))

  // EVERYTHING A SWITCH NEEDS TO KNOW IS ON THE TWO BUTTONS, one line each: what the side shows,
  // that the switch closes and restarts Claude after asking. The "ACCOUNT" word that used to
  // carry the restart note is gone, the user's choice: the buttons say what they are.
  const notes = '\n' + QUIT_NOTE + '\n' + RESTART_NOTE
  // THE CURRENT NUMBER SAYS WHERE IT CAME FROM. From the tags it is the chats made under the
  // signed-in account, and the untagged remainder is named; from the manifest or the directories
  // it is what un-sharing would hand that account back.
  const untagged = state.untagged ?? 0
  // ONE NUMBER, SAID ONCE. The page's count leads when the server has it: it is the number the
  // population list's "Signed-in account's chats" shows, read from the same function, so the
  // two cannot disagree. The app's own count (its records) follows as "in the app".
  const listedHere = typeof state.listed === 'number'
  const appCount =
    state.current_chats === null || state.current_chats === undefined
      ? 'not known while sharing is on'
      : `${state.current_chats}`
  const hover = {
    all:
      (listedHere
        ? `Every account's chats: ${state.listed} listed here; ${state.chats_visible} in the app ` +
          `across ${state.pairs} account directories`
        : `Every account's chats: ${state.chats_visible} across ${state.pairs} account directories`) +
      notes,
    current:
      (typeof state.current_listed === 'number'
        ? `Only the signed-in account's own chats: ${state.current_listed} listed here; ` +
          `${appCount} in the app` +
          (state.current_source === 'tags' && untagged ? `; ${untagged} of unknown account` : '')
        : state.current_chats === null || state.current_chats === undefined
          ? "Only the signed-in account's own chats: not known while sharing is on"
          : state.current_source === 'tags'
            ? `Only the signed-in account's own chats: ${state.current_chats}, by the account each ` +
              `chat was made under` +
              (untagged ? `; ${untagged} of unknown account` : '')
            : `Only the signed-in account's own chats: ${state.current_chats}`) + notes,
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <div
          ref={group}
          role="group"
          aria-label="Account"
          data-restart={restart ? 'true' : 'false'}
          className={`inline-flex overflow-hidden rounded-md border ${
            restart ? 'border-warn/70' : 'border-edge'
          }`}
        >
          {(['all', 'current'] as const).map((option) => (
            <button
              key={option}
              type="button"
              disabled={!writesEnabled || asking !== null}
              aria-pressed={mode === option}
              title={hover[option]}
              onClick={() => (option === mode ? undefined : setAsking(option))}
              className={
                'px-2.5 py-1.5 text-sm transition-colors disabled:opacity-50 ' +
                (mode === option
                  ? 'bg-panel text-ink'
                  : 'bg-page text-ink-dim hover:text-ink')
              }
            >
              {option === 'all' ? 'All' : 'Current'}
            </button>
          ))}
        </div>
      </div>
      {uncovered.length ? (
        <p
          data-uncovered={uncovered.length}
          title={
            COVER_HOVER +
            '\n' +
            uncovered.map((p) => (p.why ? p.path + ': ' + p.why : p.path)).join('\n')
          }
          className="flex items-center gap-2 text-xs text-warn"
        >
          <span>
            {uncovered.length} account pair{uncovered.length === 1 ? '' : 's'} not yet covered;{' '}
            {COVER_NOTE}
          </span>
          <button
            type="button"
            disabled={!writesEnabled || asking !== null}
            title={
              !writesEnabled
                ? 'This server was started without writes'
                : 'Move these into the shared directory now and link them, after asking'
            }
            onClick={() => setAsking('cover')}
            className="rounded-md border border-edge bg-page px-2 py-0.5 text-xs text-ink-dim
                       transition-colors hover:text-ink disabled:opacity-50"
          >
            Cover now
          </button>
        </p>
      ) : null}
      {asking ? (
        <Confirm
          label={asking === 'cover' ? 'Cover now' : asking === 'all' ? 'Show every account’s chats' : 'Show only the signed-in account’s chats'}
          question={restartQuestion(state.app_running, does[asking], true)}
          detail={asking === 'current'
            ? 'This takes chats away from every other account on the machine.'
            : undefined}
          action={() => run(asking)}
          progress={state.app_running ? progress : undefined}
          opener={group}
          onClose={() => setAsking(null)}
        />
      ) : null}
      {error ? <Problem error={error} /> : null}
    </div>
  )
}
