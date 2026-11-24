/**
 * Chromium checks for structured answer links, data tables, task examples, composer input and history paging.
 */
import {SAMPLE_EXAMPLES} from '../dev/examples'
import {entry, expect, labels, snapshot, test} from './harness'

test.describe('structured answer links', () => {
  test('the built panel renders the issued record separately and hands a click to its host', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', 'Find Grace Hopper'),
      entry('e2', 'result', 'Grace Hopper is visible.', {panel_state: 'success',
        answer_targets: [{kind: 'record', address: '/contacts/grace-hopper',
          label: 'Grace Hopper', identity: 'contacts:grace-hopper', read_ref: 'read#1'}]}),
    ]}))
    await panel.page.locator('.woa-launcher').click()
    const related = panel.page.getByRole('navigation', {name: labels.relatedLinks})
    const link = related.getByRole('link', {name: 'Grace Hopper'})
    await expect(link).toHaveAttribute('href', '/contacts/grace-hopper')
    await link.click()
    expect(await panel.calls()).toEqual([['openAnswerTarget', '/contacts/grace-hopper']])
    expect(panel.errors).toEqual([])
  })
})

test.describe('tables', () => {
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
    await expect(panel.page.locator('.woa-md tbody tr')).toHaveCount(10)
    await expect(panel.page.locator('.woa-datatable__pageinfo')).toHaveText('1 / 5')
    await expect(panel.page.locator('.woa-md')).toContainText('source: all rows in this read')
    await panel.page.getByLabel(labels.tablePageSize).selectOption('all')
    await expect(panel.page.locator('.woa-md tbody tr')).toHaveCount(50)
    await expect(panel.page.locator('.woa-md')).toContainText('contact-050@synthetic.example.test')
    await expect(panel.page.locator('.woa-md')).toContainText('source: all rows in this read')
    expect(panel.errors).toEqual([])
  })
})

test.describe('examples and composer', () => {
  test('uses the same send control in the welcome and conversation views', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    const composer = panel.page.locator('#woa-composer-input')
    await composer.fill('列出联系人')
    const style = () => panel.page.locator('.woa-send').evaluate(node => {
      const css = getComputedStyle(node)
      return [css.width, css.height, css.borderRadius, css.backgroundImage, css.color]
    })
    const welcomeStyle = await style()
    await panel.settle()
    const buttonBox = await panel.page.locator('.woa-send').boundingBox()
    expect(buttonBox!.width).toBeGreaterThanOrEqual(28)
    expect(buttonBox!.width).toBeLessThanOrEqual(32)
    await panel.set({entries: [entry('e1', 'goal', '上一条需求'),
      entry('e2', 'result', '已完成。', {panel_state: 'success'})]})
    await expect(panel.page.locator('.woa-empty')).toHaveCount(0)
    expect(await style()).toEqual(welcomeStyle)
    expect(panel.errors).toEqual([])
  })

  test('opening shows the assistant greeting, its face, and task examples', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    const empty = panel.page.locator('.woa-empty')
    await empty.waitFor()
    // Who is here -- its face beside the first line -- then the question and what it can be asked.
    await expect(empty.locator('.woa-empty__intro')).toHaveText(labels.emptyIntro)
    await expect(empty.locator('h3')).toHaveText(labels.emptyGreeting)
    await expect(empty.locator('.woa-empty__detail')).toHaveText(labels.emptyDetail)
    await expect(empty.locator('.woa-empty__face img')).toHaveCount(1)
    // A way in for each kind the page offers an example of, in the design's order, and a
    // last one that asks the assistant itself; nothing that sends on its own.
    const ways = empty.locator('.woa-example')
    await expect(ways).toHaveCount(4)
    const expected = [[labels.exampleQuery, SAMPLE_EXAMPLES[0]!.text],
                      [labels.exampleOperate, SAMPLE_EXAMPLES[2]!.text],
                      [labels.exampleFind, SAMPLE_EXAMPLES[1]!.text],
                      [labels.exampleMore, labels.exampleMorePrompt]]
    for (const [index, [title, text]] of expected.entries()) {
      await expect(ways.nth(index).locator('.woa-example__title')).toHaveText(title!)
      await expect(ways.nth(index).locator('.woa-example__text')).toHaveText(`“${text}”`)
    }
    // Each card names its kind in the request's own colour: the same order as the labels.
    const kinds = ['query', 'operate', 'find', 'more']
    for (const [index, kind] of kinds.entries()) {
      await expect(ways.nth(index)).toHaveAttribute('data-kind', kind)
    }
    // A full-width mark that ends an example is set half-width, so the closing quote sits
    // against it; a sentence that ends on a character is left as it is.
    await expect(ways.nth(3).locator('.woa-example__halt')).toHaveText('？')
    await expect(ways.nth(3).locator('.woa-example__halt'))
      .toHaveCSS('font-feature-settings', '"halt"')
    await expect(ways.nth(0).locator('.woa-example__halt')).toHaveCount(0)
    // Every answer is a model's, so the panel says so under the composer, in the server's words.
    await expect(panel.page.locator('.woa-composer__disclaimer')).toHaveText(labels.aiDisclaimer)
    // The design's composition: the invitation, then its ways in, then the writing surface at
    // the foot -- the place the composer keeps once the conversation begins. Read all boxes in
    // one frame, including while the opening animation is settling.
    const boxes = await empty.evaluate(node => {
      const hero = node.querySelector('.woa-empty__hero')!.getBoundingClientRect()
      const examples = node.querySelector('.woa-examples')!.getBoundingClientRect()
      const composer = node.querySelector('.woa-composer')!.getBoundingClientRect()
      const panel = node.getBoundingClientRect()
      return {belowHero: examples.top - hero.bottom, belowExamples: composer.top - examples.bottom,
              inLowerHalf: composer.top > panel.top + panel.height / 2}
    })
    expect(boxes.belowHero).toBeGreaterThanOrEqual(-0.1)
    expect(boxes.belowExamples).toBeGreaterThan(0)
    expect(boxes.inLowerHalf).toBe(true)
    await expect(panel.page.locator('.woa-composer button')).toHaveCount(1)
    await expect(panel.page.locator('#woa-composer-input'))
      .toHaveAttribute('placeholder', labels.placeholder)
    expect(await panel.calls()).toEqual([])
    expect(panel.errors).toEqual([])
  })

  test('a way in fills the composer for the person to send or change, and sends nothing', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    // Picked by kind, not by position: the order is the design's and may move again.
    await panel.page.locator('.woa-example[data-kind="operate"]').click()
    const composer = panel.page.locator('#woa-composer-input')
    await expect(composer).toHaveValue(SAMPLE_EXAMPLES[2]!.text)
    await expect(composer).toBeFocused()
    expect(await composer.evaluate((node: HTMLTextAreaElement) => node.selectionStart))
      .toBe(SAMPLE_EXAMPLES[2]!.text.length)
    expect(await panel.calls()).toEqual([])
    await panel.page.locator('.woa-example[data-kind="more"]').click()
    await expect(composer).toHaveValue(labels.exampleMorePrompt)
    expect(panel.errors).toEqual([])
  })

  test('in a short window the greeting is read from its top, not pinned to the bottom', async ({open}) => {
    const panel = await open(snapshot(), {width: 320, height: 480})
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-empty').waitFor()
    await panel.page.waitForTimeout(300)
    const thread = panel.page.locator('.woa-empty')
    expect(await thread.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true)
    expect(await thread.evaluate(node => node.scrollTop)).toBe(0)
    await expect(panel.page.locator('.woa-empty__intro')).toBeInViewport()
    await panel.page.locator('.woa-example').last().scrollIntoViewIfNeeded()
    await expect(panel.page.locator('.woa-example').last()).toBeInViewport()
    await panel.page.locator('.woa-example').last().click()
    await expect(panel.page.locator('#woa-composer-input')).toBeFocused()
    await expect(panel.page.locator('#woa-composer-input')).toBeInViewport()
    expect(panel.errors).toEqual([])
  })

  test('entering a conversation keeps the input node, draft and caret while docking it below the thread', async ({open}) => {
    const panel = await open(snapshot())
    const page = panel.page
    await page.locator('.woa-launcher').click()
    const input = page.locator('#woa-composer-input')
    await input.fill('保留尚未提交的草稿')
    const original = await input.elementHandle()
    await input.evaluate((node: HTMLTextAreaElement) => node.setSelectionRange(2, 5))
    await panel.set({entries: [entry('e1', 'goal', '其他页面提交的问题')]})
    await expect(page.locator('.woa-empty')).toHaveCount(0)
    expect(await input.evaluate((node, previous) => node === previous, original)).toBe(true)
    await expect(input).toHaveValue('保留尚未提交的草稿')
    await expect(input).toBeFocused()
    expect(await input.evaluate((node: HTMLTextAreaElement) => [node.selectionStart, node.selectionEnd]))
      .toEqual([2, 5])
    const window = await page.locator('.woa-window').boundingBox()
    const composer = await page.locator('.woa-composer').boundingBox()
    expect(window!.y + window!.height - (composer!.y + composer!.height)).toBeLessThan(2)
    expect(panel.errors).toEqual([])
  })

  test('a clarification releases and focuses the composer for the reply', async ({open}) => {
    const panel = await open(snapshot({
      connection: {state: 'needs_input', busy: false, stopping: false, online: true},
      entries: [
        entry('e1', 'goal', '打开 Acme'),
        entry('e2', 'result', '请选择：\n1. Acme — 伦敦\n2. Acme — 巴黎',
              {panel_state: 'needs_input', tone: 'neutral'}),
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
})

test.describe('conversation history paging', () => {
  const turns = (start: number, end: number) => Array.from({length: end - start}, (_, i) => {
    const n = start + i
    return [
      entry(`e${n * 2 + 1}`, 'goal', `历史问题 ${n}`, {turn_id: `history-${n}`}),
      entry(`e${n * 2 + 2}`, 'result', `历史回答 ${n}。`,
        {turn_id: `history-${n}`, panel_state: 'success', replayed: true}),
    ]
  }).flat()

  test('loads near the top and keeps the visible message when prepending', async ({open}) => {
    const current = turns(20, 40)
    const panel = await open(snapshot({entries: current, hasOlder: true}))
    await panel.page.locator('.woa-launcher').click()
    await panel.page.getByText('历史回答 39。', {exact: true}).waitFor()
    await expect.poll(() => panel.page.locator('.woa-thread').evaluate(
      node => node.scrollHeight - node.scrollTop - node.clientHeight)).toBeLessThanOrEqual(30)
    // Initial display follows the newest message and does not eagerly fetch all history.
    expect(await panel.calls()).toEqual([])
    const thread = panel.page.locator('.woa-thread')
    await thread.hover()
    await panel.page.mouse.wheel(0, -100000)
    await expect.poll(async () => (await panel.calls()).filter(c => c[0] === 'loadOlder').length)
      .toBe(1)
    await expect.poll(() => thread.evaluate(node => node.scrollTop)).toBe(0)
    const message = panel.page.getByText('历史问题 20', {exact: true})
    const before = await message.boundingBox()
    await panel.set({loadingOlder: true})
    await panel.set({entries: [...turns(0, 20), ...current], loadingOlder: false, hasOlder: false})
    await expect.poll(async () => (await message.boundingBox())!.y).toBeCloseTo(before!.y, 0)
    await expect(panel.page.locator('.woa-jump')).toHaveAttribute('aria-label', labels.jumpLatest)
    expect(panel.errors).toEqual([])
  })

  test('anchors an answer when the beginning of its turn arrives on an older page', async ({open}) => {
    const answer = entry('e2', 'result', Array.from({length: 40}, (_, n) => `回答行 ${n}`).join('\n\n'),
      {turn_id: 'partial', panel_state: 'success', replayed: true})
    const panel = await open(snapshot({entries: [answer], hasOlder: true}))
    await panel.page.locator('.woa-launcher').click()
    const thread = panel.page.locator('.woa-thread')
    await expect.poll(() => thread.evaluate(
      node => node.scrollHeight - node.scrollTop - node.clientHeight)).toBeLessThanOrEqual(30)
    await thread.hover()
    await panel.page.mouse.wheel(0, -100000)
    await expect.poll(async () => (await panel.calls()).filter(c => c[0] === 'loadOlder').length)
      .toBe(1)
    await expect.poll(() => thread.evaluate(node => node.scrollTop)).toBe(0)
    const result = panel.page.locator('.woa-result')
    const before = await result.boundingBox()
    await panel.set({loadingOlder: true})
    await panel.set({entries: [entry('e1', 'goal', '这一轮的早期问题', {turn_id: 'partial'}), answer],
      loadingOlder: false, hasOlder: false})
    await expect.poll(async () => (await result.boundingBox())!.y).toBeCloseTo(before!.y, 0)
    await expect(panel.page.locator('.woa-jump')).toHaveAttribute('aria-label', labels.jumpLatest)
    expect(panel.errors).toEqual([])
  })

  test('history failure offers retry and does not automatically loop', async ({open}) => {
    const panel = await open(snapshot({entries: turns(0, 20), hasOlder: true, historyError: true}))
    await panel.page.locator('.woa-launcher').click()
    const thread = panel.page.locator('.woa-thread')
    await thread.hover()
    await panel.page.mouse.wheel(0, -100000)
    await panel.page.getByRole('status').filter({hasText: labels.historyLoadFailed}).waitFor()
    expect(await panel.calls()).toEqual([])
    await panel.page.getByRole('button', {name: labels.retry, exact: true}).click()
    expect((await panel.calls()).filter(c => c[0] === 'loadOlder')).toHaveLength(1)
    await panel.set({loadingOlder: true, historyError: false})
    await expect(panel.page.locator('.woa-history button')).toBeDisabled()
    expect(panel.errors).toEqual([])
  })
})
