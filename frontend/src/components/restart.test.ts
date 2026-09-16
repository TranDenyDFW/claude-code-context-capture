import { describe, expect, it } from 'vitest'
import { restartOutcome, restartQuestion } from './restart'
import type { RestartReport } from '@/api'

/** The sentences a confirmed restart shows, one per way it can go. */
function report(over: Partial<RestartReport> = {}): RestartReport {
  return { was_running: true, quit: true, killed: 12, relaunched: true, how: 'shell',
           launch: ['explorer.exe', 'shell:AppsFolder\\F!Claude'], why: 'relaunched', ...over }
}

describe('restartOutcome', () => {
  it('says Claude was closed and started again, and how, when it came back', () => {
    expect(restartOutcome(report(), 'The directories are linked.'))
      .toEqual({ ok: true, text: 'The directories are linked. Claude was closed and started again.' })
    expect(restartOutcome(report({ how: 'task', why: 'relaunched through the task scheduler' }), 'Done.'))
      .toEqual({ ok: true, text: 'Done. Claude was closed and started again through the task scheduler.' })
  })

  it('says Claude was not running, and that it reads the files when it next starts', () => {
    const r = report({ was_running: false, quit: false, killed: 0, relaunched: false, how: null,
                       why: 'Claude was not running; it reads these records when it next starts' })
    expect(restartOutcome(r, '3 records written.')).toEqual({
      ok: true,
      text: '3 records written. Claude was not running; it reads these records when it next starts.',
    })
  })

  it('says nothing needed reading again when the write wrote nothing', () => {
    const r = report({ quit: false, killed: 0, relaunched: false, how: null,
                       why: 'nothing changed that Claude would need to read again' })
    expect(restartOutcome(r, '0 records named.').ok).toBe(true)
    expect(restartOutcome(r, '0 records named.').text).toContain('left running')
  })

  it('is NOT ok when Claude is closed and did not come back, and says to start it by hand', () => {
    const r = report({ relaunched: false, how: null,
                       why: 'Claude is closed: the app did not come back within 20 s; the task launch failed: denied; start it by hand' })
    const out = restartOutcome(r, 'The directories are linked.')
    expect(out.ok).toBe(false)
    expect(out.text).toContain('start it by hand')
  })

  it('is the write alone when the server sent no restart report', () => {
    expect(restartOutcome(null, 'Done.')).toEqual({ ok: true, text: 'Done.' })
    expect(restartOutcome(undefined, 'Done.')).toEqual({ ok: true, text: 'Done.' })
  })
})

describe('restartQuestion', () => {
  it('names the closing, the write and the restart, in the order they happen', () => {
    expect(restartQuestion(true, 'links the account directories', true)).toBe(
      'This closes every Claude window, including any chat in progress, links the account ' +
      'directories, and starts Claude again. Continue?')
    expect(restartQuestion(true, 'writes 3 records', false)).toBe(
      'This writes 3 records, then closes every Claude window, including any chat in progress, ' +
      'and starts Claude again so it lists the result. Continue?')
  })

  it('says nothing is quit when Claude is not running', () => {
    expect(restartQuestion(false, 'writes 3 records', false)).toBe(
      'Claude is not running, so nothing is quit: this writes 3 records, and Claude reads the ' +
      'result when it next starts. Continue?')
  })
})
