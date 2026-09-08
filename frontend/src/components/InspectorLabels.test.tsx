/**
 * The drawer names a field the way the table above it does.
 *
 * Clicking a Messages row listed `ts`, `role`, `type` under a table headed `Date & Time`, `Record
 * Type`, `Written By`. Same data, two vocabularies, and the one the drawer chose is the one
 * DataTable.tsx names as wrong: "COLUMN NAMES come from column_label(). The raw ids are schema, not
 * English."
 *
 * The two ids it exposed are exactly the two the label map exists to correct, and that correction is
 * not cosmetic. c4x/theme.py records why: `role` and `type` were BOTH the transcript record's own
 * `type` field, so a directory listing and a question both read "user", and 86.5% of the records
 * typed 'user' were tool results. `Record Type` and `Written By` carry the whole of that
 * distinction.
 */
import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import type { ColumnMeta, TabPayload } from '@/api'
import { Pane } from './Pane'

function column(id: string, label: string): ColumnMeta {
  return { id, label, numeric: false, specifier: null, align: 'left', hidden: false, bands: [] }
}

function payload(columns: ColumnMeta[]): TabPayload {
  return {
    tab: 'tab-session', session: null, scope: 'main', cohort: null,
    figures: [], text: [], plotly: [],
    tables: [{
      id: 'tbl-messages',
      columns: ['ts', 'role', 'type'],
      rows: [{ ts: '2026-09-07 01:28:15', role: 'user', type: 'tool_result' }],
    }],
    meta: [{
      id: 'tbl-messages', title: 'Messages', columns, filterable: true, page_size: null,
    }],
  }
}

const LABELLED = [column('ts', 'Date & Time'), column('role', 'Record Type'),
                  column('type', 'Written By')]

describe('the row drawer', () => {
  it('names each field the way the table heading does', () => {
    render(<Pane payload={payload(LABELLED)} />)
    fireEvent.click(screen.getByRole('table').querySelector('tbody tr')!)
    const dialog = screen.getByRole('dialog')
    expect(dialog.textContent).toContain('Date & Time')
    expect(dialog.textContent).toContain('Record Type')
    expect(dialog.textContent).toContain('Written By')
  })

  it('does not show the raw schema ids the table hides (gate can fail)', () => {
    // THE DEFECT. Without this the test above passes on a drawer showing BOTH vocabularies, which
    // is neither the bug nor the fix.
    render(<Pane payload={payload(LABELLED)} />)
    fireEvent.click(screen.getByRole('table').querySelector('tbody tr')!)
    const terms = [...screen.getByRole('dialog').querySelectorAll('dt')].map((n) => n.textContent)
    expect(terms).not.toContain('ts')
    expect(terms).not.toContain('role')
    expect(terms).not.toContain('type')
  })

  it('keeps the id for a field the payload does not describe', () => {
    // An unlabelled field is still worth showing, and inventing a label for it would be guessing.
    render(<Pane payload={payload([column('ts', 'Date & Time')])} />)
    fireEvent.click(screen.getByRole('table').querySelector('tbody tr')!)
    const terms = [...screen.getByRole('dialog').querySelectorAll('dt')].map((n) => n.textContent)
    expect(terms).toContain('Date & Time')
    expect(terms).toContain('role')
    expect(terms).toContain('type')
  })

  it('still shows the value, whatever the field is called', () => {
    // The rename must move the LABEL and nothing else. A map keyed the wrong way round would pass
    // both tests above and quietly relabel the data.
    render(<Pane payload={payload(LABELLED)} />)
    fireEvent.click(screen.getByRole('table').querySelector('tbody tr')!)
    const dialog = screen.getByRole('dialog')
    expect(dialog.textContent).toContain('2026-09-07 01:28:15')
    expect(dialog.textContent).toContain('tool_result')
  })
})
