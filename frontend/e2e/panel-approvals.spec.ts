/**
 * Chromium checks for approval cards, visibility dwell, operate cards and form prefill handoff.
 */
import presentation from '../src/presentation.json' with {type: 'json'}
import {PENDING, entry, expect, labels, notices, snapshot, test} from './harness'

test.describe('approval triggers and scrolling', () => {
  test('an explicitly offered form handoff requires the same visible card gate as saving', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval({offer_prefill: true})
    await expect(panel.page.getByTestId('woa-prefill')).toHaveText(labels.prefillChoice)
    await expect(panel.page.getByTestId('woa-approve')).toHaveText(labels.saveChoice)
    await expect(panel.page.getByRole('alertdialog')).toBeFocused()
    await panel.page.waitForTimeout(presentation.approval_visibility.min_visible_ms + 250)
    await panel.page.getByTestId('woa-prefill').click()
    expect(await panel.calls()).toContainEqual(['decide', 'a-1', 'prefill'])
    expect(panel.errors).toEqual([])
  })
  test('a multi-field write card fills its scrollport without a tail gap blocking approval', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval({details: Array.from({length: 12}, (_, i) => ({
      kind: 'change' as const, label: `合成字段 ${i + 1}`, text: `旧值 ${i + 1} → 新值 ${i + 1}`,
    }))})
    const card = panel.page.locator('.woa-approval')
    await expect(card).toHaveAttribute('data-approval-id', 'a-1')
    const button = card.locator('[data-testid="woa-approve"]')
    await button.scrollIntoViewIfNeeded()
    await panel.page.waitForTimeout(600)
    await button.click()
    expect(await panel.calls()).toContainEqual(['decide', 'a-1', 'approved'])
    expect(panel.errors).toEqual([])
  })
  test('a long write card exposes the whole body and remains approvable after scrolling', async ({open}) => {
    const panel = await open(snapshot())
    const body = Array.from({length: 180}, (_, i) => `合成公告第 ${i + 1} 行：完整正文，不得截断。`).join('\n') + '\n正文结尾校验'
    await panel.raiseApproval({details: [{kind: 'body', label: '公告正文', text: body},
      {kind: 'target_rule', label: '接收规则', text: '按执行时的部门下属规则确定接收人'}]})
    const card = panel.page.locator('.woa-approval')
    await expect(card.locator('pre').first()).toHaveText(body)
    const button = card.locator('[data-testid="woa-approve"]')
    await button.scrollIntoViewIfNeeded()
    await panel.page.waitForTimeout(600)
    await button.click()
    expect(await panel.calls()).toContainEqual(['decide', 'a-1', 'approved'])
    expect(panel.errors).toEqual([])
  })

  test('a confirmation opens the panel and marks the launcher when put away', async ({open}) => {
    // An unanswered confirmation is not something to leave behind a badge.
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').waitFor()
    await panel.raiseApproval()
    await panel.page.locator('.woa-window').waitFor()
    // Put it away deliberately and the launcher says there is still something waiting.
    await panel.page.getByRole('button', {name: labels.collapse}).click()
    const launcher = panel.page.locator('.woa-launcher')
    await launcher.waitFor()
    await expect(launcher).toHaveAttribute('data-state', 'awaiting')
    await expect(launcher.locator('.woa-launcher__nub')).toHaveText('1')
    expect(panel.errors).toEqual([])
  })
})

test.describe('a confirmation', () => {
  test('is keyboard operable, and escape cancels', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval()
    await expect(panel.page.locator('.woa-approval')).toContainText('S00002')
    // A change is guarded: the write card's title carries the shield.
    await expect(panel.page.locator('.woa-approval h2 svg.lucide-shield-alert')).toHaveCount(1)
    await expect(panel.page.getByRole('alertdialog')).toBeFocused()
    await panel.page.keyboard.press('Tab')
    expect((await panel.active()).text).toBe(labels.deny)
    // Tab is trapped between the two answers; nothing else is reachable from the card.
    await panel.page.keyboard.press('Tab')
    expect((await panel.active()).testid).toBe('woa-approve')
    await panel.page.keyboard.press('Escape')
    const calls = await panel.calls()
    expect(calls.map(call => call[0])).toEqual(['decide'])
    expect(calls[0]?.[2]).toBe('denied')
    expect(panel.errors).toEqual([])
  })

  test('leave to work the page can be given or declined, and locks an empty composer', async ({open}) => {
    // With no earlier draft, the card waits for an answer: 帮我操作, or 取消.
    const panel = await open(snapshot())
    await panel.raiseApproval({kind: 'operate', effect: '在页面上帮你完成这件事',
                               targets: ['会打开页面、点击、填写和选择']})
    const card = panel.page.locator('.woa-approval')
    await expect(card).toHaveAttribute('data-kind', 'operate')
    await expect(card).toContainText(labels.operateTitle)
    // Working the page is a route through it, not a guarded change.
    await expect(card.locator('h2 svg.lucide-route')).toHaveCount(1)
    await expect(card.locator('h2 svg.lucide-shield-alert')).toHaveCount(0)
    // Its terms show: it can be stopped, a change is still asked about, unsaved work may go.
    await expect(card).toContainText(labels.operateHint)
    await expect(card).toContainText('会打开页面、点击、填写和选择')
    await expect(panel.page.getByTestId('woa-deny')).toHaveText(labels.deny)
    await expect(panel.page.getByTestId('woa-approve')).toHaveText(labels.operateConfirm)
    await expect(panel.page.locator('#woa-composer-input')).toBeDisabled()
    // The card announces itself without putting a focus ring on either answer.
    await expect(card).toBeFocused()
    await expect(panel.page.getByTestId('woa-deny')).not.toBeFocused()
    expect(await panel.page.getByTestId('woa-deny').evaluate(
      node => getComputedStyle(node).outlineStyle)).toBe('none')
    await panel.page.keyboard.press('Tab')
    await expect(panel.page.getByTestId('woa-deny')).toBeFocused()
    expect(await panel.page.getByTestId('woa-deny').evaluate(
      node => getComputedStyle(node).outlineStyle)).toBe('solid')
    await panel.page.keyboard.press('Escape')
    const calls = await panel.calls()
    expect(calls.map(call => call[0])).toEqual(['decide'])
    expect(calls[0]?.[2]).toBe('denied')
    expect(panel.errors).toEqual([])
  })

  test('a draft that predates the card can replace the waiting turn', async ({open}) => {
    // Sending the preserved draft asks the Runtime to cancel then replace the turn.
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    const composer = panel.page.locator('#woa-composer-input')
    await composer.fill('改问：Azure Interior 的电话是多少？')
    await panel.set({connection: {state: 'acting', busy: true, stopping: false, online: true}})
    await panel.raiseApproval({kind: 'operate', effect: '在页面上帮你完成这件事',
                               targets: ['会打开页面、点击、填写和选择']})

    await expect(composer).toBeEnabled()
    await composer.press('Enter')
    expect(await panel.calls()).toEqual([['submit', '改问：Azure Interior 的电话是多少？']])
    expect(panel.errors).toEqual([])
  })

  test('stays usable at 320 by 480', async ({open}) => {
    const panel = await open(snapshot(), {width: 320, height: 480})
    await panel.raiseApproval({effect: '新建客户 Nordwind Handelsgesellschaft mbH & Co. KG',
                               targets: ['客户：Nordwind Handelsgesellschaft mbH & Co. KG']})
    const window = await panel.page.locator('.woa-window').boundingBox()
    expect(window).not.toBeNull()
    // The window never leaves the viewport, however small the viewport is.
    expect(window!.x).toBeGreaterThanOrEqual(0)
    expect(window!.y).toBeGreaterThanOrEqual(0)
    expect(window!.x + window!.width).toBeLessThanOrEqual(321)
    expect(window!.y + window!.height).toBeLessThanOrEqual(481)
    for (const name of [labels.deny, labels.approve]) {
      const button = panel.page.getByRole('button', {name})
      await expect(button, name).toBeVisible()
      expect((await button.boundingBox())!.height, name).toBeGreaterThanOrEqual(28)
    }
    // Under 360px the two answers stack, and the safe one sits nearest the thumb.
    const deny = await panel.page.getByRole('button', {name: labels.deny}).boundingBox()
    const approve = await panel.page.getByTestId('woa-approve').boundingBox()
    expect(deny!.y).toBeGreaterThan(approve!.y)
    expect(panel.errors).toEqual([])
  })

  test('an expired one explains the timeout and frees the composer', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval()
    const composer = panel.page.locator('textarea')
    await expect(composer, 'a live confirmation blocks the composer').toBeDisabled()

    await panel.set({pendingApproval: {...PENDING, expires_at: 0}})
    await panel.page.waitForTimeout(1200)

    await expect(panel.page.locator('.woa-approval')).toHaveCount(0)
    await expect(panel.page.locator('.woa-inline-error')).toContainText(notices.approval_expired!)
    await expect(composer, 'the person can type again').toBeEnabled()
  })

  test('a turn a card timed out on retains the server explanation in history', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '打开 Azure Interior 的客户页面'),
      entry('e2', 'progress', '正在思考'),
      entry('e3', 'progress', '已读取 1 条：客户 Azure Interior'),
      entry('e4', 'approval', '需要确认后才能操作页面', {tone: 'warning'}),
      entry('e5', 'result', notices.approval_expired!, {panel_state: 'cancelled'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-turn').waitFor()
    await expect(panel.page.locator('.woa-turn .woa-work__summary svg.lucide-circle-slash'))
      .toHaveCount(1)
    await expect(panel.page.locator('.woa-turn')).toContainText(notices.approval_expired!)
    await expect(panel.page.locator('.woa-turn .woa-answer-actions')).toHaveCount(1)
    expect(panel.errors).toEqual([])
  })

  test('the time left shows only once it is short', async ({open}) => {
    // A clock ticking from the first second pressed a person who had minutes. It shows for
    // the last one.
    const panel = await open(snapshot())
    await panel.raiseApproval()
    await expect(panel.page.locator('.woa-approval__time')).toHaveCount(0)
    await panel.set({pendingApproval: {...PENDING, expires_at: Date.now() + 90_000}})
    await panel.page.waitForTimeout(1200)
    await expect(panel.page.locator('.woa-approval__time')).toHaveCount(0)
    await panel.set({pendingApproval: {...PENDING, expires_at: Date.now() + 50_000}})
    await expect(panel.page.locator('.woa-approval__time')).toContainText(labels.approvalValidFor)
    await expect(panel.page.locator('.woa-approval__time')).toHaveAttribute('data-urgent', 'true')
    expect(panel.errors).toEqual([])
  })

  test('one that is not visible refuses and says why', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval({effect: '为 S00007 开票', targets: ['销售订单：S00007']})
    // Clicking before the card has been visible for its dwell is refused; the reason is
    // shown rather than the press silently doing nothing.
    await panel.page.getByTestId('woa-approve').dispatchEvent('click')
    await panel.page.locator('.woa-approval__blocked').waitFor({timeout: 3000})
    await expect(panel.page.locator('.woa-approval__blocked')).toContainText(labels.approvalObscured)
    expect(await panel.calls()).toEqual([])
    expect(panel.errors).toEqual([])
  })

  test('the form handoff choice also refuses before the card is visible', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval({offer_prefill: true})
    await panel.page.getByTestId('woa-prefill').dispatchEvent('click')
    await panel.page.locator('.woa-approval__blocked').waitFor({timeout: 3000})
    expect(await panel.calls()).toEqual([])
    expect(panel.errors).toEqual([])
  })
})
