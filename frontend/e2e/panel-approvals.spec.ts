/**
 * Chromium checks for approval cards, visibility dwell, operate cards and form prefill handoff.
 */
import presentation from '../src/presentation.json' with {type: 'json'}
import {PENDING, expect, labels, notices, snapshot, test} from './harness'

test.describe('approval triggers and scrolling', () => {
  test('an explicitly offered form handoff requires the same visible card gate as saving', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval({offer_prefill: true})
    await expect(panel.page.getByTestId('woa-prefill')).toHaveText(labels.prefillChoice)
    await expect(panel.page.getByTestId('woa-approve')).toHaveText(labels.saveChoice)
    await expect(panel.page.getByRole('alertdialog')).toBeFocused()
    await panel.page.getByTestId('woa-prefill').dispatchEvent('click')
    await panel.page.locator('.woa-approval__blocked').waitFor({timeout: 3000})
    expect(await panel.calls()).toEqual([])
    await panel.page.waitForTimeout(presentation.approval_visibility.min_visible_ms + 150)
    await panel.page.getByTestId('woa-prefill').click()
    expect(await panel.calls()).toContainEqual(['decide', 'a-1', 'prefill'])
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
})

test.describe('a confirmation', () => {
  test('is keyboard operable, and escape cancels', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval()
    await expect(panel.page.locator('.woa-approval')).toContainText('S00002')
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

  test('leave to work the page locks an empty composer, while a pre-existing draft can replace the turn', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval({kind: 'operate', effect: '在页面上帮你完成这件事',
                               targets: ['会打开页面、点击、填写和选择']})
    const card = panel.page.locator('.woa-approval')
    await expect(card).toHaveAttribute('data-kind', 'operate')
    await expect(panel.page.locator('#woa-composer-input')).toBeDisabled()
    await panel.page.keyboard.press('Escape')
    expect((await panel.calls())[0]?.[2]).toBe('denied')

    // Sending a preserved draft that predated the card replaces the waiting turn.
    await panel.set({pendingApproval: null})
    const composer = panel.page.locator('#woa-composer-input')
    await composer.fill('改问：Azure Interior 的电话是多少？')
    await panel.set({connection: {state: 'acting', busy: true, stopping: false, online: true}})
    await panel.raiseApproval({kind: 'operate', effect: '在页面上帮你完成这件事',
                               targets: ['会打开页面、点击、填写和选择']})
    await expect(composer).toBeEnabled()
    await composer.press('Enter')
    expect(await panel.calls()).toContainEqual(['submit', '改问：Azure Interior 的电话是多少？'])
    expect(panel.errors).toEqual([])
  })

  test('an expired one explains the timeout and frees the composer', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval()
    const composer = panel.page.locator('textarea')
    await expect(composer, 'a live confirmation blocks the composer').toBeDisabled()

    await panel.set({pendingApproval: {...PENDING, expires_at: 0}})
    await expect(panel.page.locator('.woa-approval')).toHaveCount(0, {timeout: 3000})
    await expect(panel.page.locator('.woa-inline-error')).toContainText(notices.approval_expired!)
    await expect(composer, 'the person can type again').toBeEnabled()
  })

  test('one that is not visible refuses and says why', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval({effect: '为 S00007 开票', targets: ['销售订单：S00007']})
    await panel.page.getByTestId('woa-approve').dispatchEvent('click')
    await panel.page.locator('.woa-approval__blocked').waitFor({timeout: 3000})
    await expect(panel.page.locator('.woa-approval__blocked')).toContainText(labels.approvalObscured)
    expect(await panel.calls()).toEqual([])
    expect(panel.errors).toEqual([])
  })
})
