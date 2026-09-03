/**
 * A chart library that fails to download says so. It used to leave every chart on the page blank
 * with nothing in the DOM or the console naming the cause. Found by a review sweep.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

vi.mock('plotly.js-dist-min', () => {
  throw new Error('chunk failed')
})

import { Plot } from './Plot'

describe('a chart whose library cannot be loaded', () => {
  it('shows the failure where the chart would have been', async () => {
    render(<Plot figure={{ data: [], layout: {} }} />)
    const alert = await screen.findByRole('alert')
    // The loader's own wording travels in the parenthetical; vitest wraps a mock factory's error
    // in its message, so only the presence of a reason is asserted, not its text.
    expect(alert.textContent).toContain('could not be loaded (')
  })
})
