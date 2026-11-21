import {afterEach, describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
// @ts-expect-error Generic Element Plus modules ship as framework-free JS.
import {findElRestrictedControls, focusElRestrictedControl} from '../src/integrations/element-plus/form.js'
import {probeComboboxOptions} from '../src/runtime/combobox'
import {executeCommand} from '../src/runtime/executor'
import {FormFillTracker} from '../src/runtime/formFillTracker'
import {Page, type PageControllerApi} from '../src/runtime/page'
import {hydrateScrollFacts, type ElementLike} from '../src/runtime/pageModel'
import {beginPageSettleWatch, isSubstantiveMutation, waitForPageSettle} from '../src/runtime/pageSettle'
import {describeBrowserState, summarizeObservation} from '../src/runtime/pageSnapshot'
import {PanelHost, type PanelActions} from '../src/runtime/panelHost'
import {createRedactor} from '../src/runtime/redaction'
import {receipt} from '../src/runtime/results'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'
import type {ActionEnvelope, ElementRef, ObservationEnvelope} from '../src/runtime/types'

const redactor = createRedactor()

function mockController(): PageControllerApi {
  return {
    selectorMap: new Map(),
    getBrowserState: vi.fn(async () => ({url: location.href, title: 'App', content: ''})),
    clickElement: vi.fn(async () => ({success: true})),
    inputText: vi.fn(async () => ({success: true})),
    selectOption: vi.fn(async () => ({success: true})),
    scroll: vi.fn(async () => ({success: true})),
    dispose: vi.fn(),
  }
}

function makeActions(overrides: Partial<PanelActions> = {}): PanelActions {
  return {
    submit: vi.fn(),
    cancel: vi.fn(),
    decide: vi.fn(),
    newConversation: vi.fn(),
    loadOlder: vi.fn(),
    ...overrides,
  }
}

function makeAction(action: string, args: Record<string, unknown>, overrides: Partial<ActionEnvelope> = {}): ActionEnvelope {
  return {
    schema_version: 1,
    session_id: 's1',
    turn_id: 'turn-1',
    action_id: 'act-1',
    based_on_observation_id: 'obs-1',
    page_epoch: 1,
    deadline: Date.now() + 5000,
    command_digest: '0'.repeat(64),
    command: {
      command_id: 'act-1',
      action,
      args,
      expect_navigation: false,
    },
    started_url_display: 'https://app.test/page',
    ...overrides,
  }
}

function makeObservation(epoch = 2, overrides: Partial<ObservationEnvelope['observation']> = {}): ObservationEnvelope {
  return {
    schema_version: 1,
    session_id: 's1',
    observation_id: `obs-${epoch}`,
    page_epoch: epoch,
    observation: {
      schema_version: 1,
      epoch,
      url: 'https://app.test/page',
      title: 'Page',
      landmarks: [],
      elements: [],
      content: '',
      messages: [],
      truncated: false,
      ...overrides,
    },
    events: [],
    caused_by_action_id: 'act-1',
  }
}

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('ACC-04(b,c,d): scroll room hints, async option probe, and resilient page settle', () => {
  it('ACC-04(b): annotates dialog and page scroll containers with scroll_up_px and scroll_down_px', () => {
    document.body.innerHTML = `
      <div id="main-scroll" style="overflow-y: auto">
        <button id="page-btn">Load more</button>
      </div>
      <div id="dlg" role="dialog" style="overflow-y: auto">
        <input id="dlg-input" />
      </div>
    `
    const mainScroll = document.getElementById('main-scroll')!
    const pageBtn = document.getElementById('page-btn')!
    const dlg = document.getElementById('dlg')!
    const dlgInput = document.getElementById('dlg-input')!

    Object.defineProperty(mainScroll, 'scrollHeight', {value: 1000, configurable: true})
    Object.defineProperty(mainScroll, 'clientHeight', {value: 400, configurable: true})
    Object.defineProperty(mainScroll, 'scrollTop', {value: 150, configurable: true})

    Object.defineProperty(dlg, 'scrollHeight', {value: 800, configurable: true})
    Object.defineProperty(dlg, 'clientHeight', {value: 300, configurable: true})
    Object.defineProperty(dlg, 'scrollTop', {value: 80, configurable: true})

    const map = new Map([[1, {ref: pageBtn}], [2, {ref: dlgInput}]])
    const lookup = (m: unknown, idx: number) => (m as Map<number, {ref: Element}>).get(idx)?.ref
    const elements: ElementRef[] = [
      {ref: 'e1', role: 'button', name: 'Load more', tag: 'button', epoch: 1, attrs: {}},
      {ref: 'e2', role: 'textbox', name: 'Name', tag: 'input', epoch: 1, attrs: {}},
    ]
    hydrateScrollFacts(elements, map, lookup)
    expect(elements[0]!.attrs).toMatchObject({scroll_up_px: '150', scroll_down_px: '450'})
    expect(elements[1]!.attrs).toMatchObject({scroll_up_px: '80', scroll_down_px: '420'})

    const reading = describeBrowserState({
      controller: {...mockController(), selectorMap: map},
      lookup,
      redactor,
    }, {url: location.href, title: 'Test', content: '[1]<button>Load more />\n[2]<input />'}, 1)
    expect(reading.observation.content).toContain('[scroll_container scope=dialog scroll_up_px=80 scroll_down_px=420]')
    expect(reading.observation.content).toContain('[scroll_container scope=page scroll_up_px=150 scroll_down_px=450]')
  })

  it('ACC-04(c): performs bounded ~250ms async option probe when dropdown opens empty', async () => {
    document.body.innerHTML = `
      <form id="async-form">
        <label for="async-sel">部门</label>
        <input id="async-sel" role="combobox" readonly aria-haspopup="listbox"
          aria-controls="async-pop" aria-expanded="false">
        <ul id="async-pop" role="listbox" hidden></ul>
      </form>
    `
    const input = document.getElementById('async-sel') as HTMLInputElement
    const pop = document.getElementById('async-pop')!
    input.getBoundingClientRect = () => ({width: 120, height: 24} as DOMRect)
    pop.getBoundingClientRect = () => ({width: 120, height: 100} as DOMRect)

    let opt: HTMLLIElement | null = null
    const api = mockController()
    api.getBrowserState = vi.fn(async () => ({
      url: location.href,
      title: '',
      content: opt ? '[1]<input />\n[2]<li role=option>研发部 />' : '[1]<input />',
    }))
    api.clickElement = vi.fn(async idx => {
      if (idx === 1) {
        pop.hidden = false
        input.setAttribute('aria-expanded', 'true')
        setTimeout(() => {
          opt = document.createElement('li')
          opt.setAttribute('role', 'option')
          opt.textContent = '研发部'
          opt.getBoundingClientRect = () => ({width: 120, height: 24} as DOMRect)
          pop.appendChild(opt)
        }, 70)
      } else if (idx === 2 && opt) {
        opt.setAttribute('aria-selected', 'true')
        pop.hidden = true
        input.setAttribute('aria-expanded', 'false')
      }
      return {success: true}
    })
    const page = new Page(api, (_m, idx) => (idx === 1 ? input : idx === 2 ? opt as unknown as ElementLike | null : null), redactor)

    await page.select('e1', '10', '研发部')
    expect(api.clickElement).toHaveBeenNthCalledWith(1, 1)
    expect(api.clickElement).toHaveBeenNthCalledWith(2, 2)

    // Probe helper returns false within ~250ms if dropdown remains empty
    const emptyInput = document.createElement('input')
    emptyInput.setAttribute('role', 'combobox')
    emptyInput.setAttribute('aria-haspopup', 'listbox')
    emptyInput.setAttribute('aria-controls', 'empty-pop')
    const emptyPop = document.createElement('ul')
    emptyPop.id = 'empty-pop'
    emptyPop.setAttribute('role', 'listbox')
    emptyPop.getBoundingClientRect = () => ({width: 100, height: 40} as DOMRect)
    document.body.append(emptyInput, emptyPop)
    const start = Date.now()
    const found = await probeComboboxOptions(emptyInput, {timeoutMs: 120, intervalMs: 30})
    expect(found).toBe(false)
    expect(Date.now() - start).toBeLessThan(350)
  })

  it('ACC-04(d): ignores no-op attribute mutations and long-tail requests older than 1500ms', async () => {
    const div = document.createElement('div')
    div.setAttribute('data-state', 'same')
    document.body.append(div)

    expect(isSubstantiveMutation({
      type: 'attributes',
      target: div,
      attributeName: 'data-state',
      oldValue: 'same',
    } as unknown as MutationRecord)).toBe(false)

    div.setAttribute('data-state', 'changed')
    expect(isSubstantiveMutation({
      type: 'attributes',
      target: div,
      attributeName: 'data-state',
      oldValue: 'same',
    } as unknown as MutationRecord)).toBe(true)

    let fakeNow = 5000
    // One long-tail request started 2000ms ago (> 1500ms) -> ignored immediately without 5s stall
    const result = await waitForPageSettle({
      quietMs: 30,
      maxWaitMs: 500,
      longTailMs: 1500,
      now: () => fakeNow,
      activeRequestTimestamps: () => [fakeNow - 2000],
    })
    expect(result.settled).toBe(true)
    expect(result.reason).toBe('quiet')
    expect(result.ignoredLongTailRequests).toBe(1)

    // Watch ignores same-value attribute mutations and settles via executor
    const watch = beginPageSettleWatch({quietMs: 25, maxWaitMs: 250})
    div.setAttribute('data-state', 'changed') // same value as current!
    const settled = await watch.settle()
    expect(settled.settled).toBe(true)

    const page = {
      click: vi.fn(async () => ({success: true})),
      element: vi.fn(() => div),
    } as unknown as Page
    await expect(executeCommand(
      {command_id: 'c1', action: 'click', args: {ref: 'e1'}, expect_navigation: false},
      () => page,
      {},
      undefined,
      {quietMs: 20, maxWaitMs: 200},
    )).resolves.toEqual({success: true})
  })

  it('keeps waiting across continuous substantive DOM mutations until quiet window elapses', async () => {
    const host = document.createElement('div')
    document.body.append(host)
    const started = Date.now()
    let step = 0
    const mutator = window.setInterval(() => {
      step += 1
      const item = document.createElement('span')
      item.textContent = `row-${step}`
      host.append(item)
      if (step >= 7) {
        window.clearInterval(mutator)
      }
    }, 20)
    try {
      const watch = beginPageSettleWatch({quietMs: 70, maxWaitMs: 600})
      const result = await watch.settle()
      const totalElapsed = Date.now() - started
      expect(result.settled).toBe(true)
      expect(result.reason).toBe('quiet')
      expect(step).toBe(7)
      // 7 mutations at 20ms (~140ms) + 70ms quiet floor (~210ms); must never settle at 70ms!
      expect(totalElapsed).toBeGreaterThanOrEqual(170)
    } finally {
      window.clearInterval(mutator)
      host.remove()
    }
  })
})

describe('ACC-05: observation_summary on settled click/select/goto receipts', () => {
  it('builds bounded observation_summary with url_changed, epoch_changed, active_view, new_messages, dialog_title, row_count', () => {
    document.body.innerHTML = `
      <div role="alert">保存成功</div>
      <div role="dialog" aria-label="编辑客户">
        <table><tbody><tr><td>Row 1</td></tr><tr><td>Row 2</td></tr></tbody></table>
      </div>
    `
    const before = makeObservation(1, {url: 'https://app.test/list', messages: []}).observation
    const after = makeObservation(2, {
      url: 'https://app.test/detail',
      active_view: '客户详情',
      messages: [],
    }).observation

    const built = receipt(makeAction('click', {ref: 'e1'}), {
      ok: true,
      code: '',
      detail: '',
      observation: makeObservation(2, after),
      beforeObservation: before,
      navigated: true,
      elapsedMs: 35,
      urlBefore: before.url,
    }, redactor)

    expect(built.observation).toEqual(after)
    expect(built.observation_summary).toEqual({
      url_changed: true,
      epoch_changed: true,
      active_view: '客户详情',
      new_messages: ['保存成功'],
      dialog_title: '编辑客户',
      row_count: 2,
    })

    const summaryOnly = summarizeObservation(before, after, redactor)
    expect(summaryOnly.row_count).toBe(2)
  })
})

describe('ACC-13 + UX-03 + UX-05: form fill tracking, undoFormFill, restricted controls & auto-focus', () => {
  it('captures first-seen previous_fields, applied_fields, rejected_fields, restricted_fields, and supports undoFormFill & focusHandoffControl', async () => {
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    dialog.id = 'user-dlg'
    dialog.innerHTML = `
      <form id="user-form">
        <div class="el-form-item">
          <label class="el-form-item__label" for="u-name">姓名</label>
          <input id="u-name" name="nickname" value="旧名字" />
        </div>
        <div class="el-form-item">
          <label class="el-form-item__label" for="u-pwd">密码</label>
          <input id="u-pwd" type="password" name="password" />
        </div>
        <div class="el-form-item">
          <label class="el-form-item__label">营业执照上传</label>
          <div class="el-upload">
            <button id="upload-btn" type="button">选择文件</button>
            <input id="u-file" type="file" style="display: none" />
          </div>
        </div>
        <button id="save-btn" type="submit">保存</button>
      </form>
    `
    document.body.append(dialog)
    const nameInput = document.getElementById('u-name') as HTMLInputElement
    const pwdInput = document.getElementById('u-pwd') as HTMLInputElement
    const fileInput = document.getElementById('u-file') as HTMLInputElement
    const uploadBtn = document.getElementById('upload-btn') as HTMLButtonElement
    const saveBtn = document.getElementById('save-btn') as HTMLButtonElement

    for (const el of [nameInput, pwdInput, uploadBtn, saveBtn]) {
      el.getBoundingClientRect = () => ({width: 120, height: 28} as DOMRect)
      el.scrollIntoView = vi.fn()
    }
    fileInput.getBoundingClientRect = () => ({width: 0, height: 0} as DOMRect)

    // Element Plus helpers find both password and hidden file upload trigger
    const elRestricted = findElRestrictedControls(dialog)
    expect(elRestricted.map((r: {label: string; kind: string}) => [r.label, r.kind])).toEqual([
      ['密码', 'password'],
      ['营业执照上传', 'file'],
    ])
    expect(focusElRestrictedControl(fileInput)).toBe(true)
    expect(document.activeElement).toBe(uploadBtn)

    // Tracker captures first-seen pre-fill value across multiple fills in the same turn
    const tracker = new FormFillTracker(redactor)
    const before1 = tracker.captureCurrentValue(nameInput)
    nameInput.value = '第一次'
    tracker.recordApplied('turn-1', 'nickname', nameInput, before1, '第一次')

    const before2 = tracker.captureCurrentValue(nameInput)
    nameInput.value = '第二次'
    tracker.recordApplied('turn-1', 'nickname', nameInput, before2, '第二次')

    tracker.recordRejected('turn-1', 'deptId', 'option_unavailable', ['研发部', '财务部'], nameInput)
    expect(tracker.receiptFields('turn-1')).toEqual({
      previousFields: {nickname: '旧名字'},
      appliedFields: {nickname: '第二次'},
      rejectedFields: {deptId: {reason: 'option_unavailable', candidates: ['研发部', '财务部']}},
      restrictedFields: ['密码', '营业执照上传'],
    })

    // Runtime wires undoFormFill & focusHandoffControl onto PanelHost
    const runtime = new EmbeddedWebRuntime({
      endpoint: 'https://agent.test',
      sessionId: 's1',
      clientToken: 'tok',
      sessionGeneration: 'gen',
      panel: false,
    })
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: makeActions()})
    runtime.panelConfiguration = DEFAULT_CONFIGURATION
    runtime.panelHost = host
    runtime._attachPanel()

    host.applyTimeline({
      entries: [{
        id: 'res-1',
        sequence: 1,
        turn_id: 'turn-1',
        at: Date.now(),
        kind: 'result',
        tone: 'success',
        text: '已预填表单',
        handoff: {
          completed_fields: [{name: '姓名', value: '第二次'}],
          pending_fields: [],
          restricted_controls: [{label: '密码', kind: 'password'}, {label: '营业执照上传', kind: 'file'}],
          can_undo: true,
        },
      }],
    })

    // Seed runtime's own tracker and trigger handoff auto-focus + PanelHost undo/focus
    const api = mockController()
    runtime.controller = api
    runtime.getElementByIndex = (_m, idx) => (idx === 1 ? nameInput : idx === 2 ? saveBtn : null)
    runtime.currentObservation = makeObservation(1, {
      render_state: 'ready',
      elements: [
        {ref: 'e1', role: 'textbox', name: '姓名', tag: 'input', epoch: 1,
          attrs: {field: 'nickname', form_scope: 'dialog:user-dlg', value: '第二次'}},
        {ref: 'e2', role: 'button', name: '保存', tag: 'button', epoch: 1,
          attrs: {form_scope: 'dialog:user-dlg'}},
      ],
    })
    runtime.stored.activeMessageId = 'turn-1'
    runtime.formFillTracker.recordApplied('turn-1', 'nickname', nameInput, {value: '旧名字'}, '第二次')

    // handoff_form auto-focuses first restricted control (password)
    await runtime._execute({
      command_id: 'h1',
      action: 'handoff_form',
      args: {
        fields: [{ref: 'e1', field: 'nickname', control: 'textbox', scope: 'dialog:user-dlg', value: '第二次'}],
        submit: {ref: 'e2', label: '保存', scope: 'dialog:user-dlg'},
      },
      expect_navigation: false,
    })
    expect(pwdInput.scrollIntoView).toHaveBeenCalled()
    expect(document.activeElement).toBe(pwdInput)

    // PanelHost focusHandoffControl focuses the hidden file input's visible upload button
    await expect(host.focusHandoffControl({label: '营业执照上传', kind: 'file'})).resolves.toBe(true)
    expect(uploadBtn.scrollIntoView).toHaveBeenCalled()
    expect(document.activeElement).toBe(uploadBtn)

    // PanelHost undoFormFill restores original pre-fill value and updates handoff card state
    const inputEvents: string[] = []
    nameInput.addEventListener('input', () => inputEvents.push(nameInput.value))
    await expect(host.undoFormFill('turn-1')).resolves.toBe(true)
    expect(nameInput.value).toBe('旧名字')
    expect(inputEvents).toEqual(['旧名字'])
    expect(host.snapshot().entries[0]?.handoff).toMatchObject({can_undo: false, undone: true})

    // Second undo returns false; closed form undo returns false
    await expect(host.undoFormFill('turn-1')).resolves.toBe(false)

    // New turn undo refuses if the dialog closed
    nameInput.value = '新名字'
    runtime.formFillTracker.recordApplied('turn-2', 'nickname', nameInput, {value: '旧名字'}, '新名字')
    dialog.style.display = 'none'
    await expect(host.undoFormFill('turn-2')).resolves.toBe(false)
    expect(nameInput.value).toBe('新名字')
    runtime.stop()
  })

  it('focusControl matches generic restricted kind sent by server adapters', () => {
    document.body.innerHTML = ''
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    const label = document.createElement('label')
    label.textContent = '附件上传'
    const fileInput = document.createElement('input')
    fileInput.type = 'file'
    fileInput.id = 'upload-box'
    label.setAttribute('for', 'upload-box')
    fileInput.focus = vi.fn()
    fileInput.scrollIntoView = vi.fn()
    dialog.append(label, fileInput)
    document.body.appendChild(dialog)

    const tracker = new FormFillTracker(redactor)
    const ok = tracker.focusControl({label: '附件上传', kind: 'restricted'})
    expect(ok).toBe(true)
    expect(fileInput.scrollIntoView).toHaveBeenCalled()
    expect(fileInput.focus).toHaveBeenCalled()
  })

  it('preserves typed model props, multi-turn baseline, and unset keys during undoTurn; refuses combobox DOM fallback atomically', async () => {
    document.body.innerHTML = ''
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    dialog.innerHTML = `
      <form class="el-form">
        <label for="f-cust">客户</label>
        <input id="f-cust" role="combobox" readonly aria-controls="lb-cust" value="" />
        <ul id="lb-cust" role="listbox"><li role="option" aria-selected="false">合成星河</li></ul>
        <label for="f-note">备注</label>
        <input id="f-note" name="remark" type="text" value="" />
      </form>
    `
    document.body.append(dialog)
    const custInput = document.getElementById('f-cust') as HTMLInputElement
    const noteInput = document.getElementById('f-note') as HTMLInputElement
    for (const el of [custInput, noteInput]) {
      el.getBoundingClientRect = () => ({width: 140, height: 28} as DOMRect)
    }

    // Readonly combobox without formModelReader -> DOM fallback refuses atomically without corrupting remark
    const domTracker = new FormFillTracker(redactor)
    const bNote = domTracker.captureCurrentValue(noteInput)
    noteInput.value = '新备注'
    domTracker.recordApplied('t-dom', '备注', noteInput, bNote, '新备注')
    const bCust = domTracker.captureCurrentValue(custInput)
    custInput.value = '合成星河'
    domTracker.recordApplied('t-dom', '客户', custInput, bCust, '合成星河')
    await expect(domTracker.undoTurn('t-dom')).resolves.toBe(false)
    expect(noteInput.value).toBe('新备注')
    expect(custInput.value).toBe('合成星河')

    // With formModelReader + restoreFormModel: keeps typed number + unset + cross-turn baseline even with password withheld
    const tracker = new FormFillTracker(redactor)
    const model: Record<string, unknown> = {customerId: undefined, remark: '初始', password: 'secret'}
    const readFormModel = () => ({
      model: Object.fromEntries(Object.entries(model).filter(([k, v]) => k !== 'password' && v !== undefined)),
      items: [
        {prop: 'customerId', label: '客户', display: ''},
        {prop: 'remark', label: '备注', display: ''},
      ],
      mode: 'create' as const,
      record_id: '',
      title: '新建',
      check: 'chk',
      withheld: ['password'],
      invalid: ['customerId'],
    })
    const restoreCalls: unknown[] = []
    const restoreFormModel = vi.fn(async (_el: unknown, req: unknown) => {
      restoreCalls.push(req)
      const typed = req as {values?: Record<string, unknown>; unset?: string[]; allowInvalid?: boolean}
      expect(typed.allowInvalid).toBe(true)
      for (const [k, v] of Object.entries(typed.values ?? {})) model[k] = v
      for (const k of typed.unset ?? []) model[k] = undefined
      return true
    })

    // Turn 1 sets customerId (was undefined/unset) and remark='第一轮'
    noteInput.value = '初始'
    const beforeNoteT1 = tracker.captureCurrentValue(noteInput, readFormModel)
    const beforeCustT1 = tracker.captureCurrentValue(custInput, readFormModel)
    model.remark = '第一轮'
    noteInput.value = '第一轮'
    tracker.recordApplied('t1', '备注', noteInput, beforeNoteT1, '第一轮')
    model.customerId = 42
    custInput.value = '合成星河'
    tracker.recordApplied('t1', '客户', custInput, beforeCustT1, '合成星河')

    // Turn 2 updates remark='第二轮' on the SAME open form -> must keep Turn 1 baseline '初始'!
    const beforeNoteT2 = tracker.captureCurrentValue(noteInput, readFormModel)
    model.remark = '第二轮'
    noteInput.value = '第二轮'
    tracker.recordApplied('t2', '备注', noteInput, beforeNoteT2, '第二轮')
    expect(tracker.receiptFields('t2')).toMatchObject({previousFields: {备注: '初始'}})

    await expect(tracker.undoTurn('t1', {readFormModel, restoreFormModel})).resolves.toBe(true)
    expect(model.remark).toBe('初始')
    expect(model.customerId).toBeUndefined()
    expect(model.password).toBe('secret')
    expect(restoreCalls[0]).toMatchObject({
      values: {remark: '初始'},
      unset: ['customerId'],
      allowInvalid: true,
    })
  })
})

describe('hidden overlay ancestors and zero-rect browser dialogs', () => {
  it('excludes dialogs inside display:none overlay ancestors and 0x0 dialogs when root has layout', () => {
    document.documentElement.getBoundingClientRect = () => ({width: 1280, height: 800} as DOMRect)

    const overlay = document.createElement('div')
    overlay.style.display = 'none'
    const hiddenDialog = document.createElement('div')
    hiddenDialog.setAttribute('role', 'dialog')
    hiddenDialog.setAttribute('aria-label', '菜单搜索')
    hiddenDialog.getBoundingClientRect = () => ({width: 600, height: 400} as DOMRect)
    overlay.appendChild(hiddenDialog)
    document.body.appendChild(overlay)

    const afterObs = makeObservation(2).observation
    const summaryHidden = summarizeObservation(null, afterObs, redactor, document)
    expect(summaryHidden.dialog_title).toBe('')

    const zeroDialog = document.createElement('div')
    zeroDialog.setAttribute('role', 'dialog')
    zeroDialog.setAttribute('aria-label', '隐藏空弹窗')
    zeroDialog.getBoundingClientRect = () => ({width: 0, height: 0} as DOMRect)
    document.body.appendChild(zeroDialog)

    const summaryZero = summarizeObservation(null, afterObs, redactor, document)
    expect(summaryZero.dialog_title).toBe('')

    overlay.style.display = 'block'
    const summaryVisible = summarizeObservation(null, afterObs, redactor, document)
    expect(summaryVisible.dialog_title).toBe('菜单搜索')
  })
})
