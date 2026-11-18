/**
 * Chromium checks for host isolation, notices/fatal errors, layout/drawer/drag/resize, U07 accessibility and CSP.
 */
import type {Page} from '@playwright/test'
import presentation from '../src/presentation.json' with {type: 'json'}
import tokens from '../src/styles/design_tokens.json' with {type: 'json'}
import {
  CONFIGURATION, STRICT_CSP, entry, expect, labels, notices, snapshot, test,
  type Snapshot,
} from './harness'
import {rgb} from './panelHelpers'

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
                               targets: ['会打开页面、点击、填写和选择']})
    await page.keyboard.press('Escape')
    await expect(page.locator('.woa-window')).toHaveCount(1)
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

  test('the face is the logo, and stopping stays legible under the pointer', async ({open}) => {
    // The assistant's face is the launcher and the title bar, from the first moment to the
    // last. It is in the bundle itself: it loads with no request of its own.
    const panel = await open(snapshot())
    const loaded = (scope: string) => panel.page.locator(scope)
      .evaluate((node: HTMLImageElement) => node.naturalWidth)
    expect(await loaded('.woa-launcher__disc img')).toBe(192)
    await expect(panel.page.locator('.woa-launcher .woa-ai-tag')).toHaveCount(0)
    await panel.page.locator('.woa-launcher').click()
    await panel.page.locator('.woa-empty').waitFor()
    // A new conversation opens on the face in its own greeting, so the bar carries only its
    // controls -- no name and no second face. Both are the same picture, from the bundle.
    await expect(panel.page.locator('.woa-empty__face img')).toBeVisible()
    expect(await loaded('.woa-empty__face img')).toBe(192)
    await expect(panel.page.locator('.woa-titlebar .woa-brand')).toHaveCount(0)
    await expect(panel.page.locator('.woa-titlebar .woa-ai-tag')).toHaveCount(0)
    // Once a conversation is under way the bar carries the face and name again.
    await panel.set({
      connection: {state: 'acting', busy: true, stopping: false, online: true},
      entries: [entry('e1', 'goal', '列出订单'), entry('e2', 'progress', '正在读取业务数据')]})
    await expect(panel.page.locator('.woa-titlebar .woa-brand__face')).toBeVisible()
    expect(await loaded('.woa-titlebar .woa-brand__face')).toBe(192)
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
    await panel.raiseApproval({effect: '确认报价单 S00002', targets: ['报价单：S00002']})
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
