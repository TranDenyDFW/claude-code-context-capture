/**
 * A chart library that failed to download is tried again for the next chart.
 *
 * `Plot` keeps one shared promise so eight charts trigger one download. A rejected promise left in
 * that slot would make every later chart fail with a download that is no longer being attempted,
 * so the catch clears it. Nothing tested that: Plot.test.tsx throws unconditionally and can never
 * see a second attempt succeed, which is why this is a separate file with its own module mock.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

const lib = vi.hoisted(() => ({ attempts: 0, draws: 0 }))

vi.mock('plotly.js-dist-min', () => ({
  get default() {
    lib.attempts += 1
    // The first import fails the way a missing chunk does; every later one succeeds.
    if (lib.attempts === 1) throw new Error('chunk failed')
    return { react: () => { lib.draws += 1; return Promise.resolve(null) }, purge: () => {} }
  },
}))

import { Plot } from './Plot'

describe('after a failed download', () => {
  it('a later chart tries again instead of inheriting the rejected promise', async () => {
    const first = render(<Plot figure={{ data: [], layout: {} }} />)
    expect((await screen.findByRole('alert')).textContent).toContain('could not be loaded (')
    first.unmount()

    render(<Plot figure={{ data: [], layout: {} }} />)
    // Asserted on the draw, never on the import count: the unmount path may take an attempt of
    // its own, and what matters is that a chart appears at all.
    await waitFor(() => expect(lib.draws).toBeGreaterThan(0))
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
