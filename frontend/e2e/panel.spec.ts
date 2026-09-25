/**
 * Chromium checks for the assistant panel.
 *
 * These drive the built panel against a scripted host, so every state can be rendered without
 * a live session: the states a real run reaches rarely -- indeterminate, replaced, obscured
 * confirmation -- are exactly the ones worth pinning.
 */
import type {Page} from '@playwright/test'
import presentation from '../src/presentation.json' with {type: 'json'}
import tokens from '../src/styles/design_tokens.json' with {type: 'json'}
import {formatTime} from '../src/state/clock'
import {SAMPLE_EXAMPLES} from '../dev/examples'
import {
  CONFIGURATION, PENDING, STRICT_CSP, entry, expect, labels, notices, snapshot, test,
  type Snapshot,
} from './harness'

test.describe('the launcher', () => {
  test('carries the state the header no longer shows', async ({open}) => {
    const panel = await open(snapshot())
    const launcher = panel.page.locator('.woa-launcher')
    await launcher.waitFor()
    await expect(launcher).toHaveAttribute('data-state', 'idle')
    await panel.set({connection: {state: 'acting', busy: true, stopping: false, online: true}})
    await expect(launcher).toHaveAttribute('data-state', 'working')
    await panel.set({connection: {state: 'replaced', busy: false, stopping: false, online: true}})
    await expect(launcher).toHaveAttribute('data-state', 'closed')
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

  test('panel interactions do not reach the host application event delegates', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.evaluate(() => {
      const escaped: string[] = []
      ;(window as unknown as {__escapedPanelEvents: string[]}).__escapedPanelEvents = escaped
      for (const type of ['click', 'pointerdown', 'keydown', 'input', 'submit']) {
        document.addEventListener(type, () => escaped.push(type))
      }
    })
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('#woa-composer-input').fill('列出客户')
    await panel.page.keyboard.press('Enter')
    await panel.page.getByRole('button', {name: labels.collapse}).click()

    expect(await panel.page.evaluate(
      () => (window as unknown as {__escapedPanelEvents: string[]}).__escapedPanelEvents))
      .toEqual([])
    expect(await panel.calls()).toEqual([['submit', '列出客户']])
    await panel.page.locator('.woa-launcher').waitFor({timeout: 3000})
    expect(panel.errors).toEqual([])
  })
})

test.describe('the default state', () => {
  test('a conversation being fetched shows nothing at first, then a skeleton, never the greeting', async ({open}) => {
    // A tab that already has a conversation fetches it when the panel opens. A quick fetch
    // shows nothing in between; a slow one shows three lines -- long, short, medium -- after
    // 300 ms. The greeting belongs to a new conversation and would be a lie here.
    const panel = await open(snapshot({
      connection: {state: 'connecting', busy: false, stopping: false, online: true}}))
    const page = panel.page
    await page.locator('.woa-launcher').click()
    await page.locator('.woa-window').waitFor()
    await expect(page.locator('.woa-empty')).toHaveCount(0)
    await expect(page.locator('.woa-skeleton')).toHaveCount(0)
    await expect(page.locator('.woa-skeleton i')).toHaveCount(3, {timeout: 2000})
    const widths = await page.locator('.woa-skeleton i').evaluateAll(
      nodes => nodes.map(node => node.getBoundingClientRect().width))
    expect(widths[0]).toBeGreaterThan(widths[2]!)
    expect(widths[2]).toBeGreaterThan(widths[1]!)
    // The composer is there throughout: the person can start writing while it loads.
    await expect(page.locator('#woa-composer-input')).toBeEnabled()
    await panel.set({
      connection: {state: 'ready', busy: false, stopping: false, online: true},
      entries: [entry('e1', 'goal', '列出订单'), entry('e2', 'result', '三张。', {panel_state: 'success'})]})
    await expect(page.locator('.woa-skeleton')).toHaveCount(0)
    await expect(page.locator('.woa-user')).toHaveText('列出订单')
    expect(panel.errors).toEqual([])
  })

  test('opening shows it: a greeting, the assistant, and a way in for each kind of request', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    const empty = panel.page.locator('.woa-empty')
    await empty.waitFor()
    // Who is here, the question, and what it can be asked.
    await expect(empty.locator('.woa-empty__intro')).toHaveText(labels.emptyIntro)
    await expect(empty.locator('h3')).toHaveText(labels.emptyGreeting)
    await expect(empty.locator('.woa-empty__detail')).toHaveText(labels.emptyDetail)
    // The assistant's picture is in the bundle itself: it loads with no request of its own.
    // The light theme shows it as drawn; the dark theme has the figure alone, on a glow.
    for (const theme of ['light', 'dark']) {
      await expect.poll(() => empty.locator(`.woa-empty__art[data-for="${theme}"]`)
        .evaluate((node: HTMLImageElement) => node.naturalWidth)).toBe(360)
    }
    await expect(empty.locator('.woa-empty__art[data-for="light"]')).toBeVisible()
    await expect(empty.locator('.woa-empty__art[data-for="dark"]')).toBeHidden()
    // A way in for each kind the page offers an example of, in the page's own words, and a
    // last one that asks the assistant itself; nothing that sends on its own.
    const ways = empty.locator('.woa-example')
    await expect(ways).toHaveCount(4)
    const expected = [[labels.exampleQuery, SAMPLE_EXAMPLES[0]!.text],
                      [labels.exampleFind, SAMPLE_EXAMPLES[1]!.text],
                      [labels.exampleOperate, SAMPLE_EXAMPLES[2]!.text],
                      [labels.exampleMore, labels.exampleMorePrompt]]
    for (const [index, [title, text]] of expected.entries()) {
      await expect(ways.nth(index).locator('.woa-example__title')).toHaveText(title!)
      await expect(ways.nth(index).locator('.woa-example__text')).toHaveText(`“${text}”`)
    }
    // A full-width mark that ends an example is set half-width, so the closing quote sits
    // against it; a sentence that ends on a character is left as it is.
    await expect(ways.nth(3).locator('.woa-example__halt')).toHaveText('？')
    await expect(ways.nth(3).locator('.woa-example__halt'))
      .toHaveCSS('font-feature-settings', '"halt"')
    await expect(ways.nth(0).locator('.woa-example__halt')).toHaveCount(0)
    // Every answer is a model's, so the panel says so under the composer, in the server's words.
    await expect(panel.page.locator('.woa-composer__disclaimer')).toHaveText(labels.aiDisclaimer)
    // The composer is at the foot of the window from the start, where it stays once the
    // conversation begins: sending does not move it. It offers nothing it cannot do.
    const window = await panel.page.locator('.woa-window').boundingBox()
    const composer = await panel.page.locator('.woa-composer').boundingBox()
    expect(window!.y + window!.height - (composer!.y + composer!.height)).toBeLessThan(2)
    await expect(panel.page.locator('.woa-composer button')).toHaveCount(1)
    await expect(panel.page.locator('#woa-composer-input'))
      .toHaveAttribute('placeholder', labels.placeholder)
    expect(await panel.calls()).toEqual([])
    expect(panel.errors).toEqual([])
  })

  test('a way in fills the composer for the person to send or change, and sends nothing', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-example').nth(2).click()
    const composer = panel.page.locator('#woa-composer-input')
    await expect(composer).toHaveValue(SAMPLE_EXAMPLES[2]!.text)
    await expect(composer).toBeFocused()
    expect(await composer.evaluate((node: HTMLTextAreaElement) => node.selectionStart))
      .toBe(SAMPLE_EXAMPLES[2]!.text.length)
    expect(await panel.calls()).toEqual([])
    await panel.page.locator('.woa-example').nth(3).click()
    await expect(composer).toHaveValue(labels.exampleMorePrompt)
    expect(panel.errors).toEqual([])
  })

  test('in a short window the greeting is read from its top, not pinned to the bottom', async ({open}) => {
    const panel = await open(snapshot(), {width: 320, height: 480})
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-empty').waitFor()
    await panel.page.waitForTimeout(300)
    const thread = panel.page.locator('.woa-thread')
    expect(await thread.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true)
    expect(await thread.evaluate(node => node.scrollTop)).toBe(0)
    await expect(panel.page.locator('.woa-empty__intro')).toBeInViewport()
    expect(panel.errors).toEqual([])
  })

  test('a clarification releases and focuses the composer for the reply', async ({open}) => {
    const panel = await open(snapshot({
      connection: {state: 'needs_input', busy: false, stopping: false, online: true},
      entries: [
        entry('e1', 'goal', '打开 Acme'),
        entry('e2', 'result', '请选择：\n1. Acme — 伦敦\n2. Acme — 巴黎',
              {panel_state: 'needs_input', tone: 'warning'}),
      ],
    }))
    await panel.page.locator('.woa-launcher').click()
    const composer = panel.page.locator('#woa-composer-input')
    await composer.waitFor()

    await expect(composer).toBeEnabled()
    expect((await panel.active()).id).toBe('woa-composer-input')
    await expect(panel.page.locator('.woa-thread')).toContainText('Acme — 伦敦')
    await expect(panel.page.locator('.woa-window')).toHaveAttribute('data-state', 'needs_input')
    expect(panel.errors).toEqual([])
  })

  test('an arriving result preserves the unsent draft, focus and host route', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '先查询报价单'),
      entry('e2', 'result', '已完成第一轮。', {panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    const composer = panel.page.locator('#woa-composer-input')
    await composer.fill('这段话还没有发送')
    await composer.focus()
    const route = panel.page.url()

    await panel.set({entries: [
      {id: 'e1', turn_id: 'turn-1', kind: 'goal', text: '先查询报价单', at: 1,
       panel_state: 'acting', tone: 'neutral', replayed: false},
      {id: 'e2', turn_id: 'turn-1', kind: 'result', text: '已完成第一轮。', at: 2,
       panel_state: 'success', tone: 'success', replayed: false},
      {id: 'e3', turn_id: 'turn-2', kind: 'goal', text: '后台到达的另一轮', at: 3,
       panel_state: 'acting', tone: 'neutral', replayed: false},
      {id: 'e4', turn_id: 'turn-2', kind: 'result', text: '另一轮也已完成。', at: 4,
       panel_state: 'success', tone: 'success', replayed: false},
    ]})
    await panel.page.waitForTimeout(300)

    expect(await composer.inputValue()).toBe('这段话还没有发送')
    expect(panel.page.url()).toBe(route)
    expect((await panel.active()).id).toBe('woa-composer-input')
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
                               targets: ['你的请求：打开 azur', '会打开页面、点击、填写和选择']})
    const card = panel.page.locator('.woa-approval')
    await expect(card).toHaveAttribute('data-kind', 'operate')
    await expect(card).toContainText(labels.operateTitle)
    // Working the page is a route through it, not a guarded change.
    await expect(card.locator('h2 svg.lucide-route')).toHaveCount(1)
    await expect(card.locator('h2 svg.lucide-shield-alert')).toHaveCount(0)
    // Its terms show: it can be stopped, a change is still asked about, unsaved work may go.
    await expect(card).toContainText(labels.operateHint)
    await expect(card).toContainText('你的请求：打开 azur')
    await expect(panel.page.getByTestId('woa-deny')).toHaveText(labels.deny)
    await expect(panel.page.getByTestId('woa-approve')).toHaveText(labels.operateConfirm)
    await expect(panel.page.locator('#woa-composer-input')).toBeDisabled()
    // Focus starts on the answer that does nothing, and Escape gives it.
    expect((await panel.active()).testid).toBe('woa-deny')
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
                               targets: ['你的请求：打开 azur', '会打开页面、点击、填写和选择']})

    await expect(composer).toBeEnabled()
    await composer.press('Enter')
    expect(await panel.calls()).toEqual([['submit', '改问：Azure Interior 的电话是多少？']])
    expect(panel.errors).toEqual([])
  })

  test('stays usable at 320 by 480', async ({open}) => {
    const panel = await open(snapshot(), {width: 320, height: 480})
    await panel.raiseApproval({effect: '新建客户 Nordwind Handelsgesellschaft mbH & Co. KG',
                               targets: ['res.partner · 新建一条客户记录']})
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

  test('an expired one goes at once, says nothing, and frees the composer', async ({open}) => {
    // Past the deadline the server takes no answer and ends the turn as a stop, so the card
    // goes without a word. A card left up to be pressed once made the runtime treat the 409
    // as "another page took this session over" (P7.5 CR-38); a notice about the timeout was
    // ruled out by the owner: a timeout interrupts the message, it does not explain itself.
    const panel = await open(snapshot())
    await panel.raiseApproval()
    const composer = panel.page.locator('textarea')
    await expect(composer, 'a live confirmation blocks the composer').toBeDisabled()

    await panel.set({pendingApproval: {...PENDING, expires_at: 0}})
    await panel.page.waitForTimeout(1200)

    await expect(panel.page.locator('.woa-approval')).toHaveCount(0)
    await expect(panel.page.locator('.woa-notice, .woa-inline-error')).toHaveCount(0)
    await expect(composer, 'the person can type again').toBeEnabled()
  })

  test('a turn a card timed out on ends stopped, with no line of its own', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '打开 Azure Interior 的客户页面'),
      entry('e2', 'progress', '正在思考'),
      entry('e3', 'progress', '已读取 1 条：客户 Azure Interior'),
      entry('e4', 'approval', '需要确认后才能操作页面', {tone: 'warning'}),
      entry('e5', 'result', '', {panel_state: 'cancelled'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-turn').waitFor()
    await expect(panel.page.locator('.woa-turn .woa-work__summary svg.lucide-circle-slash'))
      .toHaveCount(1)
    await expect(panel.page.locator('.woa-turn .woa-outcome')).toHaveCount(0)
    await expect(panel.page.locator('.woa-turn .woa-answer-actions')).toHaveCount(0)
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
    await panel.raiseApproval({effect: '为 S00007 开票', targets: ['sale.order · S00007']})
    // Clicking before the card has been visible for its dwell is refused; the reason is
    // shown rather than the press silently doing nothing.
    await panel.page.getByTestId('woa-approve').dispatchEvent('click')
    await panel.page.locator('.woa-approval__blocked').waitFor({timeout: 3000})
    await expect(panel.page.locator('.woa-approval__blocked')).toContainText(labels.approvalObscured)
    expect(await panel.calls()).toEqual([])
    expect(panel.errors).toEqual([])
  })
})

test.describe('answers', () => {
  test('only model-authored prose is revealed progressively', async ({open}) => {
    const server = entry('e2', 'result', '3 张销售订单待开票。',
                         {authored: 'server', panel_state: 'success'})
    const panel = await open(snapshot({entries: [entry('e1', 'goal', '列出待开票订单'), server]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-md').waitFor()
    // A server projection is a fact the server computed; it appears whole.
    expect(await panel.page.locator('.woa-md').innerHTML()).not.toContain('woa-md__caret')
    await panel.set({entries: [
      {id: 'e1', sequence: 1, turn_id: 'turn-2', at: 0, kind: 'goal', tone: 'neutral',
       text: '再说一次'},
      {id: 'e3', sequence: 3, turn_id: 'turn-2', at: 0, kind: 'result', tone: 'neutral',
       authored: 'model', panel_state: 'success',
       text: '不是。这个页面背后的动作本身没有存任何 domain，它列的是全部销售订单。'}]})
    await panel.page.locator('.woa-md__caret').waitFor({timeout: 3000})
    await panel.page.locator('.woa-md').last().click()
    await expect(panel.page.locator('.woa-md__caret')).toHaveCount(0, {timeout: 3000})
    await expect(panel.page.locator('.woa-md').last()).toContainText('全部销售订单')
    expect(panel.errors).toEqual([])
  })

  test('markdown renders tables and never links a foreign origin', async ({open}) => {
    const answer = '3 张销售订单待开票：\n\n' +
      '| 单号 | 客户 | 金额 |\n| --- | --- | ---: |\n' +
      '| S00007 | Deco Addict | 1,231.00 |\n\n' +
      '见 [本地记录](/odoo/sales) 与 [外部站点](https://evil.example/steal)。\n\n' +
      "```python\nprint('hello')\n```\n"
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出待开票订单'),
      entry('e2', 'result', answer, {authored: 'server', panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-md table').waitFor()
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

  test('an answer keeps its controls and its time out of sight until pointed at', async ({open}) => {
    // Even the newest answer shows nothing under it at rest; pointed at, the copy control
    // appears with when the answer was given, the way a chat on a phone says it.
    const at = Date.now() - 60_000
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出待开票订单', {at}),
      entry('e2', 'result', ANSWER, {authored: 'server', panel_state: 'success', at}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    const actions = panel.page.locator('.woa-answer-actions')
    await expect(actions).toHaveCSS('opacity', '0')
    await panel.page.locator('.woa-md').first().hover()
    await expect(actions).toHaveCSS('opacity', '1')
    await expect(actions.locator('time')).toHaveText(formatTime(at, Date.now(), labels.locale))
    expect(panel.errors).toEqual([])
  })

  test('nothing of the copy control or its tooltip is cut off by a settled turn', async ({open}) => {
    // A settled turn contains its paint, so whatever overhangs its box is clipped. The copy
    // control overhangs the text to line its glyph up, and its tooltip hung centred beyond
    // the control: both were cut at the text's left edge.
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出待开票订单'),
      entry('e2', 'result', ANSWER, {authored: 'server', panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    const copy = panel.page.getByRole('button', {name: labels.copyAnswer})
    await copy.hover()
    const boxes = await copy.evaluate(node => {
      const turn = node.closest<HTMLElement>('.woa-turn')!
      const control = node.getBoundingClientRect()
      const tip = getComputedStyle(node, '::after')
      // The tooltip's box as drawn: its offset, then whatever its transform shifts it by.
      const shift = tip.transform === 'none' ? 0 : new DOMMatrix(tip.transform).m41
      const tipLeft = control.left + Number.parseFloat(tip.left) + shift
      return {
        settled: turn.dataset.settled === 'true' && getComputedStyle(turn).contentVisibility === 'auto',
        turn: turn.getBoundingClientRect().toJSON() as DOMRect,
        control: control.toJSON() as DOMRect,
        tip: {left: tipLeft, right: tipLeft + Number.parseFloat(tip.width)},
      }
    })
    expect(boxes.settled).toBe(true)
    expect(boxes.control.left).toBeGreaterThanOrEqual(boxes.turn.left)
    expect(boxes.tip.left).toBeGreaterThanOrEqual(boxes.turn.left)
    expect(boxes.tip.right).toBeLessThanOrEqual(boxes.turn.right)
    expect(panel.errors).toEqual([])
  })

  test('copies the whole answer as plain text, its table as tab-separated rows', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出待开票订单'),
      entry('e2', 'result', ANSWER, {authored: 'server', panel_state: 'success'}),
    ]}), {clipboard: true})
    await panel.page.locator('.woa-launcher').click()
    const copy = panel.page.getByRole('button', {name: labels.copyAnswer})
    await copy.click()
    // Pasted into a document it reads as prose; into a spreadsheet the table splits into cells.
    expect(await clipboard(panel.page))
      .toBe(`已扫完全部 12 条，命中 2 条。\n\n${TABLE}\n\n这两张都处于 待开票 状态。`)
    await expect(panel.page.getByRole('button', {name: labels.copied})).toBeVisible()
    expect(panel.errors).toEqual([])
  })

  test('a table carries no copy control of its own', async ({open}) => {
    // Copying the answer already brings its tables as rows a spreadsheet splits into cells.
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出待开票订单'),
      entry('e2', 'result', ANSWER, {authored: 'server', panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-md table').hover()
    await expect(panel.page.locator('.woa-md').getByRole('button')).toHaveCount(0)
    expect(panel.errors).toEqual([])
  })

  test('still copies where the page is not a secure context', async ({open}) => {
    // A host served over plain HTTP on an internal address has no asynchronous clipboard;
    // the older copy command is the way there, and it has to work.
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出待开票订单'),
      entry('e2', 'result', ANSWER, {authored: 'server', panel_state: 'success'}),
    ]}), {clipboard: true})
    await panel.page.evaluate(() => {
      Object.defineProperty(window, 'isSecureContext', {value: false})
    })
    await panel.page.locator('.woa-launcher').click()
    await panel.page.getByRole('button', {name: labels.copyAnswer}).click({force: true})
    expect(await clipboard(panel.page))
      .toBe(`已扫完全部 12 条，命中 2 条。\n\n${TABLE}\n\n这两张都处于 待开票 状态。`)
    expect(panel.errors).toEqual([])
  })
})

test.describe('the keyboard', () => {
  // ⌘J on a Mac, Ctrl+J elsewhere -- whichever this browser reports being.
  const modifier = (page: Page) => page.evaluate(
    () => (/Mac|iPhone|iPad/.test(navigator.platform) ? 'Meta' : 'Control'))

  test('one shortcut opens the panel from the page and puts it away from inside', async ({open}) => {
    const panel = await open(snapshot())
    const page = panel.page
    const mod = await modifier(page)
    await expect(page.locator('.woa-launcher')).toHaveAttribute(
      'aria-keyshortcuts', mod === 'Meta' ? 'Meta+J' : 'Control+J')
    // The launcher says nothing when pointed at: the disc in the corner is its own label.
    await page.locator('.woa-launcher').hover()
    await page.waitForTimeout(500)
    expect(await page.locator('.woa-launcher').evaluate(
      node => getComputedStyle(node, '::after').content)).toBe('none')
    await page.locator('body').click({position: {x: 5, y: 5}})
    await page.keyboard.press(`${mod}+j`)
    await page.locator('.woa-window').waitFor()
    // Opened from the keyboard, the caret is where the person types.
    expect((await panel.active()).id).toBe('woa-composer-input')
    // Inside the panel the same keys put it away, and focus waits on the launcher.
    await page.keyboard.press(`${mod}+j`)
    await expect(page.locator('.woa-window')).toHaveCount(0)
    expect((await panel.active()).label).toBe(labels.expand)
    expect(panel.errors).toEqual([])
  })

  test('escape puts the window away, except inside a confirmation', async ({open}) => {
    const panel = await open(snapshot())
    const page = panel.page
    await page.locator('.woa-launcher').click()
    await page.locator('#woa-composer-input').fill('草稿留着')
    await page.keyboard.press('Escape')
    await expect(page.locator('.woa-window')).toHaveCount(0)
    await page.locator('.woa-launcher').click()
    // The draft outlives putting the window away.
    await expect(page.locator('#woa-composer-input')).toHaveValue('草稿留着')

    // In a confirmation Escape is the card's own answer, and the window stays.
    await panel.raiseApproval({kind: 'operate', effect: '在页面上帮你完成这件事',
                               targets: ['你的请求：打开 azur']})
    await page.keyboard.press('Escape')
    await expect(page.locator('.woa-window')).toHaveCount(1)
    expect(panel.errors).toEqual([])
  })

  test('the arrows walk back through what was asked, until the person types', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出待开票订单'),
      entry('e2', 'result', '三张。', {panel_state: 'success'}),
      entry('e3', 'goal', 'Acme 有哪些草稿报价单', {turn_id: 'turn-2'}),
      entry('e4', 'result', '两张。', {panel_state: 'success', turn_id: 'turn-2'}),
    ]}))
    const page = panel.page
    await page.locator('.woa-launcher').click()
    const composer = page.locator('#woa-composer-input')
    await composer.focus()
    await page.keyboard.press('ArrowUp')
    await expect(composer).toHaveValue('Acme 有哪些草稿报价单')
    await page.keyboard.press('ArrowUp')
    await expect(composer).toHaveValue('列出待开票订单')
    // The oldest is where it stops.
    await page.keyboard.press('ArrowUp')
    await expect(composer).toHaveValue('列出待开票订单')
    await page.keyboard.press('ArrowDown')
    await expect(composer).toHaveValue('Acme 有哪些草稿报价单')
    await page.keyboard.press('ArrowDown')
    await expect(composer).toHaveValue('')
    // Once the text is the person's own, the arrows only move the caret.
    await page.keyboard.type('新的问题')
    await page.keyboard.press('ArrowUp')
    await expect(composer).toHaveValue('新的问题')
    expect(await panel.calls()).toEqual([])
    expect(panel.errors).toEqual([])
  })
})

test.describe('failures', () => {
  test('retry is offered only for a failure that can be retried', async ({open}) => {
    const panel = await open(snapshot({notice: {code: '', retryable: true}}))
    await panel.page.locator('.woa-launcher').click()
    const notice = panel.page.locator('.woa-inline-error')
    await notice.waitFor()
    await expect(notice.locator('.woa-inline-error__retry')).toHaveCount(1)
    await panel.set({notice: {code: 'runtime_replaced', retryable: false}})
    await expect(notice).toHaveAttribute('data-terminal', 'true')
    await expect(notice.locator('.woa-inline-error__retry')).toHaveCount(0)
    // It names where the assistant went, because this is the one ending the page does not
    // reconnect from: the person continues in the other page.
    await expect(notice).toContainText('另一个页面')
    expect(panel.errors).toEqual([])
  })

  test('an ended session offers nothing to send', async ({open}) => {
    // A composer that still shows its send control after the session is over reads as a
    // place to keep typing; the field says it is closed and offers no control at all.
    const panel = await open(snapshot({
      connection: {state: 'replaced', busy: false, stopping: false, online: true},
      closed: true,
      notice: {code: 'runtime_replaced', retryable: false}}))
    await panel.page.locator('.woa-launcher').click()
    await expect(panel.page.locator('.woa-field')).toHaveAttribute('data-ended', 'true')
    await expect(panel.page.locator('#woa-composer-input')).toBeDisabled()
    await expect(panel.page.locator('.woa-send')).toHaveCount(0)
    expect(panel.errors).toEqual([])
  })

  test('a failure with no guidance of its own is not called a lost connection', async ({open}) => {
    // `gateway_error` is the server falling over, not the network going away.
    //
    // The fallback used to be the reconnecting line, so any code the contract had not
    // anticipated told the operator their connection had dropped and that nothing had
    // been re-run -- two claims about a failure nobody had established the shape of.
    const panel = await open(snapshot({notice: {code: 'gateway_error', retryable: true}}))
    await panel.page.locator('.woa-launcher').click()
    const notice = panel.page.locator('.woa-inline-error')
    await notice.waitFor()
    await expect(notice).toContainText(notices.gateway_error!)
    await panel.set({notice: {code: 'nothing_maps_this', retryable: true}})
    await expect(notice).toContainText(notices.unexpected!)
    await expect(notice).not.toContainText(notices.reconnecting!)
    expect(panel.errors).toEqual([])
  })
})

test.describe('the work process', () => {
  test('a long table keeps the last row and its source', async ({open}) => {
    const rows = Array.from({length: 50}, (_, index) => {
      const n = String(index + 1).padStart(3, '0')
      return `| Synthetic Contact ${n} Demonstration Department Research Group | contact-${n}@synthetic.example.test |`
    })
    const text = '| Name | Email |\n| --- | --- |\n' + rows.join('\n') +
      '\n\n> source: all rows in this read'
    expect(text.length).toBeGreaterThan(4000)
    const panel = await open(snapshot({entries: [entry('e1', 'result', text, {panel_state: 'success'})]}))
    await panel.page.locator('.woa-launcher').click()
    await expect(panel.page.locator('.woa-md tbody tr')).toHaveCount(50)
    await expect(panel.page.locator('.woa-md')).toContainText('contact-050@synthetic.example.test')
    await expect(panel.page.locator('.woa-md')).toContainText('source: all rows in this read')
    expect(panel.errors).toEqual([])
  })

  test('source details expand by keyboard without hiding a partial answer', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '未确认报价合计？'),
      entry('e2', 'result', '**小计**：12.50 USD\n\n这是部分回答', {
        panel_state: 'success',
        source_details: '读取时间：2026-09-13T00:00:00Z\n\n扫描记录：2；匹配记录：2'}),
    ]}), {width: 390, height: 844})
    await panel.page.locator('.woa-launcher').click()
    const details = panel.page.locator('.woa-answer-sources')
    const summary = details.locator('summary')
    await expect(summary).toHaveText(labels.answerSources)
    expect(await details.evaluate(node => (node as HTMLDetailsElement).open)).toBe(false)
    await expect(panel.page.getByText('这是部分回答', {exact: true})).toBeVisible()
    await summary.focus()
    await summary.press('Enter')
    expect(await details.evaluate(node => (node as HTMLDetailsElement).open)).toBe(true)
    await expect(details).toContainText('2026-09-13')
    await summary.press('Enter')
    expect(await details.evaluate(node => (node as HTMLDetailsElement).open)).toBe(false)
    expect(panel.errors).toEqual([])
  })

  test('a turn that only ran shows its time, and one that acted folds its steps into a line', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出所有待开票的销售订单'),
      entry('e2', 'progress', '正在理解目标'),
      entry('e3', 'result', '十六张。', {panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-took').waitFor()
    // Nothing happened that a list could show, so there is no list and no control promising
    // one -- only how long it took.
    await expect(panel.page.locator('.woa-work')).toHaveCount(0)
    await expect(panel.page.locator('.woa-took')).toContainText(labels.elapsedPrefix)

    await panel.set({entries: [
      {id: 'f1', sequence: 1, turn_id: 't2', at: 1757000000000, kind: 'goal', tone: 'neutral',
       text: '打开 Acme 的客户记录'},
      {id: 'f2', sequence: 2, turn_id: 't2', at: 1757000000000, kind: 'progress', tone: 'active',
       text: '正在理解目标'},
      {id: 'f3', sequence: 3, turn_id: 't2', at: 1757000002000, kind: 'action', tone: 'active',
       text: '打开页面：/odoo/customers/10'},
      {id: 'f4', sequence: 4, turn_id: 't2', at: 1757000003000, kind: 'result', tone: 'neutral',
       text: '已打开。', panel_state: 'success'}]})
    // A landed turn that did something says how much, in one line, and the answer leads.
    const summary = panel.page.locator('.woa-work__summary')
    await expect(summary).toContainText(labels.workSteps.replace('{count}', '2'))
    await expect(summary).toContainText(`${labels.elapsedPrefix} 3s`)
    await expect(summary).toHaveAttribute('aria-expanded', 'false')
    await expect(panel.page.locator('.woa-step')).toHaveCount(0)
    // One press opens the record; nothing about it was thrown away.
    await summary.click()
    await expect(summary).toHaveAttribute('aria-expanded', 'true')
    await expect(panel.page.locator('.woa-step')).toHaveCount(2)
    expect(panel.errors).toEqual([])
  })
  test('a turn\'s time leaves out the time it waited for the person to confirm', async ({open}) => {
    // Nine seconds of work around a minute and a half spent waiting on the confirmation card:
    // the time shown is the assistant's, not the person's.
    const line = (id: string, second: number, kind: string, text: string,
                  extra: Record<string, string> = {}) =>
      entry(id, kind, text, {turn_id: 't1', at: 1_757_000_000_000 + second * 1000, ...extra})
    const panel = await open(snapshot({entries: [
      line('g1', 0, 'goal', '确认报价单 S00002'),
      line('g2', 4, 'progress', '正在准备这次更改'),
      line('g3', 5, 'approval', '需要确认后才能继续'),
      line('g4', 95, 'progress', '已收到你的确认'),
      line('g5', 99, 'result', '已确认。', {panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await expect(panel.page.locator('.woa-work__summary')).toContainText(`${labels.elapsedPrefix} 9s`)

    // Nobody answered: the wait runs to the end of the turn, and none of it counts.
    await panel.set({entries: [
      line('g1', 0, 'goal', '打开 Azure Interior'),
      line('g2', 3, 'action', '已找到唯一客户 Azure Interior'),
      line('g3', 3, 'approval', '需要确认后才能操作页面'),
      line('g4', 303, 'result', '没有得到操作页面的确认。', {panel_state: 'cancelled'}),
    ]})
    await expect(panel.page.locator('.woa-work__summary')).toContainText(`${labels.elapsedPrefix} 3s`)
    expect(panel.errors).toEqual([])
  })

  test('a running turn folds its steps into one live line, one press from the whole run', async ({open}) => {
    // While it runs, the person needs to tell working from stuck: what it is doing now, and
    // for how long. Every step in the flow grew a list that pushed the question off screen.
    const running = {state: 'acting', busy: true, stopping: false, online: true}
    const steps = [
      entry('e1', 'goal', '检查名为 Acme 的客户是否存在'),
      entry('e2', 'progress', '正在思考', {tone: 'active'}),
      entry('e3', 'progress', '正在读取业务数据', {tone: 'active'}),
      entry('e4', 'progress', '已读取 3 条：客户', {tone: 'active'}),
    ]
    const panel = await open(snapshot({connection: running, entries: steps}))
    const page = panel.page
    await page.locator('.woa-launcher').click()
    const live = page.locator('.woa-work__live')
    await expect(live).toHaveAttribute('aria-expanded', 'false')
    await expect(live.locator('.woa-work__now')).toHaveText('已读取 3 条：客户')
    await expect(live.locator('.woa-spinner')).toHaveCount(1)
    await expect(page.locator('.woa-step')).toHaveCount(0)
    // A new step takes the line.
    await panel.set({connection: running,
                     entries: [...steps, entry('e5', 'progress', '正在整理结果', {tone: 'active'})]})
    await expect(live.locator('.woa-work__now')).toHaveText('正在整理结果')
    // Opened, every step in order: the newest is the one being worked on, the rest completed.
    await live.click()
    await expect(live).toHaveAttribute('aria-expanded', 'true')
    expect((await page.locator('.woa-step__text').allInnerTexts()).map(line => line.trim()))
      .toEqual(['正在思考', '正在读取业务数据', '已读取 3 条：客户', '正在整理结果'])
    await expect(page.locator(".woa-step[data-status='running']")).toHaveCount(1)
    await expect(page.locator(".woa-step[data-status='done']")).toHaveCount(3)
    // Each mark sits on the middle of its text's first line.
    const offsets = await page.locator('.woa-step').evaluateAll(nodes => nodes.map(node => {
      const mark = node.querySelector('.woa-step__mark > *')!.getBoundingClientRect()
      const text = node.querySelector('.woa-step__text')!
      const line = Number.parseFloat(getComputedStyle(text).lineHeight)
      const top = text.getBoundingClientRect().top
      return Math.abs((mark.top + mark.height / 2) - (top + line / 2))
    }))
    for (const offset of offsets) expect(offset).toBeLessThanOrEqual(0.5)
    expect(panel.errors).toEqual([])
  })

  test('a confirmation waiting on the person turns the live line amber', async ({open}) => {
    // The ball is in the person's court: the spinner becomes a slow pulse and the line says
    // what it is waiting for, in the warning colour.
    const panel = await open(snapshot({
      connection: {state: 'acting', busy: true, stopping: false, online: true},
      entries: [
        entry('e1', 'goal', '把报价单 S00002 确认成销售订单'),
        entry('e2', 'progress', '正在准备这次更改', {tone: 'active'}),
        entry('e3', 'approval', '需要确认后才能继续', {tone: 'warning'}),
      ]}))
    await panel.page.locator('.woa-launcher').click()
    const live = panel.page.locator('.woa-work__live')
    await expect(live).toHaveAttribute('data-waiting', 'true')
    await expect(live.locator('.woa-pulse')).toHaveCount(1)
    await expect(live.locator('.woa-spinner')).toHaveCount(0)
    await expect(live.locator('.woa-work__now')).toHaveText('需要确认后才能继续')
    await expect(live.locator('.woa-work__now')).toHaveCSS('color', rgb(tokens.themes.light.warning))
    expect(panel.errors).toEqual([])
  })

  test('a settled turn keeps its timeline and opens a node onto the call', async ({open}) => {
    // P7.5: the record of how an answer was produced outlives the answer.
    //
    // It is folded to one line once the turn lands, and stays there to be opened: which call
    // produced the numbers, with what arguments, and what came back.
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '未确认报价单里已发送的合计多少钱？', {at: 1_757_000_000_000}),
      entry('e2', 'progress', '正在读取业务数据', {
        tone: 'active', at: 1_757_000_002_000,
        step: {stage: 'tool', tool: 'query_quotations', arguments: 'limit=50'}}),
      entry('e3', 'progress', '已读取 3 条：未确认、未取消的报价单', {
        tone: 'active', at: 1_757_000_005_000,
        step: {stage: 'read_done', tool: 'query_quotations',
               result: '3 条 · 未确认、未取消的报价单'}}),
      entry('e4', 'result', '合计：504.00 USD', {panel_state: 'success', at: 1_757_000_006_000}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-work__summary').click()
    await panel.page.locator('.woa-step').first().waitFor()
    await expect(panel.page.locator('.woa-step')).toHaveCount(2)
    await expect(panel.page.locator(".woa-step[data-status='done']")).toHaveCount(2)
    // A node that opens says so; how long it took is inside, not in a column beside it.
    await expect(panel.page.locator('.woa-step__chevron')).toHaveCount(2)
    await expect(panel.page.locator('.woa-work__steps')).not.toContainText('3s')
    // Opening a node shows what was called, with what, and for how long -- the panel invents
    // none of it; every field is projected and bounded by the server.
    await panel.page.locator('.woa-step__line').first().click()
    const detail = panel.page.locator('.woa-step__detail').first()
    await detail.waitFor()
    await expect(detail).toContainText('query_quotations')
    await expect(detail).toContainText('limit=50')
    await expect(detail).toContainText(`${labels.stepDuration}3s`)
    expect(panel.errors).toEqual([])
  })

  test('a stop is acknowledged at once, and a stopped turn is not marked as a failure', async ({open}) => {
    // The stop lands at the next safe point, which can be seconds away; until then the line
    // says the press was heard. A turn the person ended gets a grey stop mark, not a red
    // cross: nothing went wrong.
    const steps = [
      entry('e1', 'goal', '列出所有待开票的销售订单', {at: 1_757_000_000_000}),
      entry('e2', 'progress', '正在思考', {tone: 'active', at: 1_757_000_001_000}),
      entry('e3', 'progress', '正在读取业务数据', {
        tone: 'active', at: 1_757_000_002_000, step: {stage: 'tool', tool: 'query_sale_orders'}}),
    ]
    const panel = await open(snapshot({
      connection: {state: 'acting', busy: true, stopping: false, online: true}, entries: steps}))
    const page = panel.page
    await page.locator('.woa-launcher').click()
    const live = page.locator('.woa-work__live')
    await expect(live.locator('.woa-work__now')).toHaveText('正在读取业务数据')
    await panel.set({connection: {state: 'stopping', busy: true, stopping: true, online: true},
                     entries: steps})
    await expect(live).toHaveAttribute('data-stopping', 'true')
    await expect(live.locator('.woa-work__now')).toHaveText(labels.statusStopping)

    await panel.set({connection: {state: 'cancelled', busy: false, stopping: false, online: true},
                     entries: [...steps, entry('e4', 'result', '已按你的要求停止。', {
                       panel_state: 'cancelled', authored: 'server', at: 1_757_000_004_000})]})
    const summary = page.locator('.woa-work__summary')
    await expect(summary).toHaveAttribute('data-stopped', 'true')
    await expect(summary).not.toHaveAttribute('data-failed', 'true')
    await expect(summary.locator('svg').first()).toHaveCSS('color', rgb(tokens.themes.light['ink-faint']))
    await summary.click()
    await expect(page.locator(".woa-step[data-status='stopped']")).toHaveCount(1)
    await expect(page.locator(".woa-step[data-status='failed']")).toHaveCount(0)
    expect(panel.errors).toEqual([])
  })

  test('a turn that ended badly marks the step it ended on', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '把这张报价单确认掉', {at: 1_757_000_000_000}),
      entry('e2', 'progress', '正在读取业务数据', {
        tone: 'active', at: 1_757_000_001_000, step: {stage: 'tool', tool: 'query_quotations'}}),
      entry('e3', 'progress', '正在准备这次更改', {
        tone: 'active', at: 1_757_000_003_000, step: {stage: 'tool', tool: 'confirm_quotation'}}),
      entry('e4', 'result', '你的账号没有执行这项操作的权限。',
            {panel_state: 'refused', at: 1_757_000_004_000}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    // Folded, the line already says it ended badly; opened, it says where.
    const summary = panel.page.locator('.woa-work__summary')
    await expect(summary).toHaveAttribute('data-failed', 'true')
    await summary.click()
    await panel.page.locator('.woa-step').first().waitFor()
    await expect(panel.page.locator(".woa-step[data-status='failed']")).toHaveCount(1)
    await expect(panel.page.locator(".woa-step[data-status='failed'] .woa-step__text"))
      .toHaveText('正在准备这次更改')
    await expect(panel.page.locator(".woa-step[data-status='done']")).toHaveCount(1)
    expect(panel.errors).toEqual([])
  })

  test('elapsed time rolls over without showing sixty seconds', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '打开客户', {at: 1_757_000_000_000}),
      entry('e2', 'action', 'query_customers', {tone: 'active', at: 1_757_000_001_000}),
      entry('e3', 'result', '已打开。', {panel_state: 'success', at: 1_757_000_119_600}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    const elapsed = await panel.page.locator('.woa-took').innerText()
    expect(elapsed).toContain('2m 0s')
    expect(elapsed).not.toContain('60s')
    expect(panel.errors).toEqual([])
  })

  test('the newest line stays in view without being asked', async ({open}) => {
    // Arriving content is not a reason to stop following it.
    const lines = Array.from({length: 40}, (_, index) => `第 ${index} 行。`).join('\n')
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出所有待开票的销售订单'),
      entry('e2', 'result', lines, {panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-thread').waitFor()
    await panel.page.waitForTimeout(400)
    await expect(panel.page.locator('.woa-jump')).toHaveCount(0)

    // The echo of a message that has not reached the server yet grows the thread without
    // adding an entry. That is exactly the growth the old effect could not see, and it is
    // what left the reader parked above a button.
    await panel.set({optimistic: {turnId: 't2', text: '再列一次', failed: false}})
    await panel.page.waitForTimeout(400)
    await expect(panel.page.locator('.woa-jump')).toHaveCount(0)
    const distance = await panel.page.locator('.woa-thread').evaluate(
      node => node.scrollHeight - node.scrollTop - node.clientHeight)
    expect(distance).toBeLessThanOrEqual(30)
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

    await panel.page.locator('.woa-jump').click()
    await panel.page.waitForTimeout(500)
    const turn = await panel.page.locator('.woa-turn').last().boundingBox()
    const composer = await panel.page.locator('.woa-composer').boundingBox()
    expect(composer!.y - (turn!.y + turn!.height)).toBeGreaterThanOrEqual(85)
    expect(panel.errors).toEqual([])
  })

  test('the jump control waits until the reader is meaningfully away', async ({open}) => {
    const answer = Array.from({length: 80}, (_, index) => `第 ${index} 行内容。`).join('\n\n')
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '给我一份长清单'),
      entry('e2', 'result', answer, {panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    const thread = panel.page.locator('.woa-thread')
    await panel.page.waitForTimeout(300)

    const exact = await thread.evaluate(node => {
      node.scrollTop = node.scrollHeight - node.clientHeight - 160
      return node.scrollHeight - node.scrollTop - node.clientHeight
    })
    await panel.page.waitForTimeout(100)
    expect(exact).toBe(160)
    await expect(panel.page.locator('.woa-jump')).toHaveCount(0)

    const beyond = await thread.evaluate(node => {
      node.scrollTop = node.scrollHeight - node.clientHeight - 161
      return node.scrollHeight - node.scrollTop - node.clientHeight
    })
    await panel.page.waitForTimeout(100)
    expect(beyond).toBe(161)
    await panel.page.locator('.woa-jump').waitFor({timeout: 3000})
    expect(panel.errors).toEqual([])
  })

  test('the keyboard scroll target uses a quiet focus boundary', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出客户'),
      entry('e2', 'result', 'Acme', {panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    const thread = panel.page.locator('.woa-thread')
    // Opening puts the caret in the composer. Reverse Tabs -- past the answer's own
    // controls -- reach the scrollable transcript and its keyboard-only focus treatment.
    const focusedThread = () => thread.evaluate(
      node => node === (node.getRootNode() as ShadowRoot).activeElement)
    for (let step = 0; step < 6 && !(await focusedThread()); step += 1) {
      await panel.page.keyboard.press('Shift+Tab')
    }
    expect(await focusedThread()).toBe(true)
    const style = await thread.evaluate(node => {
      const css = getComputedStyle(node)
      return {outline: css.outlineStyle, shadow: css.boxShadow}
    })
    expect(style.outline).toBe('none')
    expect(style.shadow).not.toBe('none')
    expect(panel.errors).toEqual([])
  })

  test('a crash inside the panel never reaches the page', async ({open}) => {
    // The panel has no iframe, so its realm is the host application's realm.
    //
    // Anything it lets escape is caught by whatever the host application installed -- Odoo
    // answers with its own "something went wrong" dialog. Containment has to be at the
    // panel's own edges; installing a global handler would be the same trespass in the
    // other direction.
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    await panel.page.evaluate(() => {
      (window as unknown as {__host: {snapshot: () => unknown}}).__host.snapshot = () => {
        throw new Error('the panel failed')
      }
    })
    await panel.set({})
    await panel.page.waitForTimeout(400)
    expect(panel.errors).toEqual([])
    // And it says so, rather than vanishing from the page without explanation.
    const fatal = panel.page.locator('.woa-fatal')
    await fatal.waitFor({timeout: 4000})
    await expect(fatal).toBeVisible()
  })

  test('the panel installs no global error handler', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    expect(await panel.page.evaluate(
      () => (window as unknown as {__onerrorIsOurs: () => boolean}).__onerrorIsOurs())).toBe(true)
    expect(panel.errors).toEqual([])
  })

  test('an undelivered message carries its own retry', async ({open}) => {
    const panel = await open(snapshot({
      optimistic: {turnId: 'turn-9', text: '再带上金额', failed: true}}))
    await panel.page.locator('.woa-launcher').click()
    const undelivered = panel.page.locator('.woa-undelivered')
    await undelivered.waitFor()
    await expect(undelivered).toContainText(labels.undelivered)
    await undelivered.locator('button').click()
    expect((await panel.calls()).map(call => call[0])).toEqual(['retrySubmit'])
    expect(panel.errors).toEqual([])
  })
})

test.describe('states and layout', () => {
  test('the title bar glyphs say what they do when pointed at or reached by keyboard', async ({open}) => {
    const panel = await open(snapshot())
    const page = panel.page
    await page.locator('.woa-launcher').click()
    await panel.settle()
    const tip = (label: string) => page.getByRole('button', {name: label}).evaluate(node => {
      const after = getComputedStyle(node, '::after')
      const svg = node.querySelector('svg')!
      return {text: after.content, shown: after.opacity === '1',
              // Lucide draws on a 24px grid, so this is the line in CSS pixels.
              ink: Number(svg.getAttribute('stroke-width')) * Number(svg.getAttribute('width')) / 24,
              title: node.getAttribute('title')}
    })
    for (const label of [labels.newChat, labels.dockDrawer]) {
      const idle = await tip(label)
      // The server's words, drawn by the panel -- not the browser's `title`, which waits a
      // second and looks like no part of the panel.
      expect(idle.text, label).toBe(`"${label}"`)
      expect(idle.shown, label).toBe(false)
      expect(idle.title, label).toBeNull()
      expect(idle.ink, label).toBeCloseTo(1.25)
      await page.getByRole('button', {name: label}).hover()
      await expect.poll(async () => (await tip(label)).shown, {timeout: 2000}).toBe(true)
    }
    // The cross says nothing: closing is what a cross in a window's corner means.
    const cross = await tip(labels.collapse)
    expect(cross.text).toBe('none')
    expect(cross.ink).toBeCloseTo(1.25)
    await page.mouse.move(0, 0)
    await page.locator('#woa-composer-input').focus()
    for (let step = 0; step < 12 && (await panel.active()).label !== labels.dockDrawer; step += 1) {
      await page.keyboard.press('Shift+Tab')
    }
    expect((await panel.active()).label).toBe(labels.dockDrawer)
    await expect.poll(async () => (await tip(labels.dockDrawer)).shown, {timeout: 2000}).toBe(true)
    expect(panel.errors).toEqual([])
  })

  test('every panel state in the contract renders', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    const window = panel.page.locator('.woa-window')
    await window.waitFor()
    for (const state of Object.keys(CONFIGURATION.panel_states)) {
      await panel.set({connection: {state, busy: state === 'acting',
                                    stopping: state === 'stopping', online: true}})
      await expect(window).toHaveAttribute('data-state', state)
      await expect(window, state).toBeVisible()
    }
    expect(panel.errors).toEqual([])
  })

  test('the palette actually reaches the shadow root', async ({open}) => {
    // The tokens have to be declared on `:host`, not `:root`.
    //
    // Written after watching the panel render completely unstyled while every behavioural
    // test stayed green: inside a shadow root `:root` matches nothing, so the whole palette
    // silently resolved to the initial value of each property. Structure and behaviour
    // tests cannot see that, so this one reads computed style.
    const panel = await open(snapshot({entries: [entry('e1', 'goal', '列出订单')]}))
    await panel.page.locator('.woa-launcher').click()
    const window = panel.page.locator('.woa-window')
    await window.waitFor()
    for (const name of ['accent', 'ink', 'bg', 'line', 'warning'] as const) {
      expect(await window.evaluate(
        (node, token) => getComputedStyle(node).getPropertyValue(`--woa-${token}`).trim(), name),
      name).toBe(tokens.themes.light[name])
    }
    expect(await window.evaluate(node => getComputedStyle(node).fontSize))
      .toBe(tokens.scale['font-size'])
    // The host application's own type must not reach in.
    await panel.page.evaluate(() => {
      document.documentElement.style.fontSize = '31px'
      document.body.style.letterSpacing = '4px'
    })
    expect(await window.evaluate(node => getComputedStyle(node).fontSize))
      .toBe(tokens.scale['font-size'])
    expect(panel.errors).toEqual([])
  })

  test('an explicit dark theme on the document reaches the panel', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
    await panel.page.locator('.woa-launcher').click()
    const window = panel.page.locator('.woa-window')
    await window.waitFor()
    const background = () => window.evaluate(
      node => getComputedStyle(node).getPropertyValue('--woa-bg').trim())
    expect(await background()).toBe(tokens.themes.dark.bg)
    await panel.page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
    expect(await background()).toBe(tokens.themes.light.bg)
    expect(panel.errors).toEqual([])
  })

  test('long values wrap instead of widening the window', async ({open}) => {
    const longName = 'Nordwind Handelsgesellschaft mbH & Co. Kommanditgesellschaft '.repeat(4)
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', longName),
      entry('e2', 'result', 'a'.repeat(900), {authored: 'server', panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-user').waitFor()
    const window = (await panel.page.locator('.woa-window').boundingBox())!
    for (const selector of ['.woa-user', '.woa-md']) {
      const box = (await panel.page.locator(selector).first().boundingBox())!
      expect(box.x + box.width, selector).toBeLessThanOrEqual(window.x + window.width + 1)
    }
    expect(await panel.page.locator('.woa-thread').evaluate(
      node => node.scrollWidth > node.clientWidth + 1)).toBe(false)
    expect(panel.errors).toEqual([])
  })

  test('reduced motion stops every animation', async ({open}) => {
    const panel = await open(snapshot({
      connection: {state: 'acting', busy: true, stopping: false, online: true},
      entries: [entry('e1', 'goal', '列出订单'), entry('e2', 'action', '重新检查当前页面')]}),
    {reducedMotion: 'reduce'})
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-spinner').waitFor()
    const durations = await panel.page.locator('.woa-window').evaluate(
      node => [...node.querySelectorAll('*')].map(element =>
        getComputedStyle(element).animationDuration).filter(Boolean))
    expect(durations.length).toBeGreaterThan(0)
    // Chromium reports the 0.001ms floor as 1e-06s.
    expect(durations.every(value => parseFloat(value) < 0.01), durations.join(', ')).toBe(true)
    expect(panel.errors).toEqual([])
  })
  test('a new conversation clears the view and tells the server', async ({open}) => {
    // Starting over empties what the panel shows, and says so to the server.
    //
    // Only the *view* clears. The session's event log is what happened and the panel has no
    // way to delete it -- which is why this asserts the call was made rather than asserting
    // anything was erased anywhere else.
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '把报价单 S00002 确认成销售订单'),
      entry('e2', 'result', '报价单 S00002 已确认为销售订单。',
            {authored: 'server', panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-window').waitFor()
    await expect(panel.page.locator('.woa-thread')).toContainText('S00002')

    await panel.page.getByRole('button', {name: labels.newChat}).click()
    await panel.page.locator('.woa-empty').waitFor({timeout: 3000})
    // Assert on the turns, not on the thread's text: a substring check passes for the wrong
    // reason whenever the empty state happens to mention the same words.
    await expect(panel.page.locator('.woa-turns')).toHaveCount(0)
    await expect(panel.page.locator('.woa-user')).toHaveCount(0)
    expect((await panel.calls()).map(call => call[0])).toEqual(['newConversation'])
    expect(panel.errors).toEqual([])
  })

  test('the composer grows and only scrolls once it has stopped growing', async ({open}) => {
    // A single line must not be sitting next to a scrollbar.
    //
    // The box sizes itself to its content, so anything left over is sub-pixel: at 13px with
    // a line height of 1.5 the rounding is half a pixel, which is enough for a permanent
    // scrollbar on the machines that do not use overlay ones.
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    const field = panel.page.locator('.woa-field textarea')
    await field.waitFor()

    const oneLine = await field.evaluate(node => ({
      overflow: getComputedStyle(node).overflowY,
      overflowing: node.scrollHeight > node.clientHeight + 1,
      ceiling: parseFloat(getComputedStyle(node).maxHeight),
      height: node.clientHeight}))
    expect(oneLine.overflow).toBe('hidden')
    expect(oneLine.overflowing, JSON.stringify(oneLine)).toBe(false)

    await field.fill(Array.from({length: 40}, (_, index) => `第 ${index} 行`).join('\n'))
    const tall = await field.evaluate(node => ({
      overflow: getComputedStyle(node).overflowY,
      height: node.clientHeight,
      ceiling: parseFloat(getComputedStyle(node).maxHeight)}))
    // Past the ceiling it scrolls, and it stops at exactly the height the stylesheet
    // declares -- the value the component reads back rather than repeating.
    expect(tall.overflow).toBe('auto')
    // clientHeight is a whole number of pixels; the ceiling is whole lines of a 22.4px line.
    expect(tall.height).toBeLessThanOrEqual(Math.ceil(tall.ceiling))
    expect(tall.height).toBeGreaterThan(oneLine.height)
    expect(tall.ceiling).toBe(oneLine.ceiling)
    expect(panel.errors).toEqual([])
  })

  test('the title bar controls still work while it is also the drag handle', async ({open}) => {
    // A window you can drag by its title bar must still have usable buttons in it.
    const panel = await open(snapshot({entries: [entry('e1', 'goal', '列出订单')]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-window').waitFor()
    await panel.page.getByRole('button', {name: labels.newChat}).click()
    expect((await panel.calls()).map(call => call[0])).toEqual(['newConversation'])
    await panel.page.getByRole('button', {name: labels.collapse}).click()
    await panel.page.locator('.woa-launcher').waitFor({timeout: 3000})
    await expect(panel.page.locator('.woa-window')).toHaveCount(0)
    expect(panel.errors).toEqual([])
  })

  test('a window nobody has moved opens out of the launcher', async ({open}) => {
    // The window replaces the button that was pressed, so it appears where that was.
    //
    // Its bottom-right corner sits on the launcher's, measured on the page as shipped, and
    // the composer is wholly inside the viewport.
    const panel = await open(snapshot(), {width: 1440, height: 900})
    await panel.settle()
    const launcher = (await panel.page.locator('.woa-launcher').boundingBox())!
    await panel.page.locator('.woa-launcher').click()
    await panel.settle()
    const window = (await panel.page.locator('.woa-window').boundingBox())!
    const composer = (await panel.page.locator('textarea').boundingBox())!
    expect(Math.abs(window.x + window.width - (launcher.x + launcher.width))).toBeLessThanOrEqual(0.5)
    expect(Math.abs(window.y + window.height - (launcher.y + launcher.height))).toBeLessThanOrEqual(0.5)
    expect(composer.y + composer.height).toBeLessThanOrEqual(900)
    expect(panel.errors).toEqual([])
  })

  test('a drawer narrows the page and gives its width back', async ({open}) => {
    // The drawer takes the right edge at full height, and the page makes room for it.
    //
    // The host page declares `html, body { width: 100% }`, as Odoo does: a drawer that only
    // added a margin would push that page into a horizontal scroll.
    const panel = await open(snapshot(), {width: 1440, height: 900})
    await panel.page.addStyleTag({content: 'html, body { position: relative; width: 100%; }'})
    await panel.page.locator('.woa-launcher').click()
    await panel.page.getByRole('button', {name: labels.dockDrawer}).click()
    await panel.settle()
    const boxes = await layout(panel.page)
    expect(boxes.mode).toBe('drawer')
    expect(Math.abs(boxes.window!.x + boxes.window!.w - boxes.view)).toBeLessThanOrEqual(0.5)
    expect([boxes.window!.y, boxes.window!.h]).toEqual([0, 900])
    expect(Math.abs(boxes.page - (boxes.view - boxes.window!.w))).toBeLessThanOrEqual(0.5)
    expect(boxes.overflow).toBeLessThanOrEqual(0)

    // Widened from the edge that faces the page; the page follows, and the width is kept.
    const edge = boxes.window!.x + 2
    await panel.page.mouse.move(edge, 450)
    await panel.page.mouse.down()
    await panel.page.mouse.move(edge - 100, 450, {steps: 4})
    await panel.page.mouse.up()
    const widened = await layout(panel.page)
    expect(Math.abs(widened.window!.w - (boxes.window!.w + 100))).toBeLessThanOrEqual(1)
    expect(Math.abs(widened.page - (widened.view - widened.window!.w))).toBeLessThanOrEqual(0.5)

    // Closing gives the page its whole width back and leaves its root as it was.
    await panel.page.getByRole('button', {name: labels.collapse}).click()
    await panel.page.locator('.woa-launcher').waitFor({timeout: 3000})
    const closed = await layout(panel.page)
    expect(closed.page).toBe(closed.view)
    expect(closed.rootStyle).toBeNull()

    // Reopened, it is still a drawer at the width it was given.
    await panel.page.locator('.woa-launcher').click()
    await panel.settle()
    const reopened = await layout(panel.page)
    expect(reopened.mode).toBe('drawer')
    expect(Math.abs(reopened.window!.w - widened.window!.w)).toBeLessThanOrEqual(1)

    // Switching back floats the window from its defaults, on the launcher's corner.
    await panel.page.getByRole('button', {name: labels.dockFloat}).click()
    await panel.settle()
    const floating = await layout(panel.page)
    expect(floating.mode).toBe('float')
    expect(floating.page).toBe(floating.view)
    expect(floating.window!.w).toBe(420)
    expect(panel.errors).toEqual([])
  })

  test('a drawer covers a page too narrow to share', async ({open}) => {
    const panel = await open(snapshot(), {width: 800, height: 600})
    await panel.page.locator('.woa-launcher').click()
    await panel.page.getByRole('button', {name: labels.dockDrawer}).click()
    await panel.settle()
    const boxes = await layout(panel.page)
    expect(boxes.mode).toBe('drawer')
    expect(boxes.page).toBe(boxes.view)
    expect(boxes.rootStyle).toBeNull()
    expect(Math.abs(boxes.window!.x + boxes.window!.w - boxes.view)).toBeLessThanOrEqual(0.5)
    expect(panel.errors).toEqual([])
  })

  test('a control calls its host as a method and never escapes the panel', async ({open}) => {
    // Two failures, one click.
    //
    // `onClick={host.newConversation}` hands React a bare reference; the real host is a
    // class, so it lost its receiver and threw. With no iframe the TypeError landed in the
    // host application's realm, and Odoo raised its own error dialog over a page nothing had
    // gone wrong on.
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出所有待开票的销售订单'),
      entry('e2', 'result', '十六张。', {panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-turn').waitFor()
    await panel.page.getByLabel(labels.newChat).click()
    expect((await panel.calls()).map(call => call[0])).toEqual(['newConversation'])
    await expect(panel.page.locator('.woa-turn')).toHaveCount(0)
    expect(panel.errors).toEqual([])
  })

  test("a host that throws is the panel's problem, not the application's", async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    await panel.page.evaluate(() => {
      (window as unknown as {__host: {newConversation: () => void}}).__host.newConversation =
        () => { throw new Error('the host failed') }
    })
    await panel.page.getByLabel(labels.newChat).click()
    const notice = panel.page.locator('.woa-inline-error')
    await notice.waitFor()
    await expect(notice).toContainText(notices.unexpected!)
    // Nothing reached the page: the host application never hears about this.
    expect(panel.errors).toEqual([])
  })

  test('the face is the logo, and stopping stays legible under the pointer', async ({open}) => {
    // The assistant's face is the launcher and the title bar, from the first moment to the
    // last. It is in the bundle itself: it loads with no request of its own.
    const panel = await open(snapshot())
    const loaded = (scope: string) => panel.page.locator(scope)
      .evaluate((node: HTMLImageElement) => node.naturalWidth)
    expect(await loaded('.woa-launcher__disc img')).toBe(192)
    // Drawn, not a person: over the host page the face says it is an AI.
    await expect(panel.page.locator('.woa-launcher .woa-ai-tag')).toHaveText(labels.aiTag)
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-empty').waitFor()
    await expect(panel.page.locator('.woa-brand__face')).toBeVisible()
    expect(await loaded('.woa-brand__face')).toBe(192)
    // Inside the panel the name beside the face already says so.
    await expect(panel.page.locator('.woa-titlebar .woa-ai-tag')).toHaveCount(0)
    await panel.set({connection: {state: 'acting', busy: true, stopping: false, online: true}})
    const stop = panel.page.getByRole('button', {name: labels.stop})
    const paint = () => stop.evaluate(node => {
      const style = getComputedStyle(node)
      return {background: style.backgroundColor, color: style.color}
    })
    // The send control becomes the stop control in place, so its colours ease across.
    await expect.poll(paint).toEqual({background: rgb(tokens.themes.light.accent),
                                      color: rgb(tokens.themes.light['accent-ink'])})
    // Under the pointer it deepens; its square never takes the colour of its own disc.
    await stop.hover()
    await expect.poll(paint).toEqual({background: rgb(tokens.themes.light['accent-hover']),
                                      color: rgb(tokens.themes.light['accent-ink'])})
    expect(panel.errors).toEqual([])
  })

  test('on a phone the window is the whole screen', async ({open}) => {
    // A floating window and its margins are wider than a phone held upright. There the
    // window covers the screen in either mode, neither moves nor resizes, and offers no
    // mode to switch to; the text box is large enough that focusing it does not zoom.
    const panel = await open(snapshot(), {width: 375, height: 667})
    const page = panel.page
    await page.locator('.woa-launcher').click()
    const window = page.locator('.woa-window')
    await window.waitFor()
    await window.evaluate(node => node.getAnimations().forEach(animation => animation.finish()))
    expect(await window.getAttribute('data-mode')).toBe('full')
    // The screen inside the page's own scrollbar, where a browser draws one.
    const screen = () => page.evaluate(() => ({
      x: 0, y: 0,
      width: document.documentElement.clientWidth, height: document.documentElement.clientHeight}))
    const upright = await screen()
    expect(await window.boundingBox()).toEqual(upright)
    await expect(page.getByRole('button', {name: labels.dockDrawer})).toHaveCount(0)
    await expect(page.locator('.woa-handle')).toHaveCount(0)
    expect(await page.locator('#woa-composer-input').evaluate(node => getComputedStyle(node).fontSize))
      .toBe(tokens.scale['font-size-field-touch'])
    await page.mouse.move(120, 20)
    await page.mouse.down()
    await page.mouse.move(40, 200, {steps: 4})
    await page.mouse.up()
    expect(await window.boundingBox()).toEqual(upright)
    // Turned on its side the screen is too short for a window as well.
    await page.setViewportSize({width: 667, height: 375})
    const sideways = await screen()
    await expect.poll(() => window.boundingBox()).toEqual(sideways)
    // Given room again, the window floats as it did, and can be switched once more.
    await page.setViewportSize({width: 1280, height: 800})
    await expect(window).toHaveAttribute('data-mode', 'float')
    await expect(page.getByRole('button', {name: labels.dockDrawer})).toHaveCount(1)
    expect((await window.boundingBox())!.width).toBeLessThan(1280)
    expect(panel.errors).toEqual([])
  })

  test('the conversation does not narrow when it grows past one screen', async ({open}) => {
    // Where the scrollbar takes room it keeps it taken: appearing, it re-wrapped every line.
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出所有待开票的销售订单'),
      entry('e2', 'result', '十六张。', {panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    const turns = panel.page.locator('.woa-turns')
    await turns.waitFor()
    await panel.page.locator('.woa-window')
      .evaluate(node => node.getAnimations().forEach(animation => animation.finish()))
    const before = (await turns.boundingBox())!.width
    const lines = Array.from({length: 60}, (_, index) => `第 ${index} 行。`).join('\n\n')
    await panel.set({entries: [
      entry('e1', 'goal', '列出所有待开票的销售订单'),
      entry('e2', 'result', lines, {panel_state: 'success'}),
    ]})
    const thread = panel.page.locator('.woa-thread')
    await expect.poll(() => thread.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true)
    expect((await turns.boundingBox())!.width).toBe(before)
    expect(panel.errors).toEqual([])
  })

  test('the window moves and resizes and stays in the viewport', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    const window = panel.page.locator('.woa-window')
    await window.waitFor()
    // The opening animation transforms the measured box; dragging before it settles can
    // press outside the title bar under a busy full-suite browser run.
    await window.evaluate(node => node.getAnimations().forEach(animation => animation.finish()))
    const before = (await window.boundingBox())!
    await panel.page.mouse.move(before.x + before.width / 2, before.y + 20)
    await panel.page.mouse.down()
    await panel.page.mouse.move(before.x + before.width / 2 - 160, before.y - 60, {steps: 6})
    await panel.page.mouse.up()
    const moved = (await window.boundingBox())!
    expect(moved.x).toBeLessThan(before.x - 100)
    expect(moved.y).toBeLessThan(before.y)

    await panel.page.mouse.move(moved.x + 1, moved.y + moved.height / 2)
    await panel.page.mouse.down()
    await panel.page.mouse.move(moved.x - 120, moved.y + moved.height / 2, {steps: 6})
    await panel.page.mouse.up()
    const widened = (await window.boundingBox())!
    expect(widened.width).toBeGreaterThan(moved.width + 60)

    // A viewport that shrinks pulls the window back in rather than leaving it outside.
    await panel.page.setViewportSize({width: 420, height: 420})
    await panel.page.waitForTimeout(120)
    const clamped = (await window.boundingBox())!
    expect(clamped.x + clamped.width).toBeLessThanOrEqual(421)
    expect(clamped.y + clamped.height).toBeLessThanOrEqual(421)
    expect(clamped.width).toBeGreaterThanOrEqual(320)
    expect(panel.errors).toEqual([])
  })
})

/** Where the window is, which mode it is in, and what it left of the host page. */
function layout(page: Page) {
  return page.evaluate(() => {
    const panel = document.getElementById('woa-assistant')!.shadowRoot!
      .querySelector<HTMLElement>('.woa-window')
    const box = panel?.getBoundingClientRect()
    return {
      mode: panel?.dataset.mode ?? null,
      window: box ? {x: box.x, y: box.y, w: box.width, h: box.height} : null,
      page: document.body.getBoundingClientRect().width,
      // The viewport inside the page's own scrollbar: what the panel may lay itself out in.
      view: document.documentElement.clientWidth,
      overflow: document.documentElement.scrollWidth - innerWidth,
      rootStyle: document.documentElement.getAttribute('style'),
    }
  })
}
// ------------------------------------------------------------------------------------------
// U07, the part a machine can decide.
//
// Layout at 320 px, layout at 200 % zoom, both colour schemes, reduced motion, and whether the
// keyboard reaches the composer and both confirmation answers in a sensible order with a
// visible focus ring. 200 % zoom is emulated the way a browser produces it -- half the CSS
// viewport at twice the device pixel ratio -- which is the layout a 1280 x 800 window shows
// when zoomed, not the browser's zoom control itself.
//
// None of this is a screen-reader test. The DOM roles and names checked at the end are what a
// reader needs to be handed; whether it announces them sensibly is a person's judgement and
// stays with the manual acceptance steps.

const LONG_QUESTION = ('请把 Nordwind Handelsgesellschaft mbH & Co. Kommanditgesellschaft 名下所有' +
  '未确认的报价单按金额从高到低列出来，并说明每一张的客户、币种和创建日期，' +
  '如果有并列金额的也全部列出。').repeat(2)

const VIEWPORTS = [
  {name: '320px', width: 320, height: 568, scale: 1},
  {name: '200pct-zoom', width: 640, height: 400, scale: 2},
] as const

function conversation(): Snapshot {
  return snapshot({entries: [
    entry('e1', 'goal', LONG_QUESTION),
    entry('e2', 'result', '找到 2 张：\n\n| 单号 | 金额 |\n|---|---|\n' +
          '| S00002 | 2947.50 USD |\n| S00007 | 1180.00 USD |',
          {authored: 'server', panel_state: 'success'}),
  ]})
}

async function shot(page: Page, name: string): Promise<void> {
  const directory = process.env.WOA_U07_SCREENSHOT_DIR
  if (directory) await page.screenshot({path: `${directory}/${name}.png`})
}

function luminance(rgb: number[]): number {
  const [red, green, blue] = rgb.map(channel => {
    const scaled = channel / 255
    return scaled <= 0.04045 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4
  }) as [number, number, number]
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue
}

/** WCAG contrast of a computed `rgb()`/`rgba()` foreground over an opaque background. */
function contrast(foreground: string, background: string): number {
  const parse = (value: string) => {
    const numbers = (value.match(/[\d.]+/g) ?? []).map(Number)
    return {rgb: numbers.slice(0, 3), alpha: numbers.length > 3 ? numbers[3]! : 1}
  }
  const back = parse(background).rgb
  const {rgb: fore, alpha} = parse(foreground)
  const blended = fore.map((value, index) => alpha * value + (1 - alpha) * back[index]!)
  const [lighter, darker] = [luminance(blended), luminance(back)].sort((a, b) => b - a) as
    [number, number]
  return (lighter + 0.05) / (darker + 0.05)
}

/**
 * The colour an element's text is drawn in, and the first opaque background behind it --
 * walking out of the shadow root to its host if need be.
 */
function colours(page: Page, selector: string, pseudo = '', property = 'color') {
  return page.evaluate(([selector, pseudo, property]) => {
    const root = document.querySelector('#woa-assistant')!.shadowRoot!
    const node = root.querySelector(selector!)
    if (!node) return null
    const parent = (element: Element): Element | null => element.parentElement
      || (element.getRootNode() instanceof ShadowRoot
        ? (element.getRootNode() as ShadowRoot).host : null)
    let background = 'rgb(255, 255, 255)'
    for (let at: Element | null = node; at; at = parent(at)) {
      const value = getComputedStyle(at).backgroundColor
      const parts = value.match(/[\d.]+/g) ?? []
      if (parts.length >= 3 && (parts.length < 4 || parseFloat(parts[3]!) > 0.99)) {
        background = value
        break
      }
    }
    const style = getComputedStyle(node, pseudo || null) as unknown as Record<string, string>
    return {fg: style[property!]!, bg: background}
  }, [selector, pseudo, property])
}

/**
 * Whether a control is inside the viewport and inside the window, and whether the point a
 * person would press actually lands on it rather than on something drawn over it.
 */
async function expectReachable(page: Page, selector: string, where: string): Promise<void> {
  const facts = await page.evaluate(selector => {
    const root = document.querySelector('#woa-assistant')!.shadowRoot!
    const node = root.querySelector(selector)
    if (!node) return null
    const box = node.getBoundingClientRect()
    const frame = root.querySelector('.woa-window')!.getBoundingClientRect()
    const hit = root.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)
    return {
      width: box.width, height: box.height,
      in_viewport: box.left >= 0 && box.top >= 0
        && box.right <= innerWidth + 0.5 && box.bottom <= innerHeight + 0.5,
      in_window: box.left >= frame.left - 0.5 && box.top >= frame.top - 0.5
        && box.right <= frame.right + 0.5 && box.bottom <= frame.bottom + 0.5,
      unobscured: Boolean(hit) && (hit === node || node.contains(hit)),
    }
  }, selector)
  expect(facts, `${selector} is missing at ${where}`).not.toBeNull()
  for (const fact of ['in_viewport', 'in_window', 'unobscured'] as const) {
    expect(facts![fact], `${selector} is not ${fact} at ${where}: ${JSON.stringify(facts)}`)
      .toBe(true)
  }
  expect(Math.min(facts!.width, facts!.height),
         `${selector} is too small to press at ${where}`).toBeGreaterThanOrEqual(24)
}

/** The focused control inside the panel, with how its focus is drawn. */
function focused(page: Page) {
  return page.evaluate(() => {
    const active = document.querySelector('#woa-assistant')!.shadowRoot!.activeElement
    if (!active) return null
    const style = getComputedStyle(active)
    return {id: active.id, testid: active.getAttribute('data-testid') || '',
            label: active.getAttribute('aria-label') || (active.textContent ?? '').trim(),
            focus_visible: active.matches(':focus-visible'),
            outline_style: style.outlineStyle, outline_width: parseFloat(style.outlineWidth),
            outline_color: style.outlineColor, box_shadow: style.boxShadow}
  })
}

/** Nothing a person needs is clipped by the window or drawn over by something else. */
async function expectLaidOut(page: Page, where: string): Promise<void> {
  const facts = await page.evaluate(() => {
    const root = document.querySelector('#woa-assistant')!.shadowRoot!
    type Box = {top: number; bottom: number; left: number; right: number}
    const rect = (selector: string): Box | null => {
      const node = root.querySelector(selector)
      return node ? node.getBoundingClientRect().toJSON() as Box : null
    }
    const heading = root.querySelector('.woa-approval h2')
    let heading_hit: boolean | null = null
    if (heading) {
      const box = heading.getBoundingClientRect()
      const hit = root.elementFromPoint(box.left + 24, box.top + box.height / 2)
      heading_hit = Boolean(hit) && heading.contains(hit)
    }
    // Where the thread is actually painted: its own box, cut by the shell around it only if
    // that shell clips.
    const shell = root.querySelector('.woa-thread-shell')!
    const thread = rect('.woa-thread')!
    if (getComputedStyle(shell).overflowY !== 'visible') {
      thread.bottom = Math.min(thread.bottom, shell.getBoundingClientRect().bottom)
    }
    // Whether the card still has an edge beside and below its pinned answers: a real border,
    // and an answers bar that stays inside it.
    const cardNode = root.querySelector('.woa-approval')
    let edge = null
    if (cardNode) {
      const style = getComputedStyle(cardNode)
      const outer = cardNode.getBoundingClientRect()
      const bar = root.querySelector('.woa-approval__actions')!.getBoundingClientRect()
      const left = parseFloat(style.borderLeftWidth)
      const right = parseFloat(style.borderRightWidth)
      const bottom = parseFloat(style.borderBottomWidth)
      edge = {left, right, bottom,
              bar_inside: bar.left >= outer.left + left - 0.5
                && bar.right <= outer.right - right + 0.5
                && bar.bottom <= outer.bottom - bottom + 0.5}
    }
    const scrollers = [...root.querySelectorAll('*')].filter(node => {
      const style = getComputedStyle(node)
      // A text field scrolls its own value; what this counts is content panes.
      if (node.tagName === 'TEXTAREA' || node.tagName === 'INPUT') return false
      return /(auto|scroll)/.test(style.overflowY) && node.scrollHeight - node.clientHeight > 4
    }).map(node => node.className || node.tagName)
    const threadNode = root.querySelector('.woa-thread')
    return {window: rect('.woa-window')!, field: rect('.woa-field')!, edge, thread,
            card: rect('.woa-approval'), scrollers, actions: rect('.woa-approval__actions'),
            card_in_thread: Boolean(cardNode && threadNode && threadNode.contains(cardNode)),
            heading_hit}
  })
  const dump = JSON.stringify(facts)
  expect(facts.field.bottom, `the composer is clipped by the window at ${where}: ${dump}`)
    .toBeLessThanOrEqual(facts.window.bottom + 0.5)
  if (facts.card === null) return
  // The confirmation lives in the conversation, so the one thing that scrolls is the
  // conversation: the card used to be stacked under it, taking up to 62% of the window and
  // leaving the thread a sliver with a scrollbar of its own (owner, P7.5).
  expect(facts.card_in_thread, `the confirmation is not in the conversation at ${where}: ${dump}`)
    .toBe(true)
  expect(facts.thread.bottom, `the conversation runs under the composer at ${where}: ${dump}`)
    .toBeLessThanOrEqual(facts.field.top + 0.5)
  expect(facts.scrollers,
         `the conversation is not the only thing that scrolls at ${where}: ${dump}`)
    .toEqual(['woa-thread'])
  expect(facts.actions!.top, `the answers are scrolled above the conversation at ${where}: ${dump}`)
    .toBeGreaterThanOrEqual(facts.thread.top - 0.5)
  expect(facts.actions!.bottom, `the answers are below the conversation at ${where}: ${dump}`)
    .toBeLessThanOrEqual(facts.thread.bottom + 0.5)
  const edge = facts.edge!
  expect(Math.min(edge.left, edge.right, edge.bottom),
         `the confirmation has no edge at ${where}: ${dump}`).toBeGreaterThanOrEqual(1)
  expect(edge.bar_inside, `the pinned answers cover the confirmation's edge at ${where}: ${dump}`)
    .toBe(true)
}

test.describe('U07', () => {
  for (const {name, width, height, scale} of VIEWPORTS) {
    for (const scheme of ['light', 'dark'] as const) {
      const where = `${name}/${scheme}`
      test(`${where} keeps every control inside and unobscured`, async ({open}) => {
        const panel = await open(conversation(), {width, height, colorScheme: scheme,
                                                  deviceScaleFactor: scale})
        const page = panel.page
        await page.locator('.woa-launcher').click()
        await page.locator('#woa-composer-input').fill(LONG_QUESTION)
        await panel.settle()
        await expectLaidOut(page, where)
        const box = (await page.locator('.woa-window').boundingBox())!
        expect(box.x, where).toBeGreaterThanOrEqual(0)
        expect(box.y, where).toBeGreaterThanOrEqual(0)
        expect(box.x + box.width, where).toBeLessThanOrEqual(width + 1)
        expect(box.y + box.height, where).toBeLessThanOrEqual(height + 1)
        for (const selector of [`[aria-label="${labels.newChat}"]`,
                                `[aria-label="${labels.collapse}"]`,
                                '#woa-composer-input',
                                `[aria-label="${labels.send}"]`]) {
          await expectReachable(page, selector, where)
        }
        expect(await page.locator('.woa-thread').evaluate(
          node => node.scrollWidth > node.clientWidth + 1), where).toBe(false)
        expect(await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth + 1), where).toBe(false)
        await shot(page, `u07-${name}-${scheme}-composer`)

        await panel.raiseApproval({effect: '新建客户 Nordwind Handelsgesellschaft mbH & Co. KG',
                                   targets: ['res.partner · 新建一条客户记录',
                                             '邮箱 · buchhaltung@nordwind.example']})
        await panel.settle()
        await expectLaidOut(page, `${where} with a confirmation`)
        for (const selector of ['[data-testid="woa-deny"]', '[data-testid="woa-approve"]']) {
          await expectReachable(page, selector, `${where} with a confirmation`)
        }
        await expect(page.locator('.woa-approval__effect'), where).toBeVisible()
        await shot(page, `u07-${name}-${scheme}-confirmation`)
        expect(panel.errors, where).toEqual([])
      })
    }
  }

  for (const scheme of ['light', 'dark'] as const) {
    test(`the ${scheme} scheme follows the system and keeps text legible`, async ({open}) => {
      const text = ['.woa-user', '.woa-md', '#woa-composer-input', '.woa-composer__disclaimer',
                    '.woa-approval h2', '.woa-approval__effect',
                    '.woa-approval__targets li',
                    '[data-testid="woa-deny"]', '[data-testid="woa-approve"]']
      const panel = await open(conversation(), {colorScheme: scheme})
      const page = panel.page
      await page.locator('.woa-launcher').click()
      // Nothing on the document asked for a theme: the system preference alone has to reach
      // the shadow root.
      expect(await page.locator('.woa-window').evaluate(
        node => getComputedStyle(node).getPropertyValue('--woa-bg').trim()))
        .toBe(tokens.themes[scheme].bg)
      const placeholder = (await colours(page, '#woa-composer-input', '::placeholder'))!
      expect(contrast(placeholder.fg, placeholder.bg),
             `placeholder in ${scheme}: ${JSON.stringify(placeholder)}`).toBeGreaterThanOrEqual(4.5)
      await page.locator('#woa-composer-input').fill('列出订单')
      await panel.raiseApproval()
      for (const selector of text) {
        const pair = await colours(page, selector)
        expect(pair, selector).not.toBeNull()
        expect(contrast(pair!.fg, pair!.bg), `${selector} in ${scheme}: ${JSON.stringify(pair)}`)
          .toBeGreaterThanOrEqual(4.5)
      }
      // The focus ring is a non-text indicator: 3:1 against what it sits on.
      await page.keyboard.press('Tab')
      const active = (await focused(page))!
      const ring = (await colours(page, '.woa-approval'))!
      expect(contrast(active.outline_color, ring.bg),
             `focus ring in ${scheme}: ${JSON.stringify(active)}`).toBeGreaterThanOrEqual(3)
      expect(panel.errors).toEqual([])
    })
  }

  for (const preference of ['no-preference', 'reduce'] as const) {
    test(`reduced motion (${preference}) governs transitions as well as animations`, async ({open}) => {
      const panel = await open(conversation(), {reducedMotion: preference})
      await panel.page.locator('.woa-launcher').click()
      await panel.raiseApproval()
      const durations = await panel.page.locator('.woa-window').evaluate(
        node => [...node.querySelectorAll('*'), node].flatMap(element =>
          getComputedStyle(element).transitionDuration.split(',').map(parseFloat)))
      if (preference === 'reduce') {
        expect(durations.every(value => value < 0.01), durations.join(', ')).toBe(true)
      } else {
        // Otherwise the check above would pass on a panel with nothing to stop.
        expect(durations.some(value => value >= 0.05), durations.join(', ')).toBe(true)
      }
      expect(panel.errors).toEqual([])
    })
  }

  test('the keyboard reaches the composer and send in order with a visible ring', async ({open}) => {
    const panel = await open(conversation())
    const page = panel.page
    await page.keyboard.press('Tab')
    expect((await focused(page))!.label).toBe(labels.expand)
    await page.keyboard.press('Enter')
    await page.locator('.woa-window').waitFor()
    expect((await focused(page))!.id,
           'opening the panel puts the caret where the person types').toBe('woa-composer-input')
    await page.keyboard.type('列出订单')

    // Walk backwards to the first control in the window, then forwards through it.
    for (let step = 0; step < 20; step += 1) {
      if ((await focused(page))?.label === labels.newChat) break
      await page.keyboard.press('Shift+Tab')
    }
    const order: string[] = []
    for (let step = 0; step < 20; step += 1) {
      const active = await focused(page)
      if (active === null) break
      order.push(active.id ? active.id : active.label)
      expect(active.focus_visible, JSON.stringify(active)).toBe(true)
      if (active.id === 'woa-composer-input') {
        // The composer draws its ring on the field around the text, not on the textarea's
        // own outline.
        const field = page.locator('.woa-field')
        await expect(field).toHaveAttribute('data-focus', 'true')
        expect(await field.evaluate(node => getComputedStyle(node).boxShadow)).not.toBe('none')
      } else {
        // Most controls use an outside outline. The large scrollable transcript uses a quiet
        // inset shadow so keyboard focus does not resemble an error border around the whole
        // conversation.
        expect((active.outline_style !== 'none' && active.outline_width >= 2)
               || active.box_shadow !== 'none', JSON.stringify(active)).toBe(true)
      }
      if (active.label === labels.send) break
      await page.keyboard.press('Tab')
    }
    for (const [earlier, later] of [[labels.newChat, labels.collapse],
                                    [labels.collapse, 'woa-composer-input'],
                                    ['woa-composer-input', labels.send]] as const) {
      expect(order).toContain(earlier)
      expect(order).toContain(later)
      expect(order.indexOf(earlier), order.join(' > ')).toBeLessThan(order.indexOf(later))
    }

    await page.keyboard.press('Shift+Tab')
    expect((await focused(page))!.id).toBe('woa-composer-input')
    await page.keyboard.press('Enter')
    expect(await panel.calls()).toEqual([['submit', '列出订单']])
    expect(panel.errors).toEqual([])
  })

  test('both confirmation answers are reachable and operable by keyboard', async ({open}) => {
    const panel = await open(snapshot())
    const page = panel.page
    await panel.raiseApproval()
    expect((await focused(page))!.testid, 'the safe answer takes focus first').toBe('woa-deny')
    await page.keyboard.press('Shift+Tab')
    expect((await focused(page))!.testid,
           'Shift+Tab from the first answer wraps to the second').toBe('woa-approve')
    await page.keyboard.press('Tab')
    expect((await focused(page))!.testid).toBe('woa-deny')
    await page.keyboard.press('Tab')
    const active = (await focused(page))!
    expect(active.testid).toBe('woa-approve')
    expect(active.focus_visible).toBe(true)
    expect(active.outline_style).not.toBe('none')
    // A keyboard press is a real gesture: once the card has been visible long enough to
    // read, Enter approves exactly as a click does.
    await page.waitForTimeout(presentation.approval_visibility.min_visible_ms + 250)
    await page.keyboard.press('Enter')
    const calls = await panel.calls()
    expect(calls.map(call => call[0])).toEqual(['decide'])
    expect(calls[0]?.[2]).toBe('approved')
    expect(panel.errors).toEqual([])
  })

  test('no focus ring is cut off by a container that clips', async ({open}) => {
    // A ring drawn outside its control vanishes wherever an ancestor clips.
    //
    // A timeline node opens onto what was called, so it is a control a keyboard reaches;
    // drawing its ring outside itself put a stray line under the node instead of a ring
    // around it wherever an ancestor clipped.
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出所有待开票的销售订单，带金额'),
      entry('e2', 'progress', '正在读取业务数据', {
        tone: 'active', step: {stage: 'tool', tool: 'query_sale_orders', arguments: 'limit=50'}}),
      entry('e3', 'result', '3 张销售订单待开票。', {authored: 'server', panel_state: 'success'}),
    ]}))
    const page = panel.page
    await page.locator('.woa-launcher').click()
    // The landed turn's steps are folded; opened, each node is a control a keyboard reaches.
    await page.locator('.woa-work__summary').click()
    await page.locator('.woa-step__line').waitFor()
    // Real Tab presses: only keyboard focus draws the ring a keyboard user sees.
    const ring = () => page.evaluate(() => {
      const root = document.querySelector('#woa-assistant')!.shadowRoot!
      const node = root.activeElement
      if (!node || !node.matches(':focus-visible')) return null
      const style = getComputedStyle(node)
      const inset = style.outlineStyle === 'none' && style.boxShadow !== 'none'
      if (style.outlineStyle === 'none' && !inset) return null
      const name = `${node.tagName.toLowerCase()}.${node.className || node.id}`
      const grow = inset ? 0 : parseFloat(style.outlineOffset) + parseFloat(style.outlineWidth)
      const box = node.getBoundingClientRect()
      for (let at = node.parentElement; at; at = at.parentElement) {
        const clip = getComputedStyle(at)
        if (clip.overflowX === 'visible' && clip.overflowY === 'visible') continue
        const edge = at.getBoundingClientRect()
        if (box.left - grow < edge.left - 0.5 || box.top - grow < edge.top - 0.5
            || box.right + grow > edge.right + 0.5 || box.bottom + grow > edge.bottom + 0.5) {
          return {name, clipped_by: at.className}
        }
      }
      return {name, clipped_by: ''}
    })
    await page.locator('#woa-composer-input').focus()
    const seen: Record<string, string> = {}
    for (const key of [...Array(12).fill('Shift+Tab'), ...Array(12).fill('Tab')] as string[]) {
      await page.keyboard.press(key)
      const found = await ring()
      if (found !== null) seen[found.name] = found.clipped_by
    }
    // Otherwise an empty result could mean nothing was ever focused.
    expect(Object.keys(seen).length, JSON.stringify(seen)).toBeGreaterThanOrEqual(3)
    expect(Object.keys(seen).some(name => name.includes('woa-step__line')), JSON.stringify(seen))
      .toBe(true)
    expect(Object.fromEntries(Object.entries(seen).filter(([, by]) => by))).toEqual({})
    const summary = await page.locator('.woa-step__line').evaluate(node => {
      (node as HTMLElement).focus()
      const style = getComputedStyle(node)
      return {outline: style.outlineStyle, shadow: style.boxShadow}
    })
    expect(summary.outline).toBe('none')
    expect(summary.shadow).not.toBe('none')
    expect(panel.errors).toEqual([])
  })

  test('what a screen reader is handed names every control', async ({open}) => {
    // Roles and names only. Whether they are announced well is checked by a person.
    const panel = await open(conversation())
    await panel.page.locator('.woa-launcher').click()
    await panel.raiseApproval()
    const facts = await panel.page.evaluate(() => {
      const root = document.querySelector('#woa-assistant')!.shadowRoot!
      const text = (ids: string | null) => (ids ?? '').split(/\s+/)
        .map(id => (id && root.getElementById(id)?.textContent) || '').join(' ')
      const card = root.querySelector('[role="alertdialog"]')!
      const input = root.querySelector('#woa-composer-input')!
      const thread = root.querySelector('.woa-thread')!
      return {
        window_label: root.querySelector('.woa-window')!.getAttribute('aria-label'),
        log: [thread.getAttribute('role'), thread.getAttribute('aria-live')],
        unnamed_buttons: [...root.querySelectorAll('button')].filter(button =>
          !(button.getAttribute('aria-label') || (button.textContent ?? '').trim())).length,
        input_label: [...root.querySelectorAll(`label[for="${input.id}"]`)]
          .map(label => label.textContent).join(''),
        card_title: text(card.getAttribute('aria-labelledby')),
        card_description: text(card.getAttribute('aria-describedby')),
      }
    })
    expect(facts.window_label).toBe(labels.assistant)
    expect(facts.log).toEqual(['log', 'polite'])
    expect(facts.unnamed_buttons).toBe(0)
    expect(facts.input_label).toBe(labels.input)
    expect(facts.card_title).toContain(labels.approvalTitle)
    expect(facts.card_description).toContain('S00002')
    // What the buttons mean is said to a screen reader even where it is not shown.
    expect(facts.card_description).toContain(labels.approvalHint)
    expect(panel.errors).toEqual([])
  })
})

// ------------------------------------------------------------------------------------------
// F2b, as far as it reaches without an iframe.
//
// The policy is a response header the host application sends, so we cannot make it be there.
// What we can do -- and what these pin -- is that the panel never needs it relaxed: it
// constructs, renders and answers a confirmation under a policy that forbids inline script,
// inline style and every Trusted Types sink.
test.describe('under a strict CSP', () => {
  test('the panel constructs under strict CSP and Trusted Types', async ({open}) => {
    const panel = await open(snapshot({entries: [entry('e1', 'goal', '确认 S00002'),
                                                 entry('e2', 'action', '重新检查当前页面')]}),
                             {csp: STRICT_CSP})
    const violations: string[] = []
    panel.page.on('console', message => {
      if (message.text().includes('Content Security Policy')) violations.push(message.text())
    })
    await panel.raiseApproval({effect: '确认报价单 S00002', targets: ['sale.order · S00002']})
    await expect(panel.page.locator('.woa-approval')).toContainText('S00002')
    // The stylesheet arrived: `adoptedStyleSheets` is not governed by style-src, and no
    // <style> element was injected for the policy to refuse.
    expect(await panel.page.evaluate(() =>
      document.querySelector('#woa-assistant')!.shadowRoot!.adoptedStyleSheets.length)).toBe(1)
    expect(await panel.page.evaluate(() =>
      document.querySelector('#woa-assistant')!.shadowRoot!.querySelectorAll('style').length))
      .toBe(0)
    expect(violations).toEqual([])
    expect(panel.errors).toEqual([])
  })

  test('the panel mints no Trusted Types policy and writes no HTML', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出订单'),
      entry('e2', 'result', '**粗体** 与 `代码`', {authored: 'server', panel_state: 'success'}),
    ]}), {csp: STRICT_CSP})
    // `trusted-types 'none'` means creating a policy throws. Rendering markdown as React nodes
    // is why nothing here ever needs one.
    expect(await panel.page.evaluate(() => {
      try {
        (window as unknown as {trustedTypes: {createPolicy: (name: string, rules: object) => void}})
          .trustedTypes.createPolicy('probe', {createHTML: (value: string) => value})
        return false
      } catch {
        return true
      }
    })).toBe(true)
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-md').waitFor()
    await expect(panel.page.locator('.woa-md strong')).toContainText('粗体')
    expect(panel.errors).toEqual([])
  })
})

/** A token's hex colour as computed style reports it. */
function rgb(hex: string): string {
  const value = Number.parseInt(hex.slice(1), 16)
  return `rgb(${value >> 16}, ${(value >> 8) & 255}, ${value & 255})`
}
