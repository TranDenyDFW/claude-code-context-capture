import type { Cohort, Selection } from '@/api'

/**
 * Pick the two things Compare measures.
 *
 * WITHOUT THIS THE TAB CANNOT ANSWER THE QUESTION IT EXISTS FOR. The server has always taken
 * `compare_with`, and `compare_layout()` has always read arm A off the header selection, but the
 * page sent neither, so Compare rendered whatever `default_arm_b()` happened to pick: the most
 * recently active session that was not arm A. Comparing a chat against its own fork, which is the
 * case that makes the tab worth having, could not be expressed at all.
 *
 * A CHAT PICKER, deliberately, and only here. The header has none because nobody remembers a
 * session by name out of 317. On this tab the reader is not recalling a session, they are choosing
 * two specific ones they already have in mind, and the labels lead with the project and end with
 * the date and time, so "the fork" and "the one it came from" are told apart by reading.
 *
 * Arm A writes through to the SAME `selection.session` the header chip shows, rather than keeping
 * a second copy. Two states for one fact drift, and the header would then name a session Compare
 * was not using.
 */

const SAME_THING = 'Comparing something with itself gives a table of 1.0 ratios and no finding.'

function Choice({
  label,
  hint,
  value,
  options,
  disabledValue,
  onChange,
}: {
  label: string
  hint: string
  value: string
  options: { value: string; label: string }[]
  /** Excluded from the list, so the two arms cannot be set to the same thing. */
  disabledValue?: string | null
  onChange: (value: string) => void
}) {
  return (
    <label className="flex min-w-0 flex-1 flex-col gap-1">
      <span className="text-xs font-semibold text-ink">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full truncate rounded-md border border-edge bg-panel px-2 py-1.5 text-sm
                   text-ink"
      >
        {options
          .filter((o) => !o.value || o.value !== disabledValue)
          .map((o) => (
            <option key={o.value || '(none)'} value={o.value}>
              {o.label}
            </option>
          ))}
      </select>
      <span className="text-2xs text-ink-faint">{hint}</span>
    </label>
  )
}

export function CompareArms({
  selection,
  sessions,
  cohorts,
  onChange,
}: {
  selection: Selection
  /** Every session, labelled `project · title · date`. From /api/selector. */
  sessions: Cohort[]
  cohorts: Cohort[]
  onChange: (next: Partial<Selection>) => void
}) {
  const kind = selection.compareKind ?? 'session'
  const armB = selection.compareWith ?? ''

  const chatOptions = [
    { value: '', label: selection.cohort ? 'The selected population' : 'The whole store' },
    ...sessions.map((s) => ({ value: s.value, label: s.label })),
  ]
  const populationOptions = [
    { value: '', label: 'Pick a population…' },
    ...cohorts.map((c) => ({ value: c.value, label: c.label })),
  ]

  return (
    <div className="mb-4 rounded-lg border border-edge bg-panel/50 p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
        <Choice
          label="Arm A"
          hint="Also the header selection, so the two cannot disagree."
          value={selection.session ?? ''}
          options={chatOptions}
          disabledValue={kind === 'session' ? armB : null}
          onChange={(value) => onChange({ session: value || null })}
        />
        <div className="flex flex-col gap-1 sm:pt-5">
          <span className="text-center text-xs text-ink-faint">against</span>
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <Choice
            label="Arm B"
            hint={kind === 'session' ? 'One chat.' : 'A whole population, measured the same way.'}
            value={armB}
            options={kind === 'session' ? chatOptions : populationOptions}
            disabledValue={kind === 'session' ? (selection.session ?? null) : null}
            onChange={(value) => onChange({ compareWith: value || null, compareKind: kind })}
          />
          <div className="flex gap-1">
            {(['session', 'cohort'] as const).map((option) => (
              <button
                key={option}
                onClick={() =>
                  // The target is cleared with the kind. A session id left behind while the kind
                  // says cohort makes the server resolve a population that does not exist, and the
                  // pane comes back describing nothing with no error to explain it.
                  onChange({ compareKind: option, compareWith: null })
                }
                className={`rounded-md border px-2 py-0.5 text-2xs transition-colors ${
                  kind === option
                    ? 'border-accent/60 bg-accent/10 text-accent'
                    : 'border-edge bg-panel text-ink-dim hover:text-ink'
                }`}
              >
                {option === 'session' ? 'a chat' : 'a population'}
              </button>
            ))}
          </div>
        </div>
      </div>
      {armB && armB === selection.session && (
        <p className="mt-2 text-xs text-warn">{SAME_THING}</p>
      )}
      {!armB && (
        <p className="mt-2 text-xs text-ink-faint">
          Arm B is unset, so the server picks the most recently active chat that is not arm A.
          Choose one to compare a fork against what it came from.
        </p>
      )}
    </div>
  )
}
