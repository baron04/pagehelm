import {describe, expect, it, vi} from 'vitest'
import {executeCommand, mayNavigate, nativeNavigation} from '../src/runtime/executor'
import type {Page} from '../src/runtime/page'

function pageWithScroll(scroll: ReturnType<typeof vi.fn>): Page {
  return {scroll} as unknown as Page
}

describe('the generic scroll command is bounded before PageController receives it', () => {
  it('passes an anchor through so that its own container is the one scrolled', async () => {
    const scroll = vi.fn().mockResolvedValue({success: true})
    await executeCommand(
      {command_id: 'a9', action: 'scroll', args: {direction: 'down', pages: 2, ref: 'e7'},
        expect_navigation: false},
      () => pageWithScroll(scroll), {},
    )
    expect(scroll).toHaveBeenCalledWith(true, 2, 'e7')
  })
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

describe('a same-document fragment anchor is a click, not a navigation', () => {
  const here = window.location.href.split('#')[0]
  const anchor = (written: string) => ({
    tagName: 'A', href: new URL(written, here).href, getAttribute: () => written,
    hasAttribute: (name: string) => name === 'href',
  })
  const click = (ref: string) => ({command_id: 'c', action: 'click' as const, args: {ref},
    expect_navigation: false})

  it('does not wait for a navigation when a widget uses href="#" as a button', () => {
    const page = {element: () => anchor('#')} as unknown as Page
    expect(nativeNavigation(anchor('#') as never)).toBeNull()
    expect(nativeNavigation(anchor(here + '#sales') as never)).toBeNull()
    expect(mayNavigate(click('e37'), () => page)).toBe(false)
  })

  it('still waits for a link to another path or query', () => {
    const other = anchor('/somewhere-else#section')
    const query = anchor('?page=2')
    expect(nativeNavigation(other as never)?.kind).toBe('link')
    expect(nativeNavigation(query as never)?.kind).toBe('link')
    expect(mayNavigate(click('e1'), () => ({element: () => other}) as unknown as Page))
      .toBe(true)
  })

  it('an explicit expect_navigation is still honoured', () => {
    const page = {element: () => anchor('#')} as unknown as Page
    expect(mayNavigate({...click('e37'), expect_navigation: true}, () => page)).toBe(true)
  })
})
