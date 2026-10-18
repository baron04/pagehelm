import {afterEach, describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'

const runtimes: EmbeddedWebRuntime[] = []

function busyRuntime(): EmbeddedWebRuntime {
  const runtime = new EmbeddedWebRuntime({
    endpoint: 'https://agent.test', sessionId: crypto.randomUUID(),
    clientToken: 'token', sessionGeneration: 'generation', panel: false,
  })
  runtime.panelConfiguration = DEFAULT_CONFIGURATION
  runtime.stored.taskInFlight = true
  runtime.stored.activeMessageId = 'old-message'
  runtimes.push(runtime)
  return runtime
}

afterEach(() => {
  for (const runtime of runtimes.splice(0)) runtime.stop()
})

describe('sending a preserved draft replaces an unanswered turn', () => {
  it('does not close another page\'s write window when observing its finished turn', async () => {
    const runtime = busyRuntime()
    Object.assign(runtime.config, {pageWriteGuard: '/woa/page-guard'})
    runtime.stored.activeMessageId = ''
    runtime.stored.taskInFlight = false
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    try {
      await runtime._handleEvent({kind: 'progress', turn_id: 'other-page-turn', payload: {state: 'finished'}})
      expect(fetcher).not.toHaveBeenCalled()
      expect(runtime.stopped).toBe(false)
    } finally { vi.unstubAllGlobals() }
  })
  it('cancels first and submits only after terminal cancellation arrives', async () => {
    const runtime = busyRuntime()
    const cancel = vi.spyOn(runtime, 'cancelTask').mockResolvedValue()
    const submit = vi.spyOn(runtime, 'submitTask').mockResolvedValue({accepted: true})

    await runtime.submitOrReplaceTask('  ask the other question  ')

    expect(cancel).toHaveBeenCalledOnce()
    expect(submit).not.toHaveBeenCalled()

    await runtime._handleEvent({
      kind: 'progress', turn_id: 'old-message', payload: {state: 'cancelled'},
    })
    await Promise.resolve()

    expect(submit).toHaveBeenCalledOnce()
    expect(submit).toHaveBeenCalledWith('ask the other question')
  })

  it('does not submit the replacement when cancellation fails', async () => {
    const runtime = busyRuntime()
    vi.spyOn(runtime, 'cancelTask').mockRejectedValue(new Error('offline'))
    const submit = vi.spyOn(runtime, 'submitTask').mockResolvedValue({accepted: true})

    await expect(runtime.submitOrReplaceTask('next')).rejects.toThrow('offline')
    await runtime._handleEvent({
      kind: 'progress', turn_id: 'old-message', payload: {state: 'cancelled'},
    })
    await Promise.resolve()

    expect(submit).not.toHaveBeenCalled()
  })
})
