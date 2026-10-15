import {describe, expect, it, vi} from 'vitest'
import type {PanelSnapshot} from '../src/bridge/host'
import {effectPhase} from '../src/components/ScreenEffects'
import {activityPoint} from '../src/runtime/activityPoint'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'
import {PanelHost} from '../src/runtime/panelHost'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import type {PageControllerApi} from '../src/runtime/page'

const idle: PanelSnapshot = {
  connection: {state: 'ready', busy: false, stopping: false, online: true},
  entries: [], pendingApproval: null, notice: null, hasOlder: false, loadingOlder: false,
  closed: false, optimistic: null,
}

describe('turn light is scoped to a local confirmation click', () => {
  const confirmation = {turnId: 't1', approvalId: 'a1'}
  const working = {...idle, connection: {...idle.connection, state: 'acting' as const, busy: true}}
  const pendingApproval = {turn_id: 't1', approval_id: 'a1', expires_at: 101} as
    NonNullable<PanelSnapshot['pendingApproval']>

  it('never starts for submission, reading, execution, a pending card, or replayed history alone', () => {
    expect(effectPhase(idle)).toBe('off')
    expect(effectPhase({...idle, optimistic: {turnId: 't1', text: 'read', failed: false}})).toBe('off')
    expect(effectPhase(working)).toBe('off')
    expect(effectPhase({...working, pendingApproval}, null, 100)).toBe('off')
    expect(effectPhase({...working, entries: [{id: 'replayed', sequence: 1, turn_id: 't1',
      at: 0, kind: 'progress', tone: 'neutral', text: '已收到你的确认', replayed: true}]})).toBe('off')
  })

  it('starts at the click and persists after the approval card is removed', () => {
    expect(effectPhase({...working, pendingApproval}, confirmation, 100)).toBe('active')
    expect(effectPhase(working, confirmation)).toBe('active')
    expect(effectPhase({...working, connection: {...working.connection, online: false}},
      confirmation)).toBe('waiting')
    expect(effectPhase({...working, pendingApproval}, confirmation, 101)).toBe('off')
    expect(effectPhase({...working, pendingApproval: {...pendingApproval, approval_id: 'a2'}},
      confirmation, 100)).toBe('off')
  })

  it('stops on every terminal state and never authorizes another turn', () => {
    const states = new Set([
      ...Object.values(DEFAULT_CONFIGURATION.progress_states)
        .filter(state => state.terminal).map(state => state.panel_state),
      ...Object.values(DEFAULT_CONFIGURATION.terminal_panel_state_by_code),
    ])
    for (const state of states) {
      expect(effectPhase({...working, connection: {...working.connection, state}}, confirmation)).toBe('off')
    }
    expect(effectPhase({...working, closed: true}, confirmation)).toBe('off')
    expect(effectPhase({...working, connection: {...working.connection, busy: false}}, confirmation)).toBe('off')
    expect(effectPhase({...working, optimistic: {turnId: 't2', text: 'next', failed: false}},
      confirmation)).toBe('off')
    expect(effectPhase({...working, entries: [{id: 'next', sequence: 1, turn_id: 't2', at: 0,
      kind: 'goal', tone: 'neutral', text: 'next'}]}, confirmation)).toBe('off')
    expect(effectPhase({...working, pendingApproval: {...pendingApproval, turn_id: 't2'}},
      confirmation, 100)).toBe('off')
  })
})

function visible(element: Element, box: DOMRect): void {
  vi.spyOn(element, 'getBoundingClientRect').mockReturnValue(box)
  vi.spyOn(element, 'getClientRects').mockReturnValue([box] as unknown as DOMRectList)
}

describe('the cursor reads geometry only from a live action target', () => {
  it.each(['highlight', 'handoff_form', 'input', 'scroll', 'wait'])(
    'does not show a click pointer for %s', async action => {
      const point = vi.fn()
      const element = vi.fn()
      const execute = vi.fn().mockResolvedValue({success: true})
      const runtime = {panelHost: {setActivityPoint: point}, page: {element},
        _executePageCommand: execute}
      await EmbeddedWebRuntime.prototype._execute.call(runtime as never, {
        command_id: 'a1', action, args: {ref: 'e1', submit: {ref: 'e2'}}, expect_navigation: false,
      })
      expect(point).toHaveBeenCalledWith(null)
      expect(element).not.toHaveBeenCalled()
      expect(execute).toHaveBeenCalledTimes(1)
    },
  )
  it('publishes the dispatched target after the controller scrolls, without changing its receipt', async () => {
    const button = document.createElement('button')
    document.body.append(button)
    visible(button, new DOMRect(20, -200, 80, 40))
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: {
      submit() {}, cancel() {}, decide() {}, newConversation() {}, loadOlder() {},
    }})
    const result = {success: true, message: 'clicked'}
    const controller = {selectorMap: {}, clickElement: vi.fn(async () => {
      vi.mocked(button.getBoundingClientRect).mockReturnValue(new DOMRect(20, 30, 80, 40))
      return result
    })} as unknown as PageControllerApi
    const runtime = Object.create(EmbeddedWebRuntime.prototype) as EmbeddedWebRuntime
    runtime.controller = controller
    runtime.getElementByIndex = (_, index) => index === 3 ? button : null
    runtime.panelHost = host
    try {
      const promise = runtime._execute({command_id: 'a1', action: 'click', args: {ref: 'e3'},
        expect_navigation: false})
      expect(host.snapshot().activityPoint).toBeNull()
      await expect(promise).resolves.toBe(result)
      expect(controller.clickElement).toHaveBeenCalledExactlyOnceWith(3)
      expect(host.snapshot().activityPoint).toMatchObject({x: 60, y: 50})
      host.setConnection({busy: false})
      expect(host.snapshot().activityPoint).toBeNull()
    } finally { button.remove() }
  })

  it('uses the visible portion, and refuses hidden, detached and offscreen targets', () => {
    const button = document.createElement('button')
    document.body.append(button)
    visible(button, new DOMRect(-20, 40, 100, 20))
    expect(activityPoint(button)).toMatchObject({x: 40, y: 50})
    expect(document.activeElement).not.toBe(button)
    button.style.visibility = 'hidden'
    expect(activityPoint(button)).toBeNull()
    button.style.visibility = ''
    vi.mocked(button.getBoundingClientRect).mockReturnValue(new DOMRect(-200, 40, 100, 20))
    expect(activityPoint(button)).toBeNull()
    button.remove()
    expect(activityPoint(button)).toBeNull()
  })

  it('translates a same-origin frame target into viewport coordinates', () => {
    const frame = document.createElement('iframe')
    document.body.append(frame)
    Object.defineProperty(frame, 'offsetWidth', {value: 400})
    Object.defineProperty(frame, 'offsetHeight', {value: 300})
    visible(frame, new DOMRect(100, 60, 400, 300))
    const button = frame.contentDocument!.createElement('button')
    frame.contentDocument!.body.append(button)
    visible(button, new DOMRect(20, 30, 80, 40))
    expect(activityPoint(button)).toMatchObject({x: 160, y: 110})
    frame.remove()
  })

  it('executes once even if the optional cursor presentation fails', async () => {
    const execute = vi.fn().mockResolvedValue({success: true})
    const runtime = {panelHost: {setActivityPoint: () => { throw new Error('view failed') }},
      page: {element: () => null}, _executePageCommand: execute}
    const command = {command_id: 'a1', action: 'click', args: {ref: 'e1'}, expect_navigation: false}
    await expect(EmbeddedWebRuntime.prototype._execute.call(runtime as never, command))
      .resolves.toEqual({success: true})
    expect(execute).toHaveBeenCalledExactlyOnceWith(command)
  })
})
