import {act, createElement} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import type {PanelHost, PanelSnapshot} from '../src/bridge/host'
import {DEFAULT_CONFIGURATION, type ReconciliationStatus, type TimelineEntry} from '../src/contract/panel'
import {PanelWindow} from '../src/components/PanelWindow'
import {Thread} from '../src/components/Thread'

const labels = DEFAULT_CONFIGURATION.labels
const limits = DEFAULT_CONFIGURATION.limits

function makeSnapshot(patch: Partial<PanelSnapshot> = {}): PanelSnapshot {
  return {
    connection: {state: 'ready', busy: false, stopping: false, online: true},
    entries: [],
    pendingApproval: null,
    notice: null,
    hasOlder: false,
    loadingOlder: false,
    closed: false,
    optimistic: null,
    ...patch,
  }
}

function makeEntry(id: string, kind: TimelineEntry['kind'], text: string,
                   extra: Partial<TimelineEntry> = {}): TimelineEntry {
  return {
    id,
    sequence: Number(id.replace(/\D/g, '')) || 1,
    turn_id: 't1',
    at: 1757000000000,
    kind,
    tone: 'neutral',
    text,
    ...extra,
  }
}

function makeHost(snapshot: PanelSnapshot, extra: Partial<PanelHost> = {}): PanelHost {
  return {
    configuration: DEFAULT_CONFIGURATION,
    snapshot: () => snapshot,
    subscribe: () => () => undefined,
    submit: vi.fn(),
    retrySubmit: vi.fn(),
    cancel: vi.fn(),
    decide: vi.fn(),
    newConversation: vi.fn(),
    loadOlder: vi.fn(),
    dismissNotice: vi.fn(),
    reload: vi.fn(),
    ...extra,
  }
}

describe('Thread scroll detach/reattach (BUG-06)', () => {
  let host: HTMLDivElement
  let root: Root
  let resizeCallback: ResizeObserverCallback | null = null
  let originalResizeObserver: typeof ResizeObserver | undefined

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    resizeCallback = null
    originalResizeObserver = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(cb: ResizeObserverCallback) { resizeCallback = cb }
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    } as unknown as typeof ResizeObserver
  })

  afterEach(() => {
    act(() => { root.unmount() })
    host.remove()
    if (originalResizeObserver) globalThis.ResizeObserver = originalResizeObserver
    vi.restoreAllMocks()
  })

  it('detaches on small upward scroll (12px) and stays detached on growth until <= 4px or jumpLatest', () => {
    const snap = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '列出订单'),
        makeEntry('e2', 'result', '第一批结果', {panel_state: 'success'}),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: snap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined,
      }))
    })

    const scroller = host.querySelector<HTMLDivElement>('.woa-thread')!
    let scrollHeight = 1000
    const clientHeight = 400
    let scrollTop = 600
    Object.defineProperty(scroller, 'scrollHeight', {get: () => scrollHeight, configurable: true})
    Object.defineProperty(scroller, 'clientHeight', {get: () => clientHeight, configurable: true})
    Object.defineProperty(scroller, 'scrollTop', {
      get: () => scrollTop,
      set: (value: number) => { scrollTop = value },
      configurable: true,
    })
    scroller.scrollTo = ((opts?: ScrollToOptions | number) => {
      if (typeof opts === 'object' && opts.top !== undefined) scrollTop = opts.top
    }) as typeof scroller.scrollTo

    // First scroll settles at bottom (distance = 0, follow = true).
    act(() => { scroller.dispatchEvent(new Event('scroll')) })
    expect(scrollTop).toBe(600)

    // Small 12px upward wheel scroll (previously swallowed by NEAR_BOTTOM = 28).
    act(() => {
      scroller.dispatchEvent(new Event('wheel'))
      scrollTop = 588
      scroller.dispatchEvent(new Event('scroll'))
    })

    // Content grows by 80px -> must stay detached at 588, NOT snap to 1080!
    act(() => {
      scrollHeight = 1080
      resizeCallback?.([], {} as ResizeObserver)
    })
    expect(scrollTop).toBe(588)

    // Content grows further so distance > 160 -> jumpLatest appears, scrollTop stays 588.
    act(() => {
      scrollHeight = 1200
      resizeCallback?.([], {} as ResizeObserver)
    })
    expect(scrollTop).toBe(588)
    expect(host.querySelector('.woa-jump')).not.toBeNull()

    // Scroll down to 10px from bottom (> 4px): still does NOT re-engage follow.
    act(() => {
      scroller.dispatchEvent(new Event('wheel'))
      scrollTop = 790
      scroller.dispatchEvent(new Event('scroll'))
      scrollHeight = 1260
      resizeCallback?.([], {} as ResizeObserver)
    })
    expect(scrollTop).toBe(790)

    // Scroll to <= 4px from bottom (distance = 3px): re-engages follow!
    act(() => {
      scroller.dispatchEvent(new Event('wheel'))
      scrollTop = 857
      scroller.dispatchEvent(new Event('scroll'))
      scrollHeight = 1320
      resizeCallback?.([], {} as ResizeObserver)
    })
    expect(scrollTop).toBe(1320)

    // Tiny 2px upward scroll from bottom still detaches immediately.
    scrollTop = 920
    scrollHeight = 1320
    act(() => {
      scroller.dispatchEvent(new Event('scroll'))
      scroller.dispatchEvent(new Event('wheel'))
      scrollTop = 918
      scroller.dispatchEvent(new Event('scroll'))
      scrollHeight = 1520
      resizeCallback?.([], {} as ResizeObserver)
    })
    expect(scrollTop).toBe(918)
    const jump = host.querySelector<HTMLButtonElement>('.woa-jump')!
    expect(jump).not.toBeNull()

    // Clicking jumpLatest re-engages follow and scrolls to bottom.
    act(() => { jump.click() })
    expect(scrollTop).toBe(1520)
    expect(host.querySelector('.woa-jump')).toBeNull()
  })
})

describe('Turn retry (BUG-04)', () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    localStorage.clear()
  })

  afterEach(() => {
    act(() => { root.unmount() })
    host.remove()
    vi.restoreAllMocks()
  })

  it('renders retryTurn on retryable error outcome and invokes onRetryTurn(goal.text)', () => {
    const onRetryTurn = vi.fn()
    const snap = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '查询本月销售总额'),
        makeEntry('e2', 'result', '服务暂时不可用', {panel_state: 'error', retryable: true}),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: snap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined, onRetryTurn,
      }))
    })

    const btn = Array.from(host.querySelectorAll('button')).find(
      b => b.textContent?.includes(labels.retryTurn))
    expect(btn).toBeDefined()
    act(() => { btn!.click() })
    expect(onRetryTurn).toHaveBeenCalledExactlyOnceWith('查询本月销售总额')
  })

  it('renders retryTurn on turn-associated retryable failure entry when no outcome exists', () => {
    const onRetryTurn = vi.fn()
    const snap = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '导出客户列表'),
        makeEntry('e2', 'notice', '读取超时', {panel_state: 'error', retryable: true}),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: snap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined, onRetryTurn,
      }))
    })
    const btn = Array.from(host.querySelectorAll('button')).find(
      b => b.textContent?.includes(labels.retryTurn))
    expect(btn).toBeDefined()
    act(() => { btn!.click() })
    expect(onRetryTurn).toHaveBeenCalledExactlyOnceWith('导出客户列表')
  })

  it('does not render retryTurn for non-retryable errors or indeterminate outcomes', () => {
    const snap = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '修改客户', {turn_id: 't1'}),
        makeEntry('e2', 'result', '失败', {turn_id: 't1', panel_state: 'error', retryable: false}),
        makeEntry('e3', 'goal', '提交订单', {turn_id: 't2'}),
        makeEntry('e4', 'result', '结果不确定', {turn_id: 't2', panel_state: 'indeterminate', retryable: true}),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: snap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined, onRetryTurn: vi.fn(),
      }))
    })
    const retryButtons = Array.from(host.querySelectorAll('button')).filter(
      b => b.textContent?.includes(labels.retryTurn))
    expect(retryButtons).toHaveLength(0)
  })

  it('wires PanelWindow submit to Thread retryTurn', () => {
    const snap = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '重新查询库存'),
        makeEntry('e2', 'result', '请求超时', {panel_state: 'error', retryable: true}),
      ],
    })
    const panelHost = makeHost(snap)
    act(() => {
      root.render(createElement(PanelWindow, {host: panelHost, allowedOrigins: []}))
    })
    const launcher = host.querySelector<HTMLButtonElement>('.woa-launcher')
    if (launcher) act(() => { launcher.click() })
    const btn = Array.from(host.querySelectorAll('button')).find(
      b => b.textContent?.includes(labels.retryTurn))
    expect(btn).toBeDefined()
    act(() => { btn!.click() })
    expect(panelHost.submit).toHaveBeenCalledExactlyOnceWith('重新查询库存')
  })
})

describe('Indeterminate outcome reconciliation (UX-06)', () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    localStorage.clear()
  })

  afterEach(() => {
    act(() => { root.unmount() })
    host.remove()
    vi.restoreAllMocks()
  })

  it('calls onReconcile, disables while reconciling, and renders completed/failed/unknown statuses', async () => {
    let resolveCheck!: (status: ReconciliationStatus) => void
    const onReconcile = vi.fn(() => new Promise<ReconciliationStatus>(resolve => {
      resolveCheck = resolve
    }))
    const snap = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '保存销售订单'),
        makeEntry('e2', 'result', '保存结果未确认', {
          panel_state: 'indeterminate',
          proposal_digest: 'digest-123',
        }),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: snap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined,
        onRetryTurn: vi.fn(), onReconcile,
      }))
    })

    // Never offers retryTurn on indeterminate outcomes.
    expect(host.textContent).not.toContain(labels.retryTurn)
    const btn = host.querySelector<HTMLButtonElement>('.woa-outcome__reconcile-btn')!
    expect(btn.textContent).toContain(labels.reconcileStatus)
    expect(btn.disabled).toBe(false)

    act(() => { btn.click() })
    expect(onReconcile).toHaveBeenCalledExactlyOnceWith('t1', 'digest-123')
    expect(btn.disabled).toBe(true)
    expect(btn.textContent).toContain(labels.reconciling)

    await act(async () => {
      resolveCheck({outcome: 'unknown', settled: false, message: '上游接口暂未返回'})
    })
    const status = host.querySelector('.woa-outcome__reconcile')!
    expect(status.getAttribute('data-outcome')).toBe('unknown')
    expect(status.textContent).toContain(labels.reconcileUnknown)
    expect(status.textContent).toContain('上游接口暂未返回')
    // When unsettled/unknown, user can re-check again.
    expect(host.querySelector<HTMLButtonElement>('.woa-outcome__reconcile-btn')?.disabled).toBe(false)

    // Settled completed hides button and shows completed message.
    onReconcile.mockResolvedValueOnce({outcome: 'completed', settled: true, message: '订单 S0001 已确认保存'})
    await act(async () => {
      host.querySelector<HTMLButtonElement>('.woa-outcome__reconcile-btn')!.click()
    })
    expect(host.querySelector('.woa-outcome__reconcile')?.getAttribute('data-outcome')).toBe('completed')
    expect(host.textContent).toContain(labels.reconcileCompleted)
    expect(host.textContent).toContain('订单 S0001 已确认保存')
    expect(host.querySelector('.woa-outcome__reconcile-btn')).toBeNull()
  })

  it('renders reconcileNotApplied for failed reconciliation and omits button when host.reconcile is absent', async () => {
    const snapWithoutReconcile = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '提交'),
        makeEntry('e2', 'result', '未确认', {panel_state: 'indeterminate'}),
      ],
    })
    const hostWithout = makeHost(snapWithoutReconcile)
    act(() => {
      root.render(createElement(PanelWindow, {host: hostWithout, allowedOrigins: []}))
    })
    const launcher = host.querySelector<HTMLButtonElement>('.woa-launcher')
    if (launcher) act(() => { launcher.click() })
    expect(host.querySelector('.woa-outcome__reconcile-btn')).toBeNull()

    const reconcile = vi.fn().mockResolvedValue({outcome: 'failed', settled: true})
    const hostWith = makeHost(snapWithoutReconcile, {reconcile})
    act(() => {
      root.render(createElement(PanelWindow, {host: hostWith, allowedOrigins: []}))
    })
    const launcher2 = host.querySelector<HTMLButtonElement>('.woa-launcher')
    if (launcher2) act(() => { launcher2.click() })
    await act(async () => {
      host.querySelector<HTMLButtonElement>('.woa-outcome__reconcile-btn')!.click()
    })
    expect(reconcile).toHaveBeenCalledWith('t1', undefined)
    expect(host.textContent).toContain(labels.reconcileNotApplied)
  })
})

describe('Composer draft recovery on failed delivery (BUG-01)', () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    localStorage.clear()
  })

  afterEach(() => {
    act(() => { root.unmount() })
    host.remove()
    vi.restoreAllMocks()
  })

  it('restores failed optimistic text into the composer and clears unedited draft on retry', () => {
    let snap = makeSnapshot()
    let notify: ((snapshot: PanelSnapshot) => void) | null = null
    const retrySubmit = vi.fn()
    const submit = vi.fn(() => {
      snap = makeSnapshot({optimistic: {turnId: 't-fail', text: '请帮我查今天订单', failed: false}})
      notify?.(snap)
    })
    const panelHost = makeHost(snap, {
      snapshot: () => snap,
      subscribe: listener => {
        notify = listener
        return () => { if (notify === listener) notify = null }
      },
      submit,
      retrySubmit,
    })

    act(() => {
      root.render(createElement(PanelWindow, {host: panelHost, allowedOrigins: []}))
    })
    const launcher = host.querySelector<HTMLButtonElement>('.woa-launcher')
    if (launcher) act(() => { launcher.click() })

    const textarea = () => host.querySelector<HTMLTextAreaElement>('#woa-composer-input')!
    const setTextarea = (value: string) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!
      setter.call(textarea(), value)
      textarea().dispatchEvent(new Event('input', {bubbles: true}))
    }

    act(() => { setTextarea('请帮我查今天订单') })
    expect(textarea().value).toBe('请帮我查今天订单')
    act(() => {
      host.querySelector<HTMLFormElement>('form.woa-composer')!
        .dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}))
    })
    expect(submit).toHaveBeenCalledExactlyOnceWith('请帮我查今天订单')
    expect(textarea().value).toBe('')

    act(() => {
      snap = makeSnapshot({optimistic: {turnId: 't-fail', text: '请帮我查今天订单', failed: true}})
      notify?.(snap)
    })
    expect(textarea().value).toBe('请帮我查今天订单')

    act(() => {
      host.querySelector<HTMLButtonElement>('.woa-undelivered button')!.click()
    })
    expect(retrySubmit).toHaveBeenCalledTimes(1)
    expect(textarea().value).toBe('')

    // Edited draft is preserved when clicking the undelivered retry button.
    act(() => {
      snap = makeSnapshot({optimistic: {turnId: 't-fail-2', text: '旧问题', failed: true}})
      notify?.(snap)
    })
    expect(textarea().value).toBe('旧问题')
    act(() => { setTextarea('已修改的问题') })
    act(() => {
      host.querySelector<HTMLButtonElement>('.woa-undelivered button')!.click()
    })
    expect(retrySubmit).toHaveBeenCalledTimes(2)
    expect(textarea().value).toBe('已修改的问题')
  })
})
