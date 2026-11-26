/**
 * Chromium checks for structured answer links, data tables, composer layout/caret and history scroll anchoring.
 */
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
    await expect(panel.page.locator('.woa-datatable__size')).toHaveCount(0)
    await expect(panel.page.locator('.woa-datatable__pagebtn[aria-current="page"]')).toHaveText('1')
    await expect(panel.page.locator('.woa-md')).toContainText('source: all rows in this read')
    await panel.page.locator('.woa-datatable__pagebtn', {hasText: /^5$/}).click()
    await expect(panel.page.locator('.woa-datatable__pagebtn[aria-current="page"]')).toHaveText('5')
    await expect(panel.page.locator('.woa-md tbody tr')).toHaveCount(10)
    await expect(panel.page.locator('.woa-md')).toContainText('contact-050@synthetic.example.test')
    await expect(panel.page.locator('.woa-md')).toContainText('source: all rows in this read')
    expect(panel.errors).toEqual([])
  })
})

test.describe('composer layout and caret', () => {
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

  test('the composer grows and only scrolls once it has stopped growing', async ({open}) => {
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
    expect(tall.overflow).toBe('auto')
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
})
