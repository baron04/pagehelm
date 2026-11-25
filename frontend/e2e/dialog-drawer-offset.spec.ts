/**
 * Real Chromium regression checks verifying that Element Plus fixed overlays, wide dialogs,
 * drawers, message boxes, and notifications stay clear of the docked assistant drawer,
 * and that guided controls in unadapted fixed dialogs still remain clickable.
 */
import {readFileSync} from 'node:fs'
import {join} from 'node:path'
import {entry, expect, labels, snapshot, STRICT_CSP, test, type Harness} from './harness'

const formSurfaceUrl = new URL('../dist/integrations/yudao/form-surface.js', import.meta.url)
const pageFactsUrl = new URL('../dist/integrations/yudao/page-facts.js', import.meta.url)
const yudaoModules = process.env.WOA_YUDAO_UI_MODULES

async function openDrawer(panel: Harness) {
  await panel.page.locator('.woa-launcher').click()
  await panel.page.locator('.woa-window').waitFor()
  await panel.page.getByRole('button', {name: labels.dockDrawer}).click()
  await expect(panel.page.locator('.woa-window')).toHaveAttribute('data-mode', 'drawer')
  await panel.settle()
}

test.describe('docked drawer clears fixed dialogs and overlays', () => {
  test('keeps wide Element Plus dialogs, message boxes, and notifications clear of the drawer across desktop widths and under strict CSP', async ({open}) => {
    const formSurfaceCode = readFileSync(formSurfaceUrl, 'utf8')
    for (const viewport of [{width: 1680, height: 900}, {width: 1280, height: 800}]) {
      const panel = await open(snapshot(), {...viewport, reducedMotion: 'reduce', csp: STRICT_CSP})
      await panel.page.route('**/woa/form-surface.js', route =>
        route.fulfill({body: formSurfaceCode, contentType: 'text/javascript; charset=utf-8'}))

      await panel.page.evaluate(async () => {
        const elCss = new CSSStyleSheet()
        elCss.replaceSync(`
          .el-overlay { position: fixed; top: 0; bottom: 0; left: 0; right: 0; height: 100%; background: rgba(0,0,0,.5); z-index: 2000; overflow: auto; }
          .el-overlay-dialog { position: fixed; top: 0; bottom: 0; left: 0; right: 0; overflow: auto; }
          .el-dialog { --el-dialog-width: 50%; margin: 15vh auto 50px; width: var(--el-dialog-width, 50%); box-sizing: border-box; padding: 16px; background: #fff; position: relative; }
          .el-dialog__footer { padding-top: 16px; text-align: right; box-sizing: border-box; }
          .is-message-box .el-overlay-message-box { text-align: center; padding: 16px; position: fixed; top: 0; bottom: 0; left: 0; right: 0; overflow: auto; }
          .el-message-box { display: inline-block; width: 100%; max-width: 420px; background: #fff; padding: 12px; box-sizing: border-box; }
          .el-notification { position: fixed; width: 330px; top: 16px; padding: 14px; background: #fff; box-sizing: border-box; z-index: 9999; }
          .el-notification.right { right: 16px; }
        `)
        document.adoptedStyleSheets = [...document.adoptedStyleSheets, elCss]

        const overlay = document.createElement('div')
        overlay.className = 'el-overlay el-modal-dialog'
        const dialogWrap = document.createElement('div')
        dialogWrap.className = 'el-overlay-dialog'
        dialogWrap.setAttribute('role', 'dialog')
        const dialog = document.createElement('div')
        dialog.id = 'sale-order-dialog'
        dialog.className = 'el-dialog'
        dialog.style.setProperty('--el-dialog-width', '1080px')
        const body = document.createElement('div')
        body.className = 'el-dialog__body'
        body.textContent = '新增销售订单'
        const footer = document.createElement('div')
        footer.className = 'el-dialog__footer'
        const confirmBtn = document.createElement('button')
        confirmBtn.id = 'confirm-btn'
        confirmBtn.type = 'button'
        confirmBtn.textContent = '确 定'
        const cancelBtn = document.createElement('button')
        cancelBtn.id = 'cancel-btn'
        cancelBtn.type = 'button'
        cancelBtn.textContent = '取 消'
        let confirmClicks = 0
        let cancelClicks = 0
        confirmBtn.addEventListener('click', () => { confirmClicks += 1 })
        cancelBtn.addEventListener('click', () => { cancelClicks += 1 })
        ;(window as unknown as {__clicks: () => {confirm: number; cancel: number}}).__clicks =
          () => ({confirm: confirmClicks, cancel: cancelClicks})
        footer.append(confirmBtn, cancelBtn)
        dialog.append(body, footer)
        dialogWrap.append(dialog)
        overlay.append(dialogWrap)

        const msgOverlay = document.createElement('div')
        msgOverlay.className = 'el-overlay is-message-box'
        msgOverlay.style.pointerEvents = 'none'
        const msgWrap = document.createElement('div')
        msgWrap.className = 'el-overlay-message-box'
        const msgBox = document.createElement('div')
        msgBox.id = 'confirm-box'
        msgBox.className = 'el-message-box'
        msgBox.textContent = '确认删除？'
        msgWrap.append(msgBox)
        msgOverlay.append(msgWrap)

        const toast = document.createElement('div')
        toast.id = 'right-toast'
        toast.className = 'el-notification right'
        toast.textContent = '保存成功'

        document.body.append(overlay, msgOverlay, toast)

        const surfacePath = '/woa/form-surface.js'
        const mod = await import(/* @vite-ignore */ surfacePath) as {installOverlayOffset(doc: Document): () => void}
        ;(window as unknown as {__disposeOffset?: () => void}).__disposeOffset = mod.installOverlayOffset(document)
      })

      await openDrawer(panel)

      const geometry = await panel.page.evaluate(() => {
        const drawer = document.querySelector('#woa-assistant')!.shadowRoot!
          .querySelector('.woa-window')!.getBoundingClientRect()
        const dialog = document.querySelector('#sale-order-dialog')!.getBoundingClientRect()
        const confirm = document.querySelector('#confirm-btn')!.getBoundingClientRect()
        const cancel = document.querySelector('#cancel-btn')!.getBoundingClientRect()
        const msgWrap = document.querySelector('.el-overlay-message-box')!.getBoundingClientRect()
        const toast = document.querySelector('#right-toast')!.getBoundingClientRect()
        const hitConfirm = document.elementFromPoint(confirm.left + confirm.width / 2, confirm.top + confirm.height / 2)
        const hitCancel = document.elementFromPoint(cancel.left + cancel.width / 2, cancel.top + cancel.height / 2)
        return {
          hasPushAttr: document.documentElement.hasAttribute('data-woa-drawer-push'),
          offset: document.documentElement.style.getPropertyValue('--woa-drawer-offset'),
          drawerLeft: drawer.left,
          dialogLeft: dialog.left,
          dialogRight: dialog.right,
          dialogWidth: dialog.width,
          confirmRight: confirm.right,
          cancelRight: cancel.right,
          hitConfirmId: hitConfirm?.id ?? '',
          hitCancelId: hitCancel?.id ?? '',
          msgWrapRight: msgWrap.right,
          toastRight: toast.right,
        }
      })

      expect(geometry.hasPushAttr).toBe(true)
      expect(geometry.offset).toBe('420px')
      expect(geometry.dialogLeft).toBeGreaterThanOrEqual(0)
      expect(geometry.dialogRight).toBeLessThanOrEqual(geometry.drawerLeft)
      expect(geometry.confirmRight).toBeLessThanOrEqual(geometry.drawerLeft)
      expect(geometry.cancelRight).toBeLessThanOrEqual(geometry.drawerLeft)
      expect(geometry.hitConfirmId).toBe('confirm-btn')
      expect(geometry.hitCancelId).toBe('cancel-btn')
      expect(geometry.msgWrapRight).toBeLessThanOrEqual(geometry.drawerLeft + 0.5)
      expect(geometry.toastRight).toBeLessThanOrEqual(geometry.drawerLeft)
      if (viewport.width === 1680) {
        expect(Math.round(geometry.dialogWidth)).toBe(1080)
      } else {
        expect(geometry.dialogWidth).toBeLessThanOrEqual(viewport.width - 420 - 32 + 0.5)
      }

      // Clicking both footer buttons succeeds natively without the assistant intercepting hits.
      await panel.page.locator('#confirm-btn').click()
      await panel.page.locator('#cancel-btn').click()
      expect(await panel.page.evaluate(() =>
        (window as unknown as {__clicks: () => {confirm: number; cancel: number}}).__clicks()))
        .toEqual({confirm: 1, cancel: 1})

      // Dragging the drawer wider keeps the dialog clamped and unobstructed in real time.
      const handle = panel.page.locator('.woa-handle[data-handle="w"]')
      const box = (await handle.boundingBox())!
      await panel.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
      await panel.page.mouse.down()
      await panel.page.mouse.move(box.x - 100, box.y + box.height / 2, {steps: 4})
      await panel.page.mouse.up()
      const resized = await panel.page.evaluate(() => {
        const drawer = document.querySelector('#woa-assistant')!.shadowRoot!
          .querySelector('.woa-window')!.getBoundingClientRect()
        const dialog = document.querySelector('#sale-order-dialog')!.getBoundingClientRect()
        const cancel = document.querySelector('#cancel-btn')!.getBoundingClientRect()
        return {
          drawerWidth: drawer.width,
          drawerLeft: drawer.left,
          dialogLeft: dialog.left,
          dialogRight: dialog.right,
          cancelRight: cancel.right,
        }
      })
      expect(resized.drawerWidth).toBeGreaterThan(460)
      expect(resized.dialogLeft).toBeGreaterThanOrEqual(0)
      expect(resized.dialogRight).toBeLessThanOrEqual(resized.drawerLeft)
      expect(resized.cancelRight).toBeLessThanOrEqual(resized.drawerLeft)

      // Collapsing the drawer restores the dialog to its full original width and removes push state.
      await panel.page.getByRole('button', {name: labels.collapse}).click()
      const restored = await panel.page.evaluate(() => ({
        hasPushAttr: document.documentElement.hasAttribute('data-woa-drawer-push'),
        offset: document.documentElement.style.getPropertyValue('--woa-drawer-offset'),
        dialogWidth: document.querySelector('#sale-order-dialog')!.getBoundingClientRect().width,
      }))
      expect(restored.hasPushAttr).toBe(false)
      expect(restored.offset).toBe('')
      expect(Math.round(restored.dialogWidth)).toBe(1080)
      expect(panel.errors).toEqual([])
    }
  })

  test('steps aside from an occluded submit button even in pushing drawer mode when the host app does not honor --woa-drawer-offset', async ({open}) => {
    const panel = await open(snapshot(), {width: 1440, height: 900, reducedMotion: 'reduce'})
    await openDrawer(panel)
    await panel.set({entries: [
      entry('e1', 'goal', '打开弹窗'),
      entry('e2', 'result', '已打开表单，请点击「确定」。', {panel_state: 'success'}),
    ]})
    await panel.settle()
    await panel.page.evaluate(async () => {
      const bundle = '/assistant.js'
      const {EmbeddedWebRuntime} = await import(/* @vite-ignore */ bundle) as {
        EmbeddedWebRuntime: new (config: Record<string, unknown>) => {
          panelContainer: HTMLElement | null
          controller: unknown
          getElementByIndex: unknown
          page: {guideSubmit(ref: string, turnId: string): {clear(): void}}
        }
      }
      const element = document.querySelector('#woa-assistant')!
      const container = document.createElement('div')
      element.before(container)
      container.append(element)

      const modal = document.createElement('div')
      modal.setAttribute('role', 'dialog')
      modal.style.cssText = 'position:fixed;left:220px;top:160px;width:1080px;height:420px;background:#fff;z-index:2000;box-sizing:border-box;padding:20px'
      const button = document.createElement('button')
      button.id = 'custom-submit'
      button.type = 'button'
      button.textContent = '确定'
      button.style.cssText = 'position:absolute;right:24px;bottom:24px;width:88px;height:36px'
      let clicked = 0
      button.addEventListener('click', () => { clicked += 1 })
      ;(window as unknown as {__clicked: () => number}).__clicked = () => clicked
      modal.append(button)
      document.body.append(modal)

      const runtime = new EmbeddedWebRuntime({
        endpoint: location.origin, sessionId: 'drawer-avoid',
        clientToken: 'test-only', sessionGeneration: 'test-gen', panel: false,
      })
      runtime.panelContainer = container
      runtime.controller = {
        selectorMap: {}, getBrowserState: async () => ({url: location.href, title: '', content: ''}),
        clickElement: async () => ({success: true}), inputText: async () => {},
        selectOption: async () => {}, scroll: async () => {}, dispose: () => {},
      }
      runtime.getElementByIndex = () => button
      ;(window as unknown as {__guide?: {clear(): void}}).__guide = runtime.page.guideSubmit('e1', 'turn-1')
    })

    await expect(panel.page.locator('.woa-window')).toHaveAttribute('data-mode', 'float')
    await expect(panel.page.locator('.woa-submit-guide svg')).toBeVisible()
    await panel.page.locator('#custom-submit').click()
    expect(await panel.page.evaluate(() => (window as unknown as {__clicked: () => number}).__clicked())).toBe(1)
    expect(panel.errors).toEqual([])
  })

  test('real Element Plus ElDialog and ElDrawer stay clear of the docked drawer via page-facts before PageController loads and after controller disposal', async ({open}) => {
    test.skip(!yudaoModules, 'WOA_YUDAO_UI_MODULES must name the qualified application dependencies')
    const panel = await open(snapshot(), {width: 1280, height: 800, reducedMotion: 'reduce'})
    const files = new Map<string, string | URL>([
      ['/vue.js', join(yudaoModules!, 'vue/dist/vue.global.prod.js')],
      ['/element.js', join(yudaoModules!, 'element-plus/dist/index.full.min.js')],
      ['/element.css', join(yudaoModules!, 'element-plus/dist/index.css')],
      ['/woa/page-facts.js', pageFactsUrl],
      ['/woa/form-surface.js', formSurfaceUrl],
    ])
    await panel.page.route('**/*', async route => {
      const url = new URL(route.request().url())
      const file = files.get(url.pathname)
      if (!file) return route.continue()
      return route.fulfill({
        body: readFileSync(file),
        contentType: url.pathname.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/javascript; charset=utf-8',
      })
    })

    // Load real Element Plus stylesheet + Vue + Element Plus + built page-facts.js (before any PageController).
    await panel.page.addStyleTag({url: '/element.css'})
    await panel.page.addScriptTag({url: '/vue.js'})
    await panel.page.addScriptTag({url: '/element.js'})
    await panel.page.evaluate(async () => {
      const factsPath = '/woa/page-facts.js'
      await import(/* @vite-ignore */ factsPath)
      type VueApi = {
        createApp(component: unknown): {mount(selector: string): void}
        h(...args: unknown[]): unknown
        ref<T>(initial: T): {value: T}
      }
      const {createApp, h, ref} = (window as unknown as {Vue: VueApi}).Vue
      const EP = (window as unknown as {ElementPlus: Record<string, unknown>}).ElementPlus
      const dialogOpen = ref(true)
      const drawerOpen = ref(false)
      const modal = ref(true)
      const host = document.createElement('div')
      host.id = 'ep-app'
      document.body.prepend(host)
      const app = createApp({
        setup() {
          return () => h('div', [
            h(EP.ElDialog, {
              modelValue: dialogOpen.value,
              title: '新增',
              width: '1080',
              modal: modal.value,
              appendToBody: true,
            }, {
              default: () => h('div', {style: 'height: 120px'}, 'ERP 销售订单表单'),
              footer: () => [
                h(EP.ElButton, {id: 'ep-confirm', type: 'primary'}, () => '确 定'),
                h(EP.ElButton, {id: 'ep-cancel'}, () => '取 消'),
              ],
            }),
            h(EP.ElDrawer, {
              modelValue: drawerOpen.value,
              title: '详情抽屉',
              direction: 'rtl',
              size: '460px',
              appendToBody: true,
            }, {default: () => h('div', '抽屉内容')}),
          ])
        },
      })
      app.mount('#ep-app')
      ;(window as unknown as {__ep: unknown}).__ep = {dialogOpen, drawerOpen, modal}
    })

    await panel.page.locator('#ep-confirm').waitFor({state: 'visible'})
    await openDrawer(panel)

    const measure = () => panel.page.evaluate(() => {
      const drawer = document.querySelector('#woa-assistant')!.shadowRoot!
        .querySelector('.woa-window')!.getBoundingClientRect()
      const dialog = document.querySelector('.el-dialog')!.getBoundingClientRect()
      const confirm = document.querySelector('#ep-confirm')!.getBoundingClientRect()
      const cancel = document.querySelector('#ep-cancel')!.getBoundingClientRect()
      const hitCancel = document.elementFromPoint(cancel.left + cancel.width / 2, cancel.top + cancel.height / 2)
      return {
        drawerLeft: drawer.left,
        dialogLeft: dialog.left,
        dialogRight: dialog.right,
        confirmRight: confirm.right,
        cancelRight: cancel.right,
        cancelHitsButton: Boolean(hitCancel?.closest('#ep-cancel')),
        sheetsCount: document.adoptedStyleSheets.length,
      }
    })

    const beforeController = await measure()
    expect(beforeController.dialogLeft).toBeGreaterThanOrEqual(0)
    expect(beforeController.dialogRight).toBeLessThanOrEqual(beforeController.drawerLeft)
    expect(beforeController.confirmRight).toBeLessThanOrEqual(beforeController.drawerLeft)
    expect(beforeController.cancelRight).toBeLessThanOrEqual(beforeController.drawerLeft)
    expect(beforeController.cancelHitsButton).toBe(true)

    // Installing and then disposing PageController's form-surface offset shares the registry
    // with page-facts.js: no duplicate sheet is added and disposal keeps the offset active.
    const afterControllerDispose = await panel.page.evaluate(async () => {
      const surfacePath = '/woa/form-surface.js'
      const {installOverlayOffset} = await import(/* @vite-ignore */ surfacePath) as {
        installOverlayOffset(doc: Document): () => void
      }
      const beforeSheets = document.adoptedStyleSheets.length
      const dispose = installOverlayOffset(document)
      const duringSheets = document.adoptedStyleSheets.length
      dispose()
      const afterSheets = document.adoptedStyleSheets.length
      return {beforeSheets, duringSheets, afterSheets}
    })
    expect(afterControllerDispose.duringSheets).toBe(afterControllerDispose.beforeSheets)
    expect(afterControllerDispose.afterSheets).toBe(afterControllerDispose.beforeSheets)

    const stillClear = await measure()
    expect(stillClear.dialogRight).toBeLessThanOrEqual(stillClear.drawerLeft)
    expect(stillClear.cancelHitsButton).toBe(true)

    // Non-modal ElDialog (modal: false, inline right: 0px) and RTL ElDrawer stay clear too.
    await panel.page.evaluate(() => {
      const ep = (window as unknown as {__ep: {modal: {value: boolean}}}).__ep
      ep.modal.value = false
    })
    const nonModal = await measure()
    expect(nonModal.dialogRight).toBeLessThanOrEqual(nonModal.drawerLeft)
    expect(nonModal.cancelHitsButton).toBe(true)

    await panel.page.evaluate(() => {
      const ep = (window as unknown as {__ep: {dialogOpen: {value: boolean}; drawerOpen: {value: boolean}}}).__ep
      ep.dialogOpen.value = false
      ep.drawerOpen.value = true
    })
    await panel.page.locator('.el-drawer').waitFor({state: 'visible'})
    // Element Plus plays a 300ms slide-in transition on .el-drawer regardless of reducedMotion.
    await expect.poll(async () => panel.page.evaluate(() => {
      const panelDrawer = document.querySelector('#woa-assistant')!.shadowRoot!
        .querySelector('.woa-window')!.getBoundingClientRect()
      const elDrawer = document.querySelector('.el-drawer')!.getBoundingClientRect()
      return {
        settled: elDrawer.left >= 0 && elDrawer.right <= panelDrawer.left + 0.5,
        panelLeft: Math.round(panelDrawer.left),
        elDrawerRight: Math.round(elDrawer.right),
      }
    }), {timeout: 3000}).toMatchObject({settled: true})
    expect(panel.errors).toEqual([])
  })
})
