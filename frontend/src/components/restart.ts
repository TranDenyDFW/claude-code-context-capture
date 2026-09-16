import type { RestartReport } from '@/api'

/** What a confirm dialog shows once its action has run: a sentence, and whether it went well. */
export interface Outcome {
  ok: boolean
  text: string
}

/**
 * The sentence for a write the server ran with Claude quit and started again around it.
 *
 * `did` is the write's own sentence ("The directories are linked."); the server's `restart`
 * report says what happened to Claude: started again (through the shell, or through the task
 * scheduler when the shell activation brought nothing back, which is what happened on the test
 * laptop), never running (it reads the files when it next starts), nothing to read again (a
 * write that wrote nothing), or closed and not back, which is the one outcome that asks the
 * person to do something.
 */
export function restartOutcome(restart: RestartReport | null | undefined, did: string): Outcome {
  if (!restart) return { ok: true, text: did }
  if (restart.relaunched) {
    return {
      ok: true,
      text: `${did} Claude was closed and started again` +
        (restart.how === 'task' ? ' through the task scheduler.' : '.'),
    }
  }
  if (!restart.was_running) return { ok: true, text: `${did} ${restart.why}.` }
  if (restart.why.startsWith('nothing changed')) {
    return { ok: true, text: `${did} Nothing for Claude to read again, so it was left running.` }
  }
  return { ok: false, text: `${did} ${restart.why}` }
}

/** The question a Claude restart asks, for a write that needs the app closed first or after. */
export function restartQuestion(appRunning: boolean, does: string, before: boolean): string {
  if (!appRunning) {
    return `Claude is not running, so nothing is quit: this ${does}, and Claude reads the ` +
      'result when it next starts. Continue?'
  }
  return before
    ? `This closes every Claude window, including any chat in progress, ${does}, and starts ` +
      'Claude again. Continue?'
    : `This ${does}, then closes every Claude window, including any chat in progress, and ` +
      'starts Claude again so it lists the result. Continue?'
}
