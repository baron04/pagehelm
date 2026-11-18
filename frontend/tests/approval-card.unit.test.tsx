import {act, createElement} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {ApprovalCard} from '../src/components/ApprovalCard'
import {DEFAULT_CONFIGURATION, type ApprovalRequest} from '../src/contract/panel'

const labels = DEFAULT_CONFIGURATION.labels
const limits = DEFAULT_CONFIGURATION.limits
const visibility = {min_visible_ratio: 0.75, min_visible_ms: 250}

function makeApproval(patch: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return {
    schema_version: 1,
    approval_id: 'approval-ux04-1',
    effect: '更新 1 位客户的资料',
    targets: ['客户：Acme Corporation', '编号：C-0042'],
    expires_at: Date.now() + 120_000,
    principal_digest: '',
    turn_id: 'turn-1',
    operation_id: 'op-1',
    ...patch,
  }
}

function pressKey(target: EventTarget, key: string, shiftKey = false): KeyboardEvent {
  const event = new KeyboardEvent('keydown', {key, shiftKey, bubbles: true, cancelable: true})
  act(() => {
    target.dispatchEvent(event)
  })
  return event
}

describe('ApprovalCard (UX-04)', () => {
  let host: HTMLDivElement
  let root: Root
  let origScrollIntoView: typeof Element.prototype.scrollIntoView | undefined

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
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

  it('renders change diff spans preserving exact text and splits only on first arrow', () => {
    const approval = makeApproval({
      details: [
        {kind: 'change', label: '联系电话', text: '010-1111 → 010-2222'},
        {kind: 'change', label: '明细调整', text: '数量 1 → 2，单价 10 → 12'},
        {kind: 'change', label: '补充说明', text: '无箭头变更说明'},
        {kind: 'body', label: '公告正文', text: '第一行正文\n第二行正文'},
        {kind: 'derived', label: '派生结果', text: '含税总额 1,130.00'},
      ],
    })
    act(() => {
      root.render(createElement(ApprovalCard, {
        approval,
        labels,
        limits,
        visibility,
        busy: false,
        onDecide: vi.fn(),
        onRefused: vi.fn(),
      }))
    })

    const card = host.querySelector('.woa-approval')!
    expect(card.textContent).toContain('更新 1 位客户的资料')
    expect(card.textContent).toContain('客户：Acme Corporation')
    expect(card.querySelectorAll('.woa-approval__preserved-toggle')).toHaveLength(0)

    const diffs = card.querySelectorAll('.woa-approval__diff')
    expect(diffs).toHaveLength(2)

    expect(diffs[0]!.textContent).toBe('010-1111 → 010-2222')
    expect(diffs[0]!.querySelector('.woa-approval__diff-old')!.textContent).toBe('010-1111')
    expect(diffs[0]!.querySelector('.woa-approval__diff-arrow')!.textContent).toBe(' → ')
    expect(diffs[0]!.querySelector('.woa-approval__diff-new')!.textContent).toBe('010-2222')

    expect(diffs[1]!.textContent).toBe('数量 1 → 2，单价 10 → 12')
    expect(diffs[1]!.querySelector('.woa-approval__diff-old')!.textContent).toBe('数量 1')
    expect(diffs[1]!.querySelector('.woa-approval__diff-new')!.textContent).toBe('2，单价 10 → 12')

    const pres = [...card.querySelectorAll('.woa-approval__detail-value pre')].map(n => n.textContent)
    expect(pres).toEqual(['无箭头变更说明', '第一行正文\n第二行正文', '含税总额 1,130.00'])
  })

  it('collapses preserved details by default and toggles them cleanly', () => {
    const approval = makeApproval({
      details: [
        {kind: 'change', label: '客户等级', text: '普通 → VIP'},
        {kind: 'preserved', label: '所属销售', text: '张敏'},
        {kind: 'preserved', label: '税号', text: '91110105MA0001234X'},
      ],
    })
    act(() => {
      root.render(createElement(ApprovalCard, {
        approval,
        labels,
        limits,
        visibility,
        busy: false,
        onDecide: vi.fn(),
        onRefused: vi.fn(),
      }))
    })

    const card = host.querySelector('.woa-approval')!
    const toggle = card.querySelector<HTMLButtonElement>('[data-testid="woa-preserved-toggle"]')!
    expect(toggle).not.toBeNull()
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(toggle.textContent).toBe(`${labels.approvalShowPreserved}（2）`)
    const controlsId = toggle.getAttribute('aria-controls')!
    expect(controlsId).toBeTruthy()
    expect(card.querySelector(`#${controlsId}`)).toBeNull()
    expect(card.textContent).not.toContain('张敏')
    expect(card.textContent).toContain('普通 → VIP')

    act(() => { toggle.click() })
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(toggle.textContent).toBe(labels.approvalHidePreserved)
    const preservedList = card.querySelector(`#${controlsId}`)!
    expect(preservedList).not.toBeNull()
    expect(preservedList.textContent).toContain('所属销售')
    expect(preservedList.textContent).toContain('张敏')
    expect(preservedList.textContent).toContain('91110105MA0001234X')

    // Re-rendering with a new approval_id resets the disclosure state to collapsed.
    act(() => {
      root.render(createElement(ApprovalCard, {
        approval: {...approval, approval_id: 'approval-ux04-2'},
        labels,
        limits,
        visibility,
        busy: false,
        onDecide: vi.fn(),
        onRefused: vi.fn(),
      }))
    })
    const nextToggle = card.querySelector<HTMLButtonElement>('[data-testid="woa-preserved-toggle"]')!
    expect(nextToggle.getAttribute('aria-expanded')).toBe('false')

    act(() => { nextToggle.click() })
    expect(nextToggle.getAttribute('aria-expanded')).toBe('true')
    act(() => { nextToggle.click() })
    expect(nextToggle.getAttribute('aria-expanded')).toBe('false')
  })

  it('cycles Tab and Shift+Tab across all enabled controls inside the card without leaking', () => {
    const onDecide = vi.fn()
    const approval = makeApproval({
      offer_prefill: true,
      details: [
        {kind: 'change', label: '联系电话', text: '010-1111 → 010-2222'},
        {kind: 'preserved', label: '所属销售', text: '张敏'},
      ],
    })
    act(() => {
      root.render(createElement(ApprovalCard, {
        approval,
        labels,
        limits,
        visibility,
        busy: false,
        onDecide,
        onRefused: vi.fn(),
      }))
    })

    const card = host.querySelector<HTMLElement>('.woa-approval')!
    const toggle = host.querySelector<HTMLButtonElement>('[data-testid="woa-preserved-toggle"]')!
    const deny = host.querySelector<HTMLButtonElement>('[data-testid="woa-deny"]')!
    const prefill = host.querySelector<HTMLButtonElement>('[data-testid="woa-prefill"]')!
    const approve = host.querySelector<HTMLButtonElement>('[data-testid="woa-approve"]')!

    expect(document.activeElement).toBe(card)

    const e1 = pressKey(document.activeElement!, 'Tab')
    expect(e1.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(toggle)

    pressKey(document.activeElement!, 'Tab')
    expect(document.activeElement).toBe(deny)

    pressKey(document.activeElement!, 'Tab')
    expect(document.activeElement).toBe(prefill)

    pressKey(document.activeElement!, 'Tab')
    expect(document.activeElement).toBe(approve)

    // Wraps cleanly back to toggle
    pressKey(document.activeElement!, 'Tab')
    expect(document.activeElement).toBe(toggle)

    // Shift+Tab cycles cleanly in reverse
    pressKey(document.activeElement!, 'Tab', true)
    expect(document.activeElement).toBe(approve)

    pressKey(document.activeElement!, 'Tab', true)
    expect(document.activeElement).toBe(prefill)

    pressKey(document.activeElement!, 'Tab', true)
    expect(document.activeElement).toBe(deny)

    pressKey(document.activeElement!, 'Tab', true)
    expect(document.activeElement).toBe(toggle)

    // Busy disables action buttons, leaving only toggle focusable inside the card
    act(() => {
      root.render(createElement(ApprovalCard, {
        approval,
        labels,
        limits,
        visibility,
        busy: true,
        onDecide,
        onRefused: vi.fn(),
      }))
    })
    card.focus()
    pressKey(card, 'Tab')
    expect(document.activeElement).toBe(toggle)
    pressKey(toggle, 'Tab')
    expect(document.activeElement).toBe(toggle)

    pressKey(toggle, 'Escape')
    expect(onDecide).toHaveBeenCalledWith('denied')
  })
})
