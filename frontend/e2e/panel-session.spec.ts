/**
 * Chromium checks for launcher state, session lifecycle, work process timeline, answers and copying.
 */
import type {Page} from '@playwright/test'
import tokens from '../src/styles/design_tokens.json' with {type: 'json'}
import {formatTime} from '../src/state/clock'
import {CONFIGURATION, entry, expect, labels, snapshot, test} from './harness'
import {rgb} from './panelHelpers'

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

  test('the launcher counts answers not yet seen, not every line of a turn', async ({open}) => {
    const turn = (n: number, at: number) => [
      entry(`e${n}1`, 'goal', `问题 ${n}`, {at}),
      entry(`e${n}2`, 'progress', '正在思考', {at: at + 1, transient: true}),
      entry(`e${n}3`, 'progress', '正在读取业务数据', {at: at + 2}),
      entry(`e${n}4`, 'result', `回答 ${n}`, {at: at + 3, panel_state: 'success'}),
    ]
    const panel = await open(snapshot())
    const launcher = panel.page.locator('.woa-launcher')
    const nub = launcher.locator('.woa-launcher__nub')
    await launcher.waitFor()
    // A whole turn finishes while the panel is put away: one answer to come back to.
    await panel.set({entries: turn(1, 1_000)})
    await expect(nub).toHaveText('1')
    await launcher.click()
    await panel.page.getByText('回答 1', {exact: true}).waitFor()
    await panel.page.getByRole('button', {name: labels.collapse}).click()
    await expect(nub).toHaveCount(0)
    // A refresh starts collapsed and the session hands back the conversation already read.
    await panel.page.reload()
    await launcher.waitFor()
    await panel.set({entries: turn(1, 1_000)})
    await expect(nub).toHaveCount(0)
    // Only what arrived after it counts.
    await panel.set({entries: [...turn(1, 1_000), ...turn(2, 2_000)]})
    await expect(nub).toHaveText('1')
    expect(panel.errors).toEqual([])
  })
})

test.describe('session lifecycle', () => {
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
    // Losing the history request does not turn the conversation into a new greeting.
    await panel.set({connection: {state: 'error', busy: false, stopping: false, online: true},
      notice: {code: 'start_failed', retryable: true, message: '连接失败，请重试。'}})
    await expect(page.locator('.woa-empty')).toHaveCount(0)
    await expect(page.locator('.woa-skeleton')).toHaveCount(0)
    await expect(page.locator('.woa-inline-error')).toBeVisible()
    await panel.set({connection: {state: 'reconnecting', busy: false, stopping: false, online: true}})
    await expect(page.locator('.woa-empty')).toHaveCount(0)
    await expect(page.locator('.woa-skeleton')).toHaveCount(1, {timeout: 2000})
    await panel.set({
      connection: {state: 'ready', busy: false, stopping: false, online: true},
      notice: null,
      entries: [entry('e1', 'goal', '列出订单'), entry('e2', 'result', '三张。', {panel_state: 'success'})]})
    await expect(page.locator('.woa-skeleton')).toHaveCount(0)
    await expect(page.locator('.woa-user')).toHaveText('列出订单')
    expect(panel.errors).toEqual([])
  })

  test('title bar shows read-only badge only when server projects read_only_mode=true', async ({open}) => {
    const writable = await open(snapshot())
    await writable.page.locator('.woa-launcher').click()
    await expect(writable.page.locator('.woa-readonly-badge')).toHaveCount(0)
    expect(writable.errors).toEqual([])

    const readonly = await open(snapshot(), {
      configuration: {...CONFIGURATION, read_only_mode: true},
    })
    await readonly.page.locator('.woa-launcher').click()
    const badge = readonly.page.locator('.woa-readonly-badge')
    await expect(badge).toBeVisible()
    await expect(badge).toHaveText(labels.readOnlyMode)
    expect(readonly.errors).toEqual([])
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
})

test.describe('answers', () => {
  test('refused and uncertain outcomes render the same safe Markdown as successful answers', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('goal', 'goal', '定位业务操作'),
      entry('result', 'result', '**尚未执行。**\n\n- 已定位目标\n- 请自行确认', {panel_state: 'refused'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    const card = panel.page.locator('.woa-outcome')
    await expect(card.locator('strong')).toHaveText('尚未执行。')
    await expect(card.locator('li')).toHaveCount(2)
    await expect(card).not.toContainText('**')
    await panel.set({entries: [
      entry('goal', 'goal', '定位业务操作'),
      entry('result', 'result', '**结果待核实。**\n\n- 请核对当前记录', {panel_state: 'indeterminate'}),
    ]})
    await expect(card).toHaveAttribute('data-kind', 'indeterminate')
    await expect(card.locator('strong')).toHaveText('结果待核实。')
    expect(panel.errors).toEqual([])
  })

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

  test('a small table omits the search toolbar and redundant TSV/CSV buttons', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出待开票订单'),
      entry('e2', 'result', ANSWER, {authored: 'server', panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await expect(panel.page.locator('.woa-datatable__table')).toBeVisible()
    await expect(panel.page.locator('.woa-datatable__toolbar')).toHaveCount(0)
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
    await panel.page.getByRole('button', {name: labels.copyAnswer}).click()
    expect(await clipboard(panel.page))
      .toBe(`已扫完全部 12 条，命中 2 条。\n\n${TABLE}\n\n这两张都处于 待开票 状态。`)
    expect(panel.errors).toEqual([])
  })
})

test.describe('the work process', () => {
  test('a turn that only ran shows its time, and one that acted folds its steps into a line', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '列出所有待开票的销售订单'),
      entry('e2', 'progress', '正在理解你的需求'),
      entry('e3', 'result', '十六张。', {panel_state: 'success'}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-took').waitFor()
    // Nothing happened that a list could show, so there is no list and no control promising
    // one -- only how long it took.
    await expect(panel.page.locator('.woa-work')).toHaveCount(0)
    await expect(panel.page.locator('.woa-took')).toContainText(labels.elapsedPrefix)
    // How it ended is still said, with the mark the summary line of a turn with steps uses.
    await expect(panel.page.locator('.woa-took > svg')).toHaveCount(1)
    await expect(panel.page.locator('.woa-took')).not.toHaveAttribute('data-failed')
    await panel.set({entries: [
      entry('e1', 'goal', '列出所有待开票的销售订单'),
      entry('e2', 'progress', '正在理解你的需求'),
      entry('e3', 'result', '做不到。', {panel_state: 'refused'}),
    ]})
    await expect(panel.page.locator('.woa-took')).toHaveAttribute('data-failed', 'true')

    await panel.set({entries: [
      {id: 'f1', sequence: 1, turn_id: 't2', at: 1757000000000, kind: 'goal', tone: 'neutral',
       text: '打开 Acme 的客户记录'},
      {id: 'f2', sequence: 2, turn_id: 't2', at: 1757000000000, kind: 'progress', tone: 'active',
       text: '正在理解你的需求'},
      {id: 'f3', sequence: 3, turn_id: 't2', at: 1757000002000, kind: 'action', tone: 'active',
       text: '打开“Acme Corporation”'},
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

  test('a line that only says it is working shows while it is the newest step of a running turn', async ({open}) => {
    // Between every two things the assistant did, "正在思考" filled half the list; once
    // something newer has happened, it says nothing any more.
    const running = {state: 'acting', busy: true, stopping: false, online: true}
    const thinking = (id: string, text: string) =>
      entry(id, 'progress', text, {tone: 'active', transient: true, step: {stage: 'thinking'}})
    const steps = [
      entry('e1', 'goal', '打开 S00007'),
      thinking('e2', '正在思考'),
      entry('e3', 'action', '点击“Sales”菜单项', {tone: 'active'}),
      thinking('e4', '正在思考（第 2 步）'),
    ]
    const panel = await open(snapshot({connection: running, entries: steps}))
    const page = panel.page
    await page.locator('.woa-launcher').click()
    const live = page.locator('.woa-work__live')
    await expect(live.locator('.woa-work__now')).toHaveText('正在思考（第 2 步）')
    await live.click()
    expect((await page.locator('.woa-step__text').allInnerTexts()).map(line => line.trim()))
      .toEqual(['点击“Sales”菜单项', '正在思考（第 2 步）'])
    // Ended, none of them is left, and the count is of what was done.
    await panel.set({connection: {...running, busy: false, state: 'success'}, entries: [
      ...steps, entry('e5', 'action', '点击“S00007”', {tone: 'active'}),
      thinking('e6', '正在思考（第 3 步）'),
      entry('e7', 'result', '已打开。', {panel_state: 'success'})]})
    const summary = page.locator('.woa-work__summary')
    await expect(summary).toContainText(labels.workSteps.replace('{count}', '2'))
    if (await summary.getAttribute('aria-expanded') === 'false') await summary.click()
    expect((await page.locator('.woa-step__text').allInnerTexts()).map(line => line.trim()))
      .toEqual(['点击“Sales”菜单项', '点击“S00007”'])
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

  test('while the card is up it is the only thing saying a confirmation is needed', async ({open}) => {
    const running = {state: 'acting', busy: true, stopping: false, online: true}
    const entries = [
      entry('e1', 'goal', '把报价单 S00002 确认成销售订单'),
      entry('e2', 'progress', '正在准备这次更改', {tone: 'active'}),
      entry('e3', 'approval', '需要确认后才能继续', {tone: 'warning'}),
    ]
    const panel = await open(snapshot({connection: running, entries}))
    await panel.page.locator('.woa-launcher').click()
    await panel.raiseApproval()
    await expect(panel.page.locator('.woa-work')).toHaveCount(0)
    // Answered, the turn goes on, and its line comes back with the steps behind it.
    await panel.set({connection: running, pendingApproval: null, entries: [
      ...entries, entry('e4', 'progress', '已收到你的确认', {tone: 'active'})]})
    await expect(panel.page.locator('.woa-work__now')).toHaveText('已收到你的确认')
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
    await expect(panel.page.locator('.woa-step')).toHaveCount(1)
    await expect(panel.page.locator(".woa-step[data-status='done']")).toHaveCount(1)
    await expect(panel.page.locator('.woa-step__text')).toHaveText('已读取 3 条：未确认、未取消的报价单')
    // A node that opens says so; how long it took is inside, not in a column beside it.
    await expect(panel.page.locator('.woa-step__chevron')).toHaveCount(1)
    await expect(panel.page.locator('.woa-work__steps')).not.toContainText('4s')
    // Opening a node shows what was called, with what, what came back, and for how long --
    // the panel invents none of it; every field is projected and bounded by the server.
    await panel.page.locator('.woa-step__line').first().click()
    const detail = panel.page.locator('.woa-step__detail').first()
    await detail.waitFor()
    await expect(detail).toContainText('query_quotations')
    await expect(detail).toContainText('limit=50')
    await expect(detail).toContainText('3 条 · 未确认、未取消的报价单')
    await expect(detail).toContainText(`${labels.stepDuration}4s`)
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
    // Entering the 28px near-bottom band need not mean the smooth scroll has finished.
    await expect.poll(async () => {
      const turn = await panel.page.locator('.woa-turn').last().boundingBox()
      const composer = await panel.page.locator('.woa-composer').boundingBox()
      return composer!.y - (turn!.y + turn!.height)
    }).toBeGreaterThanOrEqual(55)
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
