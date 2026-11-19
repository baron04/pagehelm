import {act, createElement} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import type {PanelHost, PanelSnapshot} from '../src/bridge/host'
import {
  DEFAULT_CONFIGURATION,
  type ApprovalRequest,
  type TimelineEntry,
} from '../src/contract/panel'
import {ApprovalCard} from '../src/components/ApprovalCard'
import {PanelWindow} from '../src/components/PanelWindow'
import {Thread} from '../src/components/Thread'
import {useVisibilityGate} from '../src/state/visibility'
import type * as VisibilityModule from '../src/state/visibility'

vi.mock('../src/state/visibility', async importOriginal => {
  const actual = await importOriginal<typeof VisibilityModule>()
  return {
    ...actual,
    useVisibilityGate: vi.fn(actual.useVisibilityGate),
  }
})

const labels = DEFAULT_CONFIGURATION.labels
const limits = DEFAULT_CONFIGURATION.limits
const visibility = {min_visible_ratio: 0.75, min_visible_ms: 0}

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

function makeEntry(
  id: string,
  kind: TimelineEntry['kind'],
  text: string,
  extra: Partial<TimelineEntry> = {},
): TimelineEntry {
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

function setInputValue(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
  setter?.call(input, value)
  input.dispatchEvent(new Event('input', {bubbles: true}))
  input.dispatchEvent(new Event('change', {bubbles: true}))
}

function setSelectValue(select: HTMLSelectElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set
  setter?.call(select, value)
  select.dispatchEvent(new Event('change', {bubbles: true}))
}

function openPanel(host: HTMLElement): void {
  const launcher = host.querySelector<HTMLButtonElement>('.woa-launcher')
  if (launcher) act(() => { launcher.click() })
}

describe('ARC-04 progress steps inside WorkProcess', () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    host.remove()
    vi.restoreAllMocks()
  })

  it('renders read_page, settle, and awaiting_approval cleanly and hides transient steps after transition', () => {
    const busySnap = makeSnapshot({
      connection: {state: 'acting', busy: true, stopping: false, online: true},
      entries: [
        makeEntry('e1', 'goal', '列出所有客户'),
        makeEntry('e2', 'progress', '正在翻页查看（第 1 页）', {
          at: 1757000001000,
          step: {stage: 'read_page'},
        }),
        makeEntry('e3', 'progress', labels.progressSettling, {
          at: 1757000002000,
          transient: true,
          step: {stage: 'settle'},
        }),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: busySnap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined,
      }))
    })

    const live = host.querySelector<HTMLButtonElement>('.woa-work__live')!
    expect(live).not.toBeNull()
    expect(live.querySelector('.woa-work__now')?.textContent).toBe(labels.progressSettling)
    expect(live.getAttribute('data-waiting')).toBeNull()

    // Once a subsequent step (read_page page 2) lands, settle is hidden while read_page(1) remains.
    const nextStepSnap = makeSnapshot({
      connection: {state: 'acting', busy: true, stopping: false, online: true},
      entries: [
        ...busySnap.entries,
        makeEntry('e4', 'progress', '正在翻页查看（第 2 页）', {
          at: 1757000003000,
          step: {stage: 'read_page'},
        }),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: nextStepSnap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined,
      }))
    })
    expect(host.querySelector('.woa-work__now')?.textContent).toBe('正在翻页查看（第 2 页）')

    act(() => { host.querySelector<HTMLButtonElement>('.woa-work__live')!.click() })
    const expandedTexts = Array.from(host.querySelectorAll('.woa-step__text')).map(n => n.textContent)
    expect(expandedTexts).toEqual(['正在翻页查看（第 1 页）', '正在翻页查看（第 2 页）'])
    expect(expandedTexts).not.toContain(labels.progressSettling)

    // awaiting_approval marks live line as waiting with pulse, and hides once outcome lands.
    const waitingSnap = makeSnapshot({
      connection: {state: 'acting', busy: true, stopping: false, online: true},
      entries: [
        ...nextStepSnap.entries,
        makeEntry('e5', 'progress', labels.progressAwaitingApproval, {
          at: 1757000004000,
          tone: 'warning',
          transient: true,
          step: {stage: 'awaiting_approval'},
        }),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: waitingSnap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined,
      }))
    })
    const waitingLive = host.querySelector<HTMLButtonElement>('.woa-work__live')!
    expect(waitingLive.getAttribute('data-waiting')).toBe('true')
    expect(waitingLive.querySelector('.woa-pulse')).not.toBeNull()
    expect(waitingLive.querySelector('.woa-work__now')?.textContent).toBe(labels.progressAwaitingApproval)

    const settledSnap = makeSnapshot({
      connection: {state: 'ready', busy: false, stopping: false, online: true},
      entries: [
        ...waitingSnap.entries,
        makeEntry('e6', 'result', '已读取两页客户。', {at: 1757000005000, panel_state: 'success'}),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: settledSnap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined,
      }))
    })
    const summary = host.querySelector<HTMLButtonElement>('.woa-work__summary')!
    expect(summary.textContent).toContain('2 个步骤')
    // Already open from toggling live steps above; collapse and reopen via summary to verify toggle.
    expect(Array.from(host.querySelectorAll('.woa-step__text')).map(n => n.textContent))
      .toEqual(['正在翻页查看（第 1 页）', '正在翻页查看（第 2 页）'])
    act(() => { summary.click() })
    expect(host.querySelectorAll('.woa-step__text')).toHaveLength(0)
    act(() => { summary.click() })
    const finalSteps = Array.from(host.querySelectorAll('.woa-step__text')).map(n => n.textContent)
    expect(finalSteps).toEqual(['正在翻页查看（第 1 页）', '正在翻页查看（第 2 页）'])
  })
})

describe('ARC-06 structured tables inside Outcome', () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    host.remove()
    vi.restoreAllMocks()
  })

  it('renders StructuredTable via DataTable inside .woa-result with title, pagination, filter, and sort', () => {
    const rows = Array.from({length: 15}, (_, index) => [
      `[C-${String(index + 1).padStart(3, '0')}](http://localhost/customers/${index + 1})`,
      index === 0 ? '[外链](https://evil.example/x)' : `客户 ${index + 1}`,
      String((15 - index) * 100),
    ])
    const snap = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '列出前 15 位客户'),
        makeEntry('e2', 'result', '共找到 15 位客户。', {
          panel_state: 'success',
          table: {
            title: '客户列表',
            columns: ['编号', '名称', '金额'],
            rows,
            total_rows: 15,
          },
        }),
      ],
    })

    act(() => {
      root.render(createElement(Thread, {
        snapshot: snap, labels, limits, allowedOrigins: ['http://localhost'],
        onLoadOlder: () => undefined, onRetry: () => undefined,
      }))
    })

    const result = host.querySelector('.woa-result')!
    expect(result).not.toBeNull()
    expect(result.querySelector('.woa-result__table-title')?.textContent).toBe('客户列表')
    expect(result.querySelector('.woa-datatable')).not.toBeNull()
    expect(result.querySelectorAll('tbody tr')).toHaveLength(10)
    expect(result.querySelector('.woa-datatable__pageinfo')?.textContent).toBe('1 / 2')

    // Same-origin markdown inline link stays clickable; foreign link is inert.
    expect(result.querySelector('tbody a')?.getAttribute('href')).toBe('http://localhost/customers/1')
    expect(result.querySelector('tbody .woa-md__inert')?.textContent).toBe('外链')

    // Select 'all' inside .woa-result .woa-datatable__size select (runner.py contract).
    const sizeSelect = result.querySelector<HTMLSelectElement>('.woa-datatable__size select')!
    expect(Array.from(sizeSelect.options).map(o => o.value)).toEqual(['10', '25', '50', 'all'])
    act(() => { setSelectValue(sizeSelect, 'all') })
    expect(result.querySelectorAll('tbody tr')).toHaveLength(15)

    // Numeric sorting ascending on 金额 column.
    const sortButtons = result.querySelectorAll<HTMLButtonElement>('.woa-datatable__sort')
    act(() => { sortButtons[2]!.click() })
    expect(result.querySelector('tbody tr td:last-child')?.textContent).toBe('100')

    // Filtering narrows rows without redundant export buttons.
    const filter = result.querySelector<HTMLInputElement>('.woa-datatable__filter input')!
    act(() => { setInputValue(filter, 'C-002') })
    expect(result.querySelectorAll('tbody tr')).toHaveLength(1)
    expect(result.querySelector('.woa-datatable__actions')).toBeNull()
  })

  it('renders StructuredTable cleanly without title and even when result text is empty', () => {
    const snap = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '查表'),
        makeEntry('e2', 'result', '', {
          panel_state: 'success',
          table: {columns: ['列A'], rows: [['值1']], total_rows: 1},
        }),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: snap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined,
      }))
    })
    expect(host.querySelector('.woa-result .woa-datatable')).not.toBeNull()
    expect(host.querySelector('.woa-result__table-title')).toBeNull()
    expect(host.querySelector('.woa-result__table select')).toBeNull()
  })
})

describe('UX-02 clarification option pills', () => {
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

  it('renders clickable .woa-input-option pills on latest needs_input turn and submits prompt ?? label', () => {
    const snap = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '打开客户', {turn_id: 't1'}),
        makeEntry('e2', 'result', '请问你要打开哪一位客户？', {
          turn_id: 't1',
          panel_state: 'needs_input',
          input_options: [
            {id: 'opt-1', label: 'Acme 公司', prompt: '选择 Acme 公司 (ID: 101)'},
            {id: 'opt-2', label: 'Beta 科技'},
          ],
        }),
      ],
    })
    const panelHost = makeHost(snap)
    act(() => {
      root.render(createElement(PanelWindow, {host: panelHost, allowedOrigins: []}))
    })
    openPanel(host)

    const pills = Array.from(host.querySelectorAll<HTMLButtonElement>('.woa-result .woa-input-option'))
    expect(pills).toHaveLength(2)
    expect(pills.map(p => p.textContent)).toEqual(['Acme 公司', 'Beta 科技'])
    expect(pills.every(p => !p.disabled)).toBe(true)

    act(() => { pills[0]!.click() })
    expect(panelHost.submit).toHaveBeenLastCalledWith('选择 Acme 公司 (ID: 101)')

    act(() => { pills[1]!.click() })
    expect(panelHost.submit).toHaveBeenLastCalledWith('Beta 科技')
  })

  it('disables pills when busy, closed, or not the latest turn', () => {
    const baseOptions = [{id: 'o1', label: '选项 A', prompt: '选 A'}]
    const busySnap = makeSnapshot({
      connection: {state: 'acting', busy: true, stopping: false, online: true},
      entries: [
        makeEntry('e1', 'goal', '问题1', {turn_id: 't1'}),
        makeEntry('e2', 'result', '请选择', {
          turn_id: 't1', panel_state: 'needs_input', input_options: baseOptions,
        }),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: busySnap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined, onSelectOption: vi.fn(),
      }))
    })
    expect(host.querySelector<HTMLButtonElement>('.woa-input-option')!.disabled).toBe(true)

    const closedSnap = makeSnapshot({
      closed: true,
      entries: busySnap.entries,
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: closedSnap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined, onSelectOption: vi.fn(),
      }))
    })
    expect(host.querySelector<HTMLButtonElement>('.woa-input-option')!.disabled).toBe(true)

    const staleTurnSnap = makeSnapshot({
      entries: [
        ...busySnap.entries,
        makeEntry('e3', 'goal', '新问题', {turn_id: 't2'}),
        makeEntry('e4', 'result', '已完成', {turn_id: 't2', panel_state: 'success'}),
      ],
    })
    act(() => {
      root.render(createElement(Thread, {
        snapshot: staleTurnSnap, labels, limits, allowedOrigins: [],
        onLoadOlder: () => undefined, onRetry: () => undefined, onSelectOption: vi.fn(),
      }))
    })
    expect(host.querySelector<HTMLButtonElement>('.woa-input-option')!.disabled).toBe(true)
  })
})

describe('UX-03 + UX-05 handoff summary card & prefill approval choice', () => {
  let host: HTMLDivElement
  let root: Root
  let origScrollIntoView: typeof Element.prototype.scrollIntoView | undefined

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    localStorage.clear()
    origScrollIntoView = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = vi.fn()
  })

  afterEach(() => {
    act(() => { root.unmount() })
    host.remove()
    if (origScrollIntoView) {
      Element.prototype.scrollIntoView = origScrollIntoView
    } else {
      delete (Element.prototype as Partial<Element>).scrollIntoView
    }
    vi.restoreAllMocks()
  })

  it('renders structured handoff card, locates restricted controls, and handles undoFormFill transitions', async () => {
    const undoFormFill = vi.fn()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true)
    const focusHandoffControl = vi.fn().mockResolvedValue(true)

    const snap = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '预填新建用户表单'),
        makeEntry('e2', 'result', '已帮你填入基本信息，请补充密码并自行提交。', {
          panel_state: 'success',
          handoff: {
            completed_fields: [{name: '用户名', value: 'alice'}, {name: '密码'}],
            pending_fields: [{name: '登录密码'}, {name: '头像附件'}],
            restricted_controls: [{label: '登录密码框', kind: 'password', ref: 'e9'}],
            can_undo: true,
          },
        }),
      ],
    })
    const panelHost = makeHost(snap, {undoFormFill, focusHandoffControl})
    act(() => {
      root.render(createElement(PanelWindow, {host: panelHost, allowedOrigins: []}))
    })
    openPanel(host)

    const card = host.querySelector('[data-testid="woa-handoff"]')!
    expect(card).not.toBeNull()
    expect(card.textContent).toContain(labels.handoffTitle)
    expect(card.textContent).toContain(labels.handoffCompletedFields)
    expect(card.textContent).toContain('用户名')
    expect(card.textContent).toContain('alice')
    expect(card.textContent).toContain(labels.handoffPendingFields)
    expect(card.textContent).toContain('头像附件')

    const locateBtn = card.querySelector<HTMLButtonElement>('.woa-handoff__locate')!
    expect(locateBtn.textContent).toContain(labels.handoffLocateControl)
    act(() => { locateBtn.click() })
    expect(focusHandoffControl).toHaveBeenCalledExactlyOnceWith({
      label: '登录密码框', kind: 'password', ref: 'e9',
    })

    const undoBtn = card.querySelector<HTMLButtonElement>('.woa-handoff__undo')!
    expect(undoBtn.textContent).toContain(labels.handoffUndoFill)
    expect(undoBtn.disabled).toBe(false)

    // First click returns false -> stays retryable.
    await act(async () => {
      undoBtn.click()
      await Promise.resolve()
    })
    expect(undoFormFill).toHaveBeenCalledWith('t1')
    expect(undoBtn.disabled).toBe(false)
    expect(undoBtn.textContent).toContain(labels.handoffUndoFill)

    // Second click returns true -> disabled confirmed state.
    await act(async () => {
      undoBtn.click()
      await Promise.resolve()
    })
    expect(undoBtn.disabled).toBe(true)
    expect(undoBtn.getAttribute('data-undone')).toBe('true')
    expect(undoBtn.textContent).toContain(labels.handoffUndoDone)
  })

  it('omits undo button when host.undoFormFill is absent', () => {
    const snap = makeSnapshot({
      entries: [
        makeEntry('e1', 'goal', '预填'),
        makeEntry('e2', 'result', '已填', {
          panel_state: 'success',
          handoff: {
            completed_fields: [{name: '名称', value: 'Acme'}],
            pending_fields: [],
            can_undo: true,
          },
        }),
      ],
    })
    const panelHost = makeHost(snap)
    act(() => {
      root.render(createElement(PanelWindow, {host: panelHost, allowedOrigins: []}))
    })
    openPanel(host)
    expect(host.querySelector('.woa-handoff')).not.toBeNull()
    expect(host.querySelector('.woa-handoff__undo')).toBeNull()
  })

  it('offers prefill choice on generic write approval cards with offer_prefill === true', () => {
    const onDecide = vi.fn()
    const onRefused = vi.fn()
    const approval: ApprovalRequest = {
      schema_version: 1,
      approval_id: 'app-prefill-1',
      effect: '创建 1 条客户记录',
      targets: ['表单：新建客户'],
      expires_at: Date.now() + 120_000,
      principal_digest: '',
      turn_id: 't1',
      operation_id: 'op-1',
      kind: 'write',
      offer_prefill: true,
    }

    // With real visibility gate in jsdom (isTrusted=false), prefill is gated identically to approve.
    act(() => {
      root.render(createElement(ApprovalCard, {
        approval, labels, limits, visibility, busy: false, onDecide, onRefused,
      }))
    })
    const gatedPrefill = host.querySelector<HTMLButtonElement>('[data-testid="woa-prefill"]')!
    expect(gatedPrefill.textContent).toBe(labels.prefillChoice)
    expect(host.querySelector<HTMLButtonElement>('[data-testid="woa-approve"]')!.textContent)
      .toBe(labels.saveChoice)
    act(() => { gatedPrefill.click() })
    expect(onRefused).toHaveBeenCalledWith('untrusted_gesture')
    expect(onDecide).not.toHaveBeenCalled()

    // Once visibility gate passes, clicking prefill dispatches 'prefill' and approve dispatches 'approved'.
    act(() => { root.unmount() })
    root = createRoot(host)
    vi.mocked(useVisibilityGate).mockReturnValue({
      attach: () => undefined,
      refusalFor: () => '',
    })
    act(() => {
      root.render(createElement(ApprovalCard, {
        approval: {...approval, approval_id: 'app-prefill-2'},
        labels, limits, visibility, busy: false, onDecide, onRefused,
      }))
    })
    act(() => { host.querySelector<HTMLButtonElement>('[data-testid="woa-prefill"]')!.click() })
    expect(onDecide).toHaveBeenLastCalledWith('prefill')
    act(() => { host.querySelector<HTMLButtonElement>('[data-testid="woa-approve"]')!.click() })
    expect(onDecide).toHaveBeenLastCalledWith('approved')

    // Operate approvals never offer prefill even if offer_prefill is set.
    act(() => {
      root.render(createElement(ApprovalCard, {
        approval: {...approval, approval_id: 'app-op-1', kind: 'operate'},
        labels, limits, visibility, busy: false, onDecide, onRefused,
      }))
    })
    expect(host.querySelector('[data-testid="woa-prefill"]')).toBeNull()
  })

  it('forwards visibility-gate refusal codes from PanelWindow to host.reportApprovalRefused without deciding', () => {
    vi.mocked(useVisibilityGate).mockReturnValue({
      attach: () => undefined,
      refusalFor: () => 'insufficient_coverage',
    })
    const reportApprovalRefused = vi.fn()
    const decide = vi.fn()
    const snap = makeSnapshot({
      pendingApproval: {
        schema_version: 1,
        approval_id: 'ap-refuse-1',
        effect: '更新联系人手机',
        targets: ['联系人 林舟'],
        expires_at: Date.now() + 60_000,
        principal_digest: '',
        turn_id: 't-refuse-1',
        operation_id: 'op-refuse-1',
        kind: 'write',
      },
    })
    const panelHost = makeHost(snap, {decide, reportApprovalRefused})
    act(() => {
      root.render(createElement(PanelWindow, {host: panelHost, allowedOrigins: []}))
    })
    openPanel(host)
    const approveBtn = host.querySelector<HTMLButtonElement>('[data-testid="woa-approve"]')!
    expect(approveBtn).not.toBeNull()
    act(() => { approveBtn.click() })
    expect(reportApprovalRefused).toHaveBeenCalledWith('ap-refuse-1', 'insufficient_coverage')
    expect(decide).not.toHaveBeenCalled()
    expect(host.textContent).toContain(labels.approvalObscured)
  })

  it('clears restored composer draft before calling host.reload on failed optimistic retry', () => {
    const reload = vi.fn()
    const snap = makeSnapshot({
      optimistic: {turnId: 't-fail', text: '重发这一句', failed: true},
      notice: {code: 'submit_failed', retryable: true, turnId: 't-fail'},
    })
    const panelHost = makeHost(snap, {reload})
    act(() => {
      root.render(createElement(PanelWindow, {host: panelHost, allowedOrigins: []}))
    })
    openPanel(host)
    const composer = host.querySelector<HTMLTextAreaElement>('textarea')!
    expect(composer.value).toBe('重发这一句')
    const retryBtn = host.querySelector<HTMLButtonElement>('.woa-inline-error__retry')!
    expect(retryBtn).not.toBeNull()
    act(() => { retryBtn.click() })
    expect(reload).toHaveBeenCalledOnce()
    expect(composer.value).toBe('')
  })
})
