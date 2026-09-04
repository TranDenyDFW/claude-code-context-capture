/**
 * A figure the library refuses to draw is not a library that did not arrive.
 *
 * Both failures shared one catch, so a bad trace reported "The chart library could not be loaded"
 * and threw away a module that had downloaded fine, making every other chart on the page re-fetch
 * 3.5 MB. A separate file from Plot.test.tsx because the module mock is per file and the shared
 * promise is module state.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

const lib = vi.hoisted(() => ({ imports: 0, draws: 0, fail: true }))

vi.mock('plotly.js-dist-min', () => ({
  get default() {
    lib.imports += 1
    return {
      react: () => {
        lib.draws += 1
        return lib.fail ? Promise.reject(new Error('bad trace')) : Promise.resolve(null)
      },
      purge: () => {},
    }
  },
}))

import { Plot } from './Plot'

describe('a figure that will not draw', () => {
  it('says the CHART failed, not the library, and keeps the module it already has', async () => {
    const { unmount } = render(<Plot figure={{ data: [{ bad: true }], layout: {} }} />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('could not be drawn')
    expect(alert.textContent).not.toContain('library')
    const after = lib.imports
    unmount()

    lib.fail = false
    render(<Plot figure={{ data: [{ ok: true }], layout: {} }} />)
    await waitFor(() => expect(lib.draws).toBeGreaterThan(1))
    expect(screen.queryByRole('alert')).toBeNull()
    // The module was never discarded: a bad figure is not a reason to re-download the library.
    expect(lib.imports).toBe(after)
  })
})
