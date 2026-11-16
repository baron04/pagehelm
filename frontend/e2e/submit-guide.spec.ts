/** Real Chromium layout against the shipped Runtime and the real panel shadow root. */
import {entry, expect, snapshot, test, type Harness} from './harness'

async function setup(panel: Harness) {
  await panel.page.locator('.woa-launcher').click()
  await panel.set({entries: [entry('e1', 'goal', '预填会议室'),
    entry('e2', 'result', '已填入会议室信息。\n\n**请核对后，手动点击「确定」提交。**', {panel_state: 'success'})]})
  await panel.settle()
  await panel.page.evaluate(async () => {
    const bundle = '/assistant.js'
    const {EmbeddedWebRuntime} = await import(bundle)
    const element = document.querySelector('#woa-assistant')!
    const container = document.createElement('div')
    element.before(container); container.append(element)
    const form = document.createElement('form'); form.id = 'synthetic-room'
    form.style.cssText = 'position:absolute;left:40px;top:100px;width:480px;height:360px;overflow:auto;background:#f6f7fa;padding:24px;box-sizing:border-box'
    const title = document.createElement('h2'); title.textContent = '新增会议室'
    const label = document.createElement('label'); label.textContent = '会议室名称 '
    const input = document.createElement('input'); input.value = '云台厅'; label.append(input)
    const spacer = document.createElement('div'); spacer.id = 'spacer'; spacer.style.height = '120px'
    const button = document.createElement('button'); button.id = 'submit-room'; button.textContent = '确定'; button.type = 'submit'
    button.style.cssText = 'margin-left:300px;width:88px;height:38px;background:#409eff;color:white;border:0;border-radius:4px'
    const tail = document.createElement('div'); tail.id = 'tail'; tail.style.height = '600px'
    form.append(title, label, spacer, button, tail); document.body.prepend(form)
    const state = window as unknown as {__submitCount: number; __guide: {clear(): void};
      __runtime: {page: {guideSubmit(ref: string, turnId: string): {clear(): void}}}}
    state.__submitCount = 0
    form.addEventListener('submit', event => {event.preventDefault(); state.__submitCount++})
    const runtime = new EmbeddedWebRuntime({endpoint: location.origin, sessionId: 'synthetic-guide',
      clientToken: 'test-only', sessionGeneration: 'test-generation', panel: false})
    runtime.panelContainer = container
    runtime.controller = {selectorMap: {}, getBrowserState: async () => ({url:location.href,title:'',content:''}),
      clickElement: async () => {throw new Error('guide must not click')},
      inputText: async () => {}, selectOption: async () => {}, scroll: async () => {}, dispose: () => {}}
    runtime.getElementByIndex = () => button
    state.__runtime = runtime
    state.__guide = runtime.page.guideSubmit('e1', 'turn-1')
  })
}

test('submit guide follows nested scrolling in all four directions without scrolling or submitting', async ({open}, info) => {
  const panel = await open(snapshot(), {width:1440,height:1000,reducedMotion:'reduce'})
  await setup(panel)
  const guide = panel.page.locator('.woa-submit-guide'), line = guide.locator('svg')
  await expect(line).toBeVisible()
  await panel.page.screenshot({path:info.outputPath('guide.png')})
  for (const [axis, value, text] of [['height','650px','下方'], ['height','120px','上方'],
    ['width','900px','右侧']] as const) {
    await panel.page.evaluate(([property, size]) => {
      const form = document.querySelector('#synthetic-room')!
      if (property === 'height') (document.querySelector('#spacer') as HTMLElement).style.height = size!
      else {(document.querySelector('#submit-room') as HTMLElement).style.marginLeft = size!; form.scrollTop=0}
      if (size === '120px') form.scrollTop = 520
    }, [axis,value])
    await expect(guide.locator('[role=status]')).toContainText(text)
    await expect(line).toBeHidden()
  }
  await panel.page.evaluate(() => {
    const button = document.querySelector('#submit-room') as HTMLElement
    button.style.marginLeft = '300px'
    ;(document.querySelector('#tail') as HTMLElement).style.width='1000px'
    const form = document.querySelector('#synthetic-room')!; form.scrollLeft = 350
  })
  await expect(guide.locator('[role=status]')).toContainText('左侧')
  await panel.page.evaluate(() => {const form = document.querySelector('#synthetic-room')!; form.scrollLeft=0;form.scrollTop=0})
  await expect(line).toBeVisible()
  expect(await panel.page.evaluate(() => (window as unknown as {__submitCount:number}).__submitCount)).toBe(0)
  expect(panel.errors).toEqual([])
})

test('submit guide automatically steps aside from occluded controls so they remain clickable, and handles overlays, disabled and hidden controls', async ({open}) => {
  const panel = await open(snapshot(), {width:1440,height:1000,reducedMotion:'reduce'})
  await setup(panel)
  const guide = panel.page.locator('.woa-submit-guide'), line = guide.locator('svg')
  await panel.page.evaluate(() => {
    const form = document.querySelector('#synthetic-room') as HTMLElement
    const panel = document.querySelector('#woa-assistant')!.shadowRoot!.querySelector('.woa-window')!.getBoundingClientRect()
    form.style.left=panel.left-260+'px';form.style.top=panel.top+20+'px'
  })
  await expect(line).toBeVisible()
  await panel.page.locator('#submit-room').click()
  expect(await panel.page.evaluate(() => (window as unknown as {__submitCount:number}).__submitCount)).toBe(1)
  await panel.page.evaluate(() => {
    const state = window as unknown as {__submitCount:number; __guide:{clear():void}; __runtime:{page:{guideSubmit(ref:string,turn:string):{clear():void}}}}
    state.__submitCount = 0
    const f=document.querySelector('#synthetic-room') as HTMLElement;f.style.left='40px';f.style.top='100px'
    state.__guide = state.__runtime.page.guideSubmit('e1', 'turn-1')
  })
  await expect(line).toBeVisible()
  await panel.page.evaluate(() => {
    const r=document.querySelector('#submit-room')!.getBoundingClientRect(), cover=document.createElement('div')
    cover.id='cover';cover.style.cssText=`position:fixed;z-index:100;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;background:white`
    document.body.append(cover)
  })
  await expect(guide).toHaveAttribute('data-state','covered')
  await panel.page.evaluate(() => {document.querySelector('#cover')!.remove();(document.querySelector('#submit-room') as HTMLButtonElement).disabled=true})
  await expect(guide).toHaveAttribute('data-state','disabled')
  await expect(guide.locator('[role=status]')).toContainText('必填项')
  await panel.page.evaluate(() => {const b=document.querySelector('#submit-room') as HTMLButtonElement;b.disabled=false;b.hidden=true})
  await expect(guide).toHaveAttribute('data-state','hidden')
  await expect(line).toBeHidden()
  await panel.page.evaluate(() => {(document.querySelector('#submit-room') as HTMLElement).hidden=false})
  await expect(line).toBeVisible()
  expect(panel.errors).toEqual([])
})

test('business control handoff steps aside on desktop, warns in full-screen mode when unavoidable, and clears on label change', async ({open}) => {
  const panel = await open(snapshot(), {width:560,height:800,reducedMotion:'reduce'})
  await setup(panel)
  await panel.page.evaluate(() => {
    const state = window as unknown as {__guide:{clear():void};__runtime:{page:{highlight(ref:string,turn:string):{clear():void}}}}
    state.__guide.clear()
    document.querySelector('#submit-room')!.textContent = '停用'
    const form = document.querySelector('#synthetic-room') as HTMLElement
    form.style.left = '20px'; form.style.top = '80px'
    state.__guide = state.__runtime.page.highlight('e1','turn-1')
  })
  const guide = panel.page.locator('.woa-submit-guide')
  await expect(guide).toHaveAttribute('data-state','assistant')
  await expect(guide.locator('[role=status]')).toContainText('操作位置被助手遮挡')
  await expect(guide.locator('svg')).toBeHidden()
  await panel.page.setViewportSize({width: 1440, height: 1000})
  await panel.page.evaluate(() => {
    const form = document.querySelector('#synthetic-room') as HTMLElement
    form.style.left='40px';form.style.top='100px'
  })
  await expect(guide.locator('svg')).toBeVisible()
  expect(await panel.page.evaluate(() => (window as unknown as {__submitCount:number}).__submitCount)).toBe(0)
  await panel.page.evaluate(() => {document.querySelector('#submit-room')!.textContent='另一条记录的停用'})
  await expect(guide).toHaveCount(0)
  expect(await panel.page.locator('#submit-room').evaluate(node=>node.style.outline)).toBe('')
  expect(panel.errors).toEqual([])
})

test('submit guide disappears on manual submit and target replacement', async ({open}) => {
  const panel = await open(snapshot(), {width:1440,height:1000,reducedMotion:'reduce'})
  await setup(panel)
  await panel.page.locator('#submit-room').click()
  await expect(panel.page.locator('.woa-submit-guide')).toHaveCount(0)
  expect(await panel.page.evaluate(() => (window as unknown as {__submitCount:number}).__submitCount)).toBe(1)
  await panel.page.evaluate(() => {
    const state=window as unknown as {__guide: {clear():void}; __runtime:{page:{guideSubmit(ref:string,turn:string):{clear():void}}}}
    state.__guide=state.__runtime.page.guideSubmit('e1','turn-1')
    document.querySelector('#submit-room')!.replaceWith(document.querySelector('#submit-room')!.cloneNode(true))
  })
  await expect(panel.page.locator('.woa-submit-guide')).toHaveCount(0)
  expect(panel.errors).toEqual([])
})

test('submit guide stays inert and cleans up under strict CSP and reduced motion', async ({open}) => {
  const panel = await open(snapshot(), {width:1440,height:1000,reducedMotion:'reduce',
    csp: "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; require-trusted-types-for 'script'; trusted-types 'none'"})
  await setup(panel)
  const line=panel.page.locator('.woa-submit-guide svg')
  await expect(line).toBeVisible()
  expect(await panel.page.locator('.woa-submit-guide').evaluate(node=>getComputedStyle(node).pointerEvents)).toBe('none')
  await panel.page.evaluate(() => {history.pushState({},'', '/different');document.body.classList.add('route-changed')})
  await expect(panel.page.locator('.woa-submit-guide')).toHaveCount(0)
  expect(panel.errors).toEqual([])
})


test('submit guide pauses while the reply is scrolled away and has no idle polling', async ({open}) => {
  const panel=await open(snapshot(), {width:1440,height:1000,reducedMotion:'reduce'})
  await setup(panel)
  await expect(panel.page.locator('.woa-submit-guide svg')).toBeVisible()
  await panel.page.evaluate(() => {
    const state=window as unknown as {__geometryReads:number}
    state.__geometryReads=0
    const button=document.querySelector('#submit-room')!
    const read=button.getBoundingClientRect.bind(button)
    button.getBoundingClientRect=()=>{state.__geometryReads++;return read()}
  })
  // No running task or animation needs to measure an idle form repeatedly.
  await panel.page.waitForTimeout(180)
  expect(await panel.page.evaluate(()=>(window as unknown as {__geometryReads:number}).__geometryReads)).toBeLessThan(3)
  await panel.set({entries:[entry('e1','goal','预填会议室'),entry('e2','result',
    '已填入会议室信息。\n\n'+Array.from({length:30},(_,i)=>`说明 ${i}`).join('\n\n')+'\n\n**请核对后，手动点击「确定」提交。**',
    {panel_state:'success'})]})
  await panel.page.locator('.woa-thread').evaluate(node=>{node.scrollTop=0})
  await expect(panel.page.locator('.woa-submit-guide svg')).toBeHidden()
  await panel.page.locator('.woa-thread').evaluate(node=>{node.scrollTop=node.scrollHeight})
  await expect(panel.page.locator('.woa-submit-guide svg')).toBeVisible()
  await panel.page.evaluate(()=>{document.querySelector('#synthetic-room')!.id='replacement-form'})
  await expect(panel.page.locator('.woa-submit-guide')).toHaveCount(0)
  expect(panel.errors).toEqual([])
})


test('submit guide never follows a newer conversation turn', async ({open}) => {
  const panel=await open(snapshot(), {width:1440,height:1000,reducedMotion:'reduce'})
  await setup(panel)
  await expect(panel.page.locator('.woa-submit-guide svg')).toBeVisible()
  await panel.set({entries:[entry('e1','goal','新问题', {turn_id:'turn-2'})]})
  await expect(panel.page.locator('.woa-submit-guide')).toHaveCount(0)
  expect(panel.errors).toEqual([])
})
