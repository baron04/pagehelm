import {afterEach, describe, expect, it, vi} from 'vitest'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'

/** A Runtime with only what asking the application to open a page reads. */
function runtime(openPage: ((url: string) => unknown) | undefined): EmbeddedWebRuntime {
  const state = Object.create(EmbeddedWebRuntime.prototype) as EmbeddedWebRuntime
  state.openPage = openPage
  return state
}

afterEach(() => { vi.useRealTimers() })

describe('an application asked to open its own page has opened it only when it says exactly so', () => {
  it('takes true for an answer and nothing else', async () => {
    await expect(runtime(async () => true)._openInApplication('/a')).resolves.toBe(true)
    await expect(runtime(() => true)._openInApplication('/a')).resolves.toBe(true)
    for (const answer of [false, undefined, null, 'yes', 1, {navigated: true}]) {
      await expect(runtime(async () => answer)._openInApplication('/a')).resolves.toBe(false)
    }
  })
  it('has not opened it when it throws, when it has no way to, or when it does not answer in time', async () => {
    await expect(runtime(async () => { throw new Error('router gone') })._openInApplication('/a')).resolves.toBe(false)
    await expect(runtime(() => { throw new Error('router gone') })._openInApplication('/a')).resolves.toBe(false)
    await expect(runtime(undefined)._openInApplication('/a')).resolves.toBe(false)
    vi.useFakeTimers()
    const waiting = runtime(() => new Promise(() => {}))._openInApplication('/a')
    await vi.advanceTimersByTimeAsync(6_000)
    await expect(waiting).resolves.toBe(false)
  })
})
