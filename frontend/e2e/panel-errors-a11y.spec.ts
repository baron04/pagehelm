/**
 * Chromium checks for host isolation, fatal containment, drawer/phone/drag/resize, U07 accessibility and CSP.
 */
import type {Page} from '@playwright/test'
import presentation from '../src/presentation.json' with {type: 'json'}
import tokens from '../src/styles/design_tokens.json' with {type: 'json'}
import {
  STRICT_CSP, entry, expect, labels, snapshot, test, type Snapshot,
} from './harness'

test.describe('host containment', () => {
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

  test('a host modal cannot reclaim panel input, while its own focus trap stays active', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    await panel.page.evaluate(() => {
      const modal = document.createElement('section')
      modal.setAttribute('role', 'dialog')
      const field = document.createElement('input')
      field.id = 'host-modal-input'
      modal.appendChild(field)
      const outside = document.createElement('input')
      outside.id = 'host-outside-input'
      document.body.prepend(modal, outside)
      modal.addEventListener('focusout', event => {
        if (event.relatedTarget && !modal.contains(event.relatedTarget as Node)) {
          setTimeout(() => field.focus(), 0)
        }
      })
      field.focus()
    })
    await panel.page.locator('#woa-composer-input').click()
    await panel.page.keyboard.type('填写客户邮箱')
    await expect(panel.page.locator('#woa-composer-input')).toBeFocused()
    await expect(panel.page.locator('#woa-composer-input')).toHaveValue('填写客户邮箱')
    await panel.page.keyboard.press('Enter')
    expect(await panel.calls()).toEqual([['submit', '填写客户邮箱']])
    await panel.page.locator('#host-modal-input').focus()
    await panel.page.locator('#host-outside-input').focus()
    await expect(panel.page.locator('#host-modal-input')).toBeFocused()
    expect(panel.errors).toEqual([])
  })
})

test.describe('panel shortcuts', () => {
  // ⌘J on a Mac, Ctrl+J elsewhere -- whichever this browser reports being.
  const modifier = (page: Page) => page.evaluate(
    () => (/Mac|iPhone|iPad/.test(navigator.platform) ? 'Meta' : 'Control'))

  test('one shortcut opens the panel from the page and puts it away from inside', async ({open}) => {
    const panel = await open(snapshot())
    const page = panel.page
    const mod = await modifier(page)
    await expect(page.locator('.woa-launcher')).toHaveAttribute(
      'aria-keyshortcuts', mod === 'Meta' ? 'Meta+J' : 'Control+J')
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
                              targets: ['会打开页面、点击、填写和选择']})
    await page.keyboard.press('Escape')
    await expect(page.locator('.woa-window')).toHaveCount(1)
    expect(panel.errors).toEqual([])
  })
})

test.describe('failures', () => {
  test('a crash inside the panel never reaches the page or overwrites its error handler', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    expect(await panel.page.evaluate(
      () => (window as unknown as {__onerrorIsOurs: () => boolean}).__onerrorIsOurs())).toBe(true)
    await panel.page.evaluate(() => {
      (window as unknown as {__host: {snapshot: () => unknown}}).__host.snapshot = () => {
        throw new Error('the panel failed')
      }
    })
    await panel.set({})
    const fatal = panel.page.locator('.woa-fatal')
    await fatal.waitFor({timeout: 4000})
    await expect(fatal).toBeVisible()
    expect(panel.errors).toEqual([])
  })
})

test.describe('states and layout', () => {
  test('the palette and explicit theme reach the shadow root without host leakage', async ({open}) => {
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

    const background = () => window.evaluate(
      node => getComputedStyle(node).getPropertyValue('--woa-bg').trim())
    await panel.page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
    expect(await background()).toBe(tokens.themes.dark.bg)
    await panel.page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
    expect(await background()).toBe(tokens.themes.light.bg)
    expect(panel.errors).toEqual([])
  })

  test('a drawer narrows the page and gives its width back', async ({open}) => {
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

  test('on a phone the window is the whole screen', async ({open}) => {
    const panel = await open(snapshot(), {width: 375, height: 667})
    const page = panel.page
    await page.locator('.woa-launcher').click()
    const window = page.locator('.woa-window')
    await window.waitFor()
    await window.evaluate(node => node.getAnimations().forEach(animation => animation.finish()))
    expect(await window.getAttribute('data-mode')).toBe('full')
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

  test('the window moves and resizes and stays in the viewport', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.locator('.woa-launcher').click()
    const window = panel.page.locator('.woa-window')
    await window.waitFor()
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
      view: document.documentElement.clientWidth,
      overflow: document.documentElement.scrollWidth - innerWidth,
      rootStyle: document.documentElement.getAttribute('style'),
    }
  })
}

const LONG_QUESTION = ('请把 Nordwind Handelsgesellschaft mbH & Co. Kommanditgesellschaft 名下所有' +
  '未确认的报价单按金额从高到低列出来，并说明每一张的客户、币种和创建日期，' +
  '如果有并列金额的也全部列出。').repeat(2)

const VIEWPORTS = [
  {name: '320px', width: 320, height: 568, scale: 1, scheme: 'dark'},
  {name: '200pct-zoom', width: 640, height: 400, scale: 2, scheme: 'light'},
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
    const shell = root.querySelector('.woa-thread-shell')!
    const thread = rect('.woa-thread')!
    if (getComputedStyle(shell).overflowY !== 'visible') {
      thread.bottom = Math.min(thread.bottom, shell.getBoundingClientRect().bottom)
    }
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
  for (const {name, width, height, scale, scheme} of VIEWPORTS) {
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
                                 targets: ['客户：Nordwind Handelsgesellschaft mbH & Co. KG',
                                           '邮箱：buchhaltung@nordwind.example']})
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

  for (const scheme of ['light', 'dark'] as const) {
    test(`the ${scheme} scheme follows the system and keeps text legible`, async ({open}) => {
      const text = ['.woa-user', '.woa-md', '#woa-composer-input', '.woa-composer__disclaimer',
                    '.woa-approval h2', '.woa-approval__effect',
                    '.woa-approval__targets li',
                    '[data-testid="woa-deny"]', '[data-testid="woa-approve"]']
      const panel = await open(conversation(), {colorScheme: scheme})
      const page = panel.page
      await page.locator('.woa-launcher').click()
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
      await page.keyboard.press('Tab')
      const active = (await focused(page))!
      const ring = (await colours(page, '.woa-approval'))!
      expect(contrast(active.outline_color, ring.bg),
             `focus ring in ${scheme}: ${JSON.stringify(active)}`).toBeGreaterThanOrEqual(3)
      expect(panel.errors).toEqual([])
    })
  }

  test('reduced motion governs animations as well as transitions', async ({open}) => {
    const panel = await open(snapshot({
      connection: {state: 'acting', busy: true, stopping: false, online: true},
      entries: [entry('e1', 'goal', '列出订单'), entry('e2', 'action', '重新检查当前页面')],
    }), {reducedMotion: 'reduce'})
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-spinner').waitFor()
    await panel.raiseApproval()
    const {animations, transitions} = await panel.page.locator('.woa-window').evaluate(node => ({
      animations: [...node.querySelectorAll('*')]
        .map(el => getComputedStyle(el).animationDuration).filter(Boolean),
      transitions: [...node.querySelectorAll('*'), node]
        .flatMap(el => getComputedStyle(el).transitionDuration.split(',').map(parseFloat)),
    }))
    expect(animations.length).toBeGreaterThan(0)
    expect(animations.every(value => parseFloat(value) < 0.01), animations.join(', ')).toBe(true)
    expect(transitions.every(value => value < 0.01), transitions.join(', ')).toBe(true)
    expect(panel.errors).toEqual([])
  })

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
        const field = page.locator('.woa-field')
        await expect(field).toHaveAttribute('data-focus', 'true')
        expect(await field.evaluate(node => getComputedStyle(node).boxShadow)).not.toBe('none')
      } else {
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
    await expect(page.getByRole('alertdialog')).toBeFocused()
    await page.keyboard.press('Tab')
    expect((await focused(page))!.testid, 'Tab reaches the safe answer first').toBe('woa-deny')
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
    await page.waitForTimeout(presentation.approval_visibility.min_visible_ms + 250)
    await page.keyboard.press('Enter')
    const calls = await panel.calls()
    expect(calls.map(call => call[0])).toEqual(['decide'])
    expect(calls[0]?.[2]).toBe('approved')
    expect(panel.errors).toEqual([])
  })
})

test.describe('under a strict CSP', () => {
  test('the panel constructs and renders without minting Trusted Types or writing HTML', async ({open}) => {
    const panel = await open(snapshot({entries: [
      entry('e1', 'goal', '确认 S00002'),
      entry('e2', 'result', '**粗体** 与 `代码`', {authored: 'server', panel_state: 'success'}),
    ]}), {csp: STRICT_CSP})
    const violations: string[] = []
    panel.page.on('console', message => {
      if (message.text().includes('Content Security Policy')) violations.push(message.text())
    })
    expect(await panel.page.evaluate(() => {
      try {
        (window as unknown as {trustedTypes: {createPolicy: (name: string, rules: object) => void}})
          .trustedTypes.createPolicy('probe', {createHTML: (value: string) => value})
        return false
      } catch {
        return true
      }
    })).toBe(true)
    await panel.raiseApproval({effect: '确认报价单 S00002', targets: ['报价单：S00002']})
    await expect(panel.page.locator('.woa-approval')).toContainText('S00002')
    await expect(panel.page.locator('.woa-md strong')).toContainText('粗体')
    expect(await panel.page.evaluate(() =>
      document.querySelector('#woa-assistant')!.shadowRoot!.adoptedStyleSheets.length)).toBe(1)
    expect(await panel.page.evaluate(() =>
      document.querySelector('#woa-assistant')!.shadowRoot!.querySelectorAll('style').length))
      .toBe(0)
    expect(violations).toEqual([])
    expect(panel.errors).toEqual([])
  })
})
