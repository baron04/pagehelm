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
  test(`the rendered glow visibly colours every edge while keeping the centre clear (${colorScheme})`, async ({open}) => {
    const panel = await open(snapshot(), {colorScheme})
    const page = panel.page
    await page.evaluate(theme => { document.body.style.background = theme === 'light' ? 'white' : '#0f1115' }, colorScheme)
    const bare = (await page.screenshot()).toString('base64')
    await panel.set({connection: working})
    await confirm(panel)
    await panel.set({pendingApproval: null})
    await expect(page.locator('.woa-agent-cursor')).toHaveCount(0)
    await page.getByRole('button', {name: labels.collapse}).click()
    await expect(page.locator('.woa-screen-effects__canvas')).toHaveAttribute('data-ready', 'true')
    expect(await page.locator('.woa-screen-effects__canvas').evaluate(node =>
      !(node as HTMLCanvasElement).getContext('webgl2')!.isContextLost())).toBe(true)
    const renderer = await page.locator('.woa-screen-effects__canvas').evaluate(node => {
      const gl = (node as HTMLCanvasElement).getContext('webgl2')!
      const program = gl.getParameter(gl.CURRENT_PROGRAM) as WebGLProgram
      return {
        colours: [0, 1, 2, 3].map(i => Array.from(gl.getUniform(program,
          gl.getUniformLocation(program, `uColors[${i}]`)!) as Float32Array)),
        factor: gl.getUniform(program, gl.getUniformLocation(program, 'uGlowFactor')!) as number,
        aaGuard: gl.getAttachedShaders(program)!.some(shader =>
          gl.getShaderSource(shader)!.includes('max(fwidth(d), 0.0001)')),
      }
    })
    expect(renderer.aaGuard).toBe(true)
    expect(renderer.factor).toBeCloseTo(colorScheme === 'dark' ? 1.8 : 1)
    for (const [i, hex] of ['39b6ff', 'bd45fb', 'ff5733', 'ffd600'].entries()) {
      for (let channel = 0; channel < 3; channel++) {
        expect(renderer.colours[i]![channel]).toBeCloseTo(parseInt(hex.slice(channel * 2, channel * 2 + 2), 16) / 255)
      }
    }
    await page.waitForTimeout(800)
    const lit = (await page.screenshot()).toString('base64')
    await page.waitForTimeout(2200)
    const flowed = (await page.screenshot()).toString('base64')
    const {changes, hueMovement} = await page.evaluate(async ({bare, lit, flowed}) => {
      async function pixels(base64: string): Promise<CanvasRenderingContext2D> {
        const image = new Image()
        image.src = `data:image/png;base64,${base64}`
        await image.decode()
        const canvas = document.createElement('canvas')
        canvas.width = image.width; canvas.height = image.height
        const context = canvas.getContext('2d')!
        context.drawImage(image, 0, 0)
        return context
      }
      const before = await pixels(bare), after = await pixels(lit), later = await pixels(flowed)
      const width = document.documentElement.clientWidth, height = window.innerHeight
      // Moving sources can leave a side's midpoint almost white. Sample the whole
      // visible colour pool on each side, excluding the panel's launcher corner.
      const sides = [
        [0.2, 0.35, 0.5, 0.65, 0.8].map(t => [8, height * t]),
        [0.2, 0.35, 0.5, 0.65, 0.8].map(t => [width - 8, height * t]),
        [0.2, 0.35, 0.5, 0.65, 0.8].map(t => [width * t, 8]),
        [0.2, 0.35, 0.5, 0.65, 0.8].map(t => [width * t, height - 8]),
      ]
      const samples = sides.flat()
      const change = ([x, y]: number[]): number => {
        const a = before.getImageData(x!, y!, 1, 1).data
        const b = after.getImageData(x!, y!, 1, 1).data
        const c = later.getImageData(x!, y!, 1, 1).data
        return Math.max(...[b, c].map(pixel => [0, 1, 2].reduce(
          (sum, index) => sum + Math.abs(a[index]! - pixel[index]!), 0)))
      }
      const changes = sides.map(side => Math.max(...side.map(change)))
      changes.push(change([width / 2, height / 2]))
      // Hue changes, rather than just opacity/brightness, demonstrate moving colours.
      const hue = (data: Uint8ClampedArray): number[] => {
        const r = data[0]!, g = data[1]!, b = data[2]!
        const range = Math.max(r, g, b) - Math.min(r, g, b)
        return range < 10 ? [0, 0] : [(r - g) / range, (b - g) / range]
      }
      const hueMovement = samples.map(([x, y]) => {
        const a = hue(after.getImageData(x!, y!, 1, 1).data)
        const b = hue(later.getImageData(x!, y!, 1, 1).data)
        return Math.abs(a[0]! - b[0]!) + Math.abs(a[1]! - b[1]!)
      })
      return {changes, hueMovement}
    }, {bare, lit, flowed})
    for (const edge of changes.slice(0, 4)) expect(edge, JSON.stringify(changes)).toBeGreaterThan(45)
    expect(changes[4]).toBeLessThan(3)
    expect(Math.max(...hueMovement)).toBeGreaterThan(0.3)
  })

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

test('the glow stays visible across a full breath and many progress updates', async ({open}) => {
  const panel = await open(snapshot({connection: working}))
  await confirm(panel)
  await panel.set({pendingApproval: null})
  const glow = panel.page.locator('.woa-screen-effects__glow')
  for (let tick = 0; tick < 7; tick++) {
    await panel.page.waitForTimeout(800)
    await panel.set({connection: {...working, state: tick % 2 ? 'observing' : 'acting'}})
    await expect(panel.page.locator('.woa-screen-effects')).toHaveAttribute('data-phase', 'active')
    expect(await glow.evaluate(node => Number(getComputedStyle(node).opacity))).toBeGreaterThan(0.5)
  }
  await panel.set({connection: {...working, busy: false, state: 'success'}})
  await expect(panel.page.locator('.woa-screen-effects')).toHaveAttribute('data-phase', 'off')
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
