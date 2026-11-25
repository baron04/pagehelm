/**
 * Chromium checks for launcher persistence, safe markdown/links, clipboard fallback, scroll anchoring and outcomes.
 */
import type {Page} from '@playwright/test'
import {entry, expect, labels, snapshot, test} from './harness'

test.describe('the launcher', () => {
  test('keeps the open or collapsed choice through a page refresh', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    await expect(panel.page.locator('.woa-window')).toBeVisible()
    await panel.page.reload()
    await expect(panel.page.locator('.woa-window')).toBeVisible()
    expect(await panel.page.evaluate(() => (window as unknown as {__visibility: boolean[]}).__visibility))
      .toEqual([true])
    await panel.page.getByRole('button', {name: labels.collapse}).click()
    await panel.page.reload()
    await expect(panel.page.locator('.woa-launcher')).toBeVisible()
    await expect(panel.page.locator('.woa-window')).toHaveCount(0)
    expect(panel.errors).toEqual([])
  })
})

test.describe('answers', () => {
  test('markdown renders tables and never links a foreign origin', async ({open}) => {
    const answer = '# 查询结果\n\n**3 张**销售订单待开票：\n\n' +
      '| 单号 | 客户 | 金额 |\n| --- | --- | ---: |\n' +
      '| S00007 | Deco Addict | 1,231.00 |\n\n' +
      '- 第一条\n- 第二条\n\n' +
      '见 [本地记录](/odoo/sales) 与 [外部站点](https://evil.example/steal)。\n\n' +
      "```python\nprint('hello')\n```\n"
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出待开票订单'),
      entry('e2', 'result', answer, {authored: 'server', panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-md table').waitFor()
    await expect(panel.page.locator('.woa-md h2')).toHaveText('查询结果')
    await expect(panel.page.locator('.woa-md strong')).toHaveText('3 张')
    await expect(panel.page.locator('.woa-md li')).toHaveCount(2)
    await expect(panel.page.locator('.woa-md table td')).toHaveCount(3)
    await expect(panel.page.locator('.woa-md table td').nth(2)).toHaveAttribute('data-align', 'right')
    // Untrusted output cannot mint a clickable link out of this session's origins.
    const hrefs = await panel.page.locator('.woa-md a').evaluateAll(
      nodes => nodes.map(node => (node as HTMLAnchorElement).href))
    expect(hrefs).toHaveLength(1)
    expect(hrefs[0]).toContain('/odoo/sales')
    await expect(panel.page.locator('.woa-md__inert')).toContainText('外部站点')
    await expect(panel.page.locator('.woa-md__block pre')).toContainText("print('hello')")
    expect(panel.errors).toEqual([])
  })
})

test.describe('copying an answer', () => {
  const ANSWER = '已扫完全部 12 条，命中 2 条。\n\n| 单号 | 金额 |\n| --- | ---: |\n' +
    '| S00007 | 1,231.00 |\n| S00019 | 725.50 |\n\n这两张都处于 **待开票** 状态。'
  const TABLE = '单号\t金额\nS00007\t1,231.00\nS00019\t725.50'
  const clipboard = (page: Page) => page.evaluate(() => navigator.clipboard.readText())

  test('copies the whole answer as plain text, its table as tab-separated rows', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出待开票订单'),
      entry('e2', 'result', ANSWER, {authored: 'server', panel_state: 'success'}),
    ]}), {clipboard: true})
    await panel.page.locator('.woa-launcher').click()
    const copy = panel.page.getByRole('button', {name: labels.copyAnswer})
    await copy.click()
    expect(await clipboard(panel.page))
      .toBe(`已扫完全部 12 条，命中 2 条。\n\n${TABLE}\n\n这两张都处于 待开票 状态。`)
    await expect(panel.page.getByRole('button', {name: labels.copied})).toBeVisible()
    expect(panel.errors).toEqual([])
  })

  test('still copies where the page is not a secure context', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出待开票订单'),
      entry('e2', 'result', ANSWER, {authored: 'server', panel_state: 'success'}),
    ]}), {clipboard: true})
    await panel.page.evaluate(() => {
      Object.defineProperty(window, 'isSecureContext', {value: false})
    })
    await panel.page.locator('.woa-launcher').click()
    await panel.page.getByRole('button', {name: labels.copyAnswer}).click()
    expect(await clipboard(panel.page))
      .toBe(`已扫完全部 12 条，命中 2 条。\n\n${TABLE}\n\n这两张都处于 待开票 状态。`)
    expect(panel.errors).toEqual([])
  })
})

test.describe('scroll and outcomes', () => {
  test('the newest line stays in view without being asked', async ({open}) => {
    const lines = Array.from({length: 40}, (_, index) => `第 ${index} 行。`).join('\n')
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出所有待开票的销售订单'),
      entry('e2', 'result', lines, {panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    const thread = panel.page.locator('.woa-thread')
    await thread.waitFor()
    await expect.poll(() => thread.evaluate(
      node => node.scrollHeight - node.scrollTop - node.clientHeight)).toBeLessThanOrEqual(30)
    await expect(panel.page.locator('.woa-jump')).toHaveCount(0)

    await panel.set({optimistic: {turnId: 't2', text: '再列一次', failed: false}})
    await expect(panel.page.locator('.woa-user')).toHaveCount(2)
    await expect.poll(() => thread.evaluate(
      node => node.scrollHeight - node.scrollTop - node.clientHeight)).toBeLessThanOrEqual(30)
    await expect(panel.page.locator('.woa-jump')).toHaveCount(0)
    expect(panel.errors).toEqual([])
  })

  test('the jump control is anchored and the latest answer has breathing room', async ({open}) => {
    const answer = Array.from({length: 80}, (_, index) => `第 ${index} 行内容。`).join('\n\n')
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '给我一份长清单'),
      entry('e2', 'result', answer, {panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.settle()
    const thread = panel.page.locator('.woa-thread')
    await thread.evaluate(node => { node.scrollTop = 0 })
    await panel.page.locator('.woa-jump').waitFor()
    const before = await panel.page.locator('.woa-jump').boundingBox()
    await thread.evaluate(node => { node.scrollTop = Math.min(10, node.scrollHeight) })
    const after = await panel.page.locator('.woa-jump').boundingBox()
    expect(Math.abs(before!.y - after!.y)).toBeLessThanOrEqual(1)

    const jump = panel.page.locator('.woa-jump')
    const restingBackground = await jump.evaluate(node => getComputedStyle(node).backgroundColor)
    await jump.hover()
    await expect.poll(() => jump.evaluate(node => getComputedStyle(node).backgroundColor))
      .not.toBe(restingBackground)

    await panel.page.emulateMedia({reducedMotion: 'no-preference'})
    await thread.evaluate(node => {
      const original = node.scrollTo.bind(node)
      node.scrollTo = (options?: ScrollToOptions | number, y?: number) => {
        if (typeof options === 'object' && options.behavior === 'smooth') {
          node.setAttribute('data-smooth-jump', 'true')
        }
        if (typeof options === 'number') original(options, y ?? 0)
        else original(options)
      }
    })
    await jump.click()
    await expect(thread).toHaveAttribute('data-smooth-jump', 'true')
    await expect.poll(() => thread.evaluate(
      node => node.scrollHeight - node.scrollTop - node.clientHeight)).toBeLessThanOrEqual(28)
    await expect.poll(async () => {
      const turn = await panel.page.locator('.woa-turn').last().boundingBox()
      const composer = await panel.page.locator('.woa-composer').boundingBox()
      return composer!.y - (turn!.y + turn!.height)
    }).toBeGreaterThanOrEqual(55)
    expect(panel.errors).toEqual([])
  })

  test('renders declined card neutrally by category, distinguishes faults, and focuses live needs_input', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '新建用户并填写初始密码'),
      entry('e2', 'action', 'fill_form', {tone: 'active'}),
      entry('e3', 'result', '密码字段须由你手动输入，请前往用户表单完成。', {
        panel_state: 'declined',
        tone: 'neutral',
        reason_category: 'policy',
        answer_targets: [{
          kind: 'page',
          address: '/system/user/edit/1',
          label: '用户编辑页',
          identity: 'system:user:1',
          read_ref: 'read#1',
        }],
        handoff: {
          completed_fields: [{name: '用户昵称', value: '测试'}],
          pending_fields: [{name: '密码'}],
          restricted_controls: [{label: '密码', kind: 'password', ref: 'pwd'}],
          can_undo: true,
        },
      }),
    ]}))
    await panel.page.locator('.woa-launcher').click()

    const declined = panel.page.locator('.woa-outcome[data-kind="declined"]')
    await expect(declined).toHaveAttribute('data-tone', 'neutral')
    await expect(declined).toHaveAttribute('data-category', 'policy')
    await expect(declined.locator('> h3')).toContainText('按规则不自动执行')
    await expect(panel.page.locator('.woa-took')).toHaveAttribute('data-declined', 'true')
    await expect(panel.page.locator('.woa-took')).not.toHaveAttribute('data-failed', 'true')
    await expect(declined.locator('.woa-handoff')).toBeVisible()
    await expect(declined.getByTestId('woa-handoff-controls')).toContainText('密码')
    await expect(declined.locator('.woa-answer-targets a')).toHaveAttribute('href', '/system/user/edit/1')

    // Move focus away from the composer before a live needs_input turn arrives.
    const closeButton = panel.page.getByRole('button', {name: labels.collapse})
    await closeButton.focus()
    expect((await panel.active()).id).not.toBe('woa-composer-input')

    await panel.set({
      connection: {state: 'needs_input', busy: false, stopping: false, online: true},
      entries: [
        entry('e1', 'goal', '新建用户并填写初始密码'),
        entry('e3', 'result', '密码字段须由你手动输入，请前往用户表单完成。', {
          panel_state: 'declined', tone: 'neutral', reason_category: 'policy',
        }),
        entry('e4', 'goal', '再查一下客户'),
        entry('e5', 'result', '请补充具体的客户名称。', {
          panel_state: 'needs_input', tone: 'neutral', replayed: false,
        }),
      ],
    })
    await expect(panel.page.locator('.woa-result__status')).toContainText(labels.statusNeedsInput)
    await expect(panel.page.locator('#woa-composer-input')).toBeFocused()
    expect(panel.errors).toEqual([])
  })
})
