import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import type {PanelConfiguration} from '../src/contract/panel'
import type {PanelActions} from '../src/runtime/panelHost'
import {autoBoot} from '../src/runtime/boot'
import {engagementRecord} from '../src/runtime/engagement'
import {createShell} from '../src/runtime/shell'

vi.mock('../src/runtime/shell', async () => {
  const {PanelHost} = await import('../src/runtime/panelHost')
  return {createShell: vi.fn((actions: PanelActions, configuration: PanelConfiguration) => ({
    host: new PanelHost({actions, configuration}), container: document.createElement('div'),
  }))}
})

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  delete window.__WOA_ASSISTANT__
  document.body.innerHTML = '<script data-woa-bootstrap="/bootstrap"></script>'
  vi.clearAllMocks()
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
})
afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
  delete window.__WOA_ASSISTANT__
})

describe('the first panel state depends on a locally saved session', () => {
  it('shows the default immediately and opening creates no session without an ID', () => {
    const supervisor = autoBoot()
    const host = vi.mocked(createShell).mock.results[0]!.value.host
    expect(host.snapshot().connection.state).toBe('ready')
    host.panelVisibility(true)
    expect(supervisor?.state.kind).toBe('idle')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('keeps the greeting hidden while opening restores a saved session', () => {
    engagementRecord().remember('actor-session')
    const supervisor = autoBoot()
    const host = vi.mocked(createShell).mock.results[0]!.value.host
    expect(host.snapshot().connection.state).toBe('connecting')
    expect(fetch).not.toHaveBeenCalled()
    host.panelVisibility(true)
    expect(supervisor?.state.kind).toBe('starting')
    expect(host.snapshot().connection.state).toBe('connecting')
    expect(fetch).toHaveBeenCalledOnce()
  })
})
