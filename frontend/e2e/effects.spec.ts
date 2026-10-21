import {test, expect, snapshot, STRICT_CSP, labels, PENDING, type Harness} from './harness'

const working = {state: 'acting', busy: true, stopping: false, online: true}

async function confirm(panel: Harness): Promise<void> {
  await panel.raiseApproval({kind: 'operate'})
  await panel.settle()
  await panel.page.waitForTimeout(600)
  await panel.page.getByTestId('woa-approve').click()
  await expect(panel.page.locator('.woa-screen-effects')).toHaveAttribute('data-phase', 'active')
}

test('only a confirmation click starts the light; completion retires it before the next turn', async ({open}) => {
  const panel = await open(snapshot())
  const effects = panel.page.locator('.woa-screen-effects')
  await expect(effects).toHaveCount(0)
  await panel.set({optimistic: {turnId: 't1', text: 'Read the list', failed: false}})
  await expect(effects).toHaveCount(0)
  await panel.set({optimistic: null, connection: working})
  await panel.raiseApproval({kind: 'operate'})
  await expect(effects).toHaveCount(0)
  // Honour the approval card's visibility/dwell gate before pressing it.
  await panel.settle()
  await panel.page.waitForTimeout(600)
  await panel.page.getByTestId('woa-approve').click()
  expect(await panel.calls()).toContainEqual(['decide', 'a-1', 'approved'])
  // The effect brightens at the press, before the simulated network removes the card.
  await expect(effects).toHaveAttribute('data-phase', 'active')
  await panel.set({pendingApproval: null})
  await expect(effects).toHaveAttribute('data-phase', 'active')
  await panel.page.getByRole('button', {name: labels.collapse}).click()
  await expect(effects).toHaveAttribute('data-phase', 'active')
  // Transport termination can arrive before an old busy flag is cleared.
  await panel.set({connection: {...working, state: 'success'}})
  await expect(effects).toHaveCount(0)
  await panel.set({connection: working})
  await expect(effects).toHaveCount(0)
  await panel.raiseApproval({approval_id: 'next', turn_id: 'turn-2', kind: 'operate'})
  await expect(effects).toHaveCount(0)
  await panel.settle()
  await panel.page.waitForTimeout(600)
  await panel.page.getByTestId('woa-approve').click()
  await expect(effects).toHaveAttribute('data-phase', 'active')
  await panel.set({pendingApproval: null, connection: {...working, busy: false}})
  await expect(effects).toHaveCount(0)
  expect(panel.errors).toEqual([])
})

for (const colorScheme of ['light', 'dark'] as const) {
  test(`viewport light and cursor preserve host interactions under strict CSP (${colorScheme})`, async ({open}) => {
    const panel = await open(snapshot({connection: working}), {colorScheme, csp: STRICT_CSP})
    const {page} = panel
    await confirm(panel)
    await panel.set({pendingApproval: null})
    await page.getByRole('button', {name: labels.collapse}).click()
    await page.evaluate(() => {
      // The harness's inline body stylesheet is deliberately blocked by this CSP.
      document.body.style.height = '2000px'
      const input = document.createElement('input')
      input.id = 'host-input'
      input.style.cssText = 'position:fixed;left:80px;top:80px;width:160px'
      document.body.append(input)
      const button = document.createElement('button')
      button.id = 'host-button'
      button.textContent = 'Host action'
      button.style.cssText = 'position:fixed;left:80px;top:130px'
      button.addEventListener('click', () => { button.dataset.clicked = 'true' })
      document.body.append(button)
      input.focus()
    })
    await panel.set({activityPoint: {x: 90, y: 90, at: Date.now()}})
    const cursor = page.locator('.woa-agent-cursor')
    await expect(cursor).toHaveAttribute('data-targeted', 'true')
    await expect(cursor).toHaveCSS('left', '90px')
    await expect(cursor.locator('svg')).toHaveCount(1)
    await expect(page.locator('#host-input')).toBeFocused()
    await page.locator('#host-input').fill('still editable')
    await page.locator('#host-button').click()
    await expect(page.locator('#host-button')).toHaveAttribute('data-clicked', 'true')
    await page.mouse.move(400, 300)
    await page.mouse.wheel(0, 500)
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0)
    await page.locator('.woa-launcher').click()
    await page.locator('#woa-composer-input').fill('a follow-up')
    await expect(page.locator('#woa-composer-input')).toHaveValue('a follow-up')
    const effectsBox = await page.locator('.woa-screen-effects').boundingBox()
    const viewport = await page.evaluate(() => ({width: document.documentElement.clientWidth,
      height: window.innerHeight}))
    expect(effectsBox).toEqual({x: 0, y: 0, ...viewport})
    await page.getByRole('button', {name: labels.dockDrawer}).click()
    expect(await page.locator('.woa-screen-effects').boundingBox()).toEqual(effectsBox)
    await expect(page.locator('#woa-composer-input')).toBeEditable()
    // Model/tool gaps must not send the assistant pointer back to a corner.
    await page.waitForTimeout(2200)
    await expect(cursor).toHaveAttribute('data-targeted', 'true')
    await expect(cursor).toHaveCSS('left', '90px')
    await panel.set({activityPoint: null})
    await expect(cursor).toHaveCount(0)
    expect(panel.errors).toEqual([])
  })
}

test('an expired approval and a failed submission leave no viewport effect', async ({open}) => {
  const panel = await open(snapshot({connection: working}))
  await expect(panel.page.locator('.woa-screen-effects')).toHaveCount(0)
  await panel.raiseApproval()
  await panel.set({pendingApproval: {...PENDING, expires_at: Date.now() + 200}})
  await expect(panel.page.locator('.woa-screen-effects')).toHaveCount(0, {timeout: 3000})
  await panel.set({pendingApproval: null, connection: {...working, busy: false},
    optimistic: {turnId: 't1', text: 'read', failed: true}})
  await expect(panel.page.locator('.woa-screen-effects')).toHaveCount(0)
})

test('reduced motion keeps the light static and destroying the panel removes it', async ({open}) => {
  const panel = await open(snapshot({connection: working}), {reducedMotion: 'reduce'})
  await confirm(panel)
  await panel.set({pendingApproval: null})
  const canvas = panel.page.locator('.woa-screen-effects__canvas')
  await expect(canvas).toHaveAttribute('data-ready', 'true')
  // Inspect the real GPU's clock: CSS-only checks cannot catch a running shader.
  const clock = () => canvas.evaluate(node => {
    const gl = (node as HTMLCanvasElement).getContext('webgl2')!
    const program = gl.getParameter(gl.CURRENT_PROGRAM) as WebGLProgram
    return gl.getUniform(program, gl.getUniformLocation(program, 'uTime')!) as number
  })
  const frozen = await clock()
  await panel.page.waitForTimeout(300)
  expect(await clock()).toBe(frozen)
  await panel.page.emulateMedia({reducedMotion: 'no-preference'})
  await expect.poll(clock).toBeGreaterThan(frozen)
  await panel.page.emulateMedia({reducedMotion: 'reduce'})
  const repeating = await panel.page.locator('.woa-screen-effects').evaluate(node =>
    node.getAnimations({subtree: true}).some(animation => animation.effect?.getTiming().iterations === Infinity))
  expect(repeating).toBe(false)
  await panel.page.evaluate(() => (window as unknown as {__panel: {destroy(): void}}).__panel.destroy())
  await expect(panel.page.locator('#woa-assistant')).toHaveCount(0)
})

test('GPU loss reveals the fallback and leaves the host interactive', async ({open}) => {
  const panel = await open(snapshot({connection: working}))
  await confirm(panel)
  await panel.set({pendingApproval: null})
  const canvas = panel.page.locator('.woa-screen-effects__canvas')
  await expect(canvas).toHaveAttribute('data-ready', 'true')
  await canvas.evaluate(node => (node as HTMLCanvasElement).getContext('webgl2')!
    .getExtension('WEBGL_lose_context')!.loseContext())
  await expect(canvas).not.toHaveAttribute('data-ready')
  await expect(panel.page.locator('.woa-screen-effects__fallback')).toBeVisible()
  await panel.page.locator('#woa-composer-input').fill('still interactive')
  await expect(panel.page.locator('#woa-composer-input')).toHaveValue('still interactive')
  expect(panel.errors).toEqual([])
})

test('a browser without WebGL still displays a pass-through CSS field after confirmation', async ({open}) => {
  const panel = await open(snapshot({connection: working}))
  await panel.page.evaluate(() => {
    const original = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, kind: string, ...args: unknown[]) {
      return kind === 'webgl2' ? null : Reflect.apply(original, this, [kind, ...args])
    } as typeof original
  })
  await confirm(panel)
  const fallback = panel.page.locator('.woa-screen-effects__fallback')
  await expect(fallback).toBeVisible()
  await expect(fallback).toHaveCSS('pointer-events', 'none')
  await expect(panel.page.locator('.woa-screen-effects__canvas')).not.toHaveAttribute('data-ready')
  expect(panel.errors).toEqual([])
})

test('declining, a new conversation, and a second approval cannot reuse the previous click', async ({open}) => {
  const panel = await open(snapshot({connection: working}))
  await confirm(panel)
  await panel.set({pendingApproval: null})
  await panel.raiseApproval({approval_id: 'a2', kind: 'operate'})
  await expect(panel.page.locator('.woa-screen-effects')).toHaveCount(0)
  await panel.settle()
  await panel.page.waitForTimeout(600)
  await panel.page.getByTestId('woa-approve').click()
  await expect(panel.page.locator('.woa-screen-effects')).toHaveAttribute('data-phase', 'active')
  await panel.set({pendingApproval: null})
  await panel.page.getByRole('button', {name: labels.newChat}).click()
  await expect(panel.page.locator('.woa-screen-effects')).toHaveCount(0)
  await panel.raiseApproval({approval_id: 'a3', kind: 'operate'})
  await panel.settle()
  await panel.page.waitForTimeout(600)
  await panel.page.getByTestId('woa-deny').click()
  await expect(panel.page.locator('.woa-screen-effects')).toHaveCount(0)
})
