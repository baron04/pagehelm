import {describe, expect, it, vi} from 'vitest'
import {executeCommand} from '../src/runtime/executor'
import type {Page} from '../src/runtime/page'

function pageWithScroll(scroll: ReturnType<typeof vi.fn>): Page {
  return {scroll} as unknown as Page
}

describe('the generic scroll command is bounded before PageController receives it', () => {
  it('scrolls one viewport down by default', async () => {
    const scroll = vi.fn().mockResolvedValue({success: true})

    await executeCommand(
      {command_id: 'a1', action: 'scroll', args: {direction: 'down'},
        expect_navigation: false},
      () => pageWithScroll(scroll), {},
    )

    expect(scroll).toHaveBeenCalledWith(true, 1)
  })

  it('accepts at most three whole viewports and refuses unknown directions', async () => {
    const scroll = vi.fn().mockResolvedValue({success: true})
    const access = () => pageWithScroll(scroll)

    await executeCommand(
      {command_id: 'a2', action: 'scroll', args: {direction: 'up', pages: 3},
        expect_navigation: false}, access, {})
    expect(scroll).toHaveBeenLastCalledWith(false, 3)
    await expect(executeCommand(
      {command_id: 'a3', action: 'scroll', args: {direction: 'left'},
        expect_navigation: false}, access, {})).rejects.toThrow(/direction/)
    await expect(executeCommand(
      {command_id: 'a4', action: 'scroll', args: {direction: 'down', pages: 4},
        expect_navigation: false}, access, {})).rejects.toThrow(/integer from 1 to 3/)
  })
})
