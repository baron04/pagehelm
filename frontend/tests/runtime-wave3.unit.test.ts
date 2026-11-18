import {afterEach, describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
// @ts-expect-error Generic Element Plus modules ship as framework-free JS.
import {findElRestrictedControls, focusElRestrictedControl, isElFormOpenAndWritable} from '../src/integrations/element-plus/form.js'
// @ts-expect-error Generic Element Plus modules ship as framework-free JS.
import {isElSelectClearedWithoutSelection, probeElSelectOptions, readElSelectVisibleCandidates} from '../src/integrations/element-plus/select.js'
import {probeComboboxOptions, visibleComboboxOptions} from '../src/runtime/combobox'
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
import type {ActionEnvelope, ActionResultEnvelope, ElementRef, ObservationEnvelope} from '../src/runtime/types'

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

describe('ACC-01: combobox cleared without selection emits option_unavailable + no_effect + visible candidates', () => {
  it('detects cleared input on filterable combobox fill and reports only visible options', async () => {
    const form = document.createElement('form')
    form.id = 'order-form'
    form.innerHTML = `
      <label for="cust">客户</label>
      <input id="cust" role="combobox" aria-haspopup="listbox" aria-controls="cust-pop" aria-expanded="true">
      <ul id="cust-pop" role="listbox">
        <li id="opt-vis" role="option">华南科技</li>
        <li id="opt-hidden" role="option" style="display: none">隐藏客户</li>
        <li id="opt-disabled" role="option" aria-disabled="true">禁用客户</li>
      </ul>
    `
    document.body.append(form)
    const input = document.getElementById('cust') as HTMLInputElement
    const pop = document.getElementById('cust-pop')!
    const optVis = document.getElementById('opt-vis')!
    const optHidden = document.getElementById('opt-hidden')!
    const optDisabled = document.getElementById('opt-disabled')!
    for (const el of [input, pop, optVis, optDisabled]) {
      el.getBoundingClientRect = () => ({width: 120, height: 24} as DOMRect)
    }
    optHidden.getBoundingClientRect = () => ({width: 0, height: 0} as DOMRect)

    const api = mockController()
    api.inputText = vi.fn(async () => {
      // Simulate widget rejecting/clearing unmatched filter text on input/blur
      input.value = ''
      return {success: true}
    })
    const page = new Page(api, (_m, idx) => (idx === 1 ? input : null), redactor)

    await expect(page.input('e1', '不存在客户', true)).rejects.toMatchObject({
      code: 'option_unavailable',
      no_effect: true,
      candidate_labels: ['华南科技'],
    })
    expect(visibleComboboxOptions(input)).toEqual(['华南科技'])
  })

  it('emits option_unavailable + no_effect + candidate_labels on select when clicking clears value with no selection', async () => {
    const form = document.createElement('form')
    form.id = 'select-clear-form'
    form.innerHTML = `
      <label for="owner">负责人</label>
      <input id="owner" role="combobox" readonly aria-haspopup="listbox" aria-controls="owner-pop" aria-expanded="true">
      <ul id="owner-pop" role="listbox">
        <li id="o1" role="option">Alice</li>
        <li id="o2" role="option">Bob</li>
      </ul>
    `
    document.body.append(form)
    const input = document.getElementById('owner') as HTMLInputElement
    const pop = document.getElementById('owner-pop')!
    const o1 = document.getElementById('o1')!
    const o2 = document.getElementById('o2')!
    for (const el of [input, pop, o1, o2]) {
      el.getBoundingClientRect = () => ({width: 120, height: 24} as DOMRect)
    }
    const api = mockController()
    api.getBrowserState = vi.fn(async () => ({
      url: location.href,
      title: '',
      content: '[1]<input />\n[2]<li role=option>Alice />\n[3]<li role=option>Bob />',
    }))
    // Click clears input and leaves no selected option
    api.clickElement = vi.fn(async () => {
      input.value = ''
      o1.setAttribute('aria-selected', 'false')
      return {success: true}
    })
    const page = new Page(api, (_m, idx) => (idx === 1 ? input : idx === 2 ? o1 : o2), redactor)

    await expect(page.select('e1', '1', 'Alice')).rejects.toMatchObject({
      code: 'option_unavailable',
      no_effect: true,
      candidate_labels: ['Alice', 'Bob'],
    })
  })

  it('Element Plus select helpers detect cleared wrapper and visible dropdown options', async () => {
    document.body.innerHTML = `
      <div class="el-select">
        <div class="el-select__wrapper">
          <div class="el-select__selected-item el-select__placeholder is-transparent">请选择</div>
          <input class="el-select__input" role="combobox" aria-haspopup="listbox" aria-controls="el-pop-1">
        </div>
      </div>
      <div class="el-select-dropdown">
        <ul id="el-pop-1" role="listbox">
          <li class="el-select-dropdown__item">选项A</li>
          <li class="el-select-dropdown__item" style="display: none">隐藏B</li>
          <li class="el-select-dropdown__item is-disabled">禁用C</li>
        </ul>
      </div>
    `
    const input = document.querySelector('input') as HTMLInputElement
    const items = document.querySelectorAll('.el-select-dropdown__item')
    document.querySelector('.el-select-dropdown')!.getBoundingClientRect = () => ({width: 200, height: 120} as DOMRect)
    items[0]!.getBoundingClientRect = () => ({width: 180, height: 32} as DOMRect)
    items[1]!.getBoundingClientRect = () => ({width: 0, height: 0} as DOMRect)
    items[2]!.getBoundingClientRect = () => ({width: 180, height: 32} as DOMRect)

    expect(isElSelectClearedWithoutSelection(input)).toBe(true)
    expect(readElSelectVisibleCandidates(input)).toEqual(['选项A'])
    expect(await probeElSelectOptions(input, {timeoutMs: 80})).toBe(true)
  })
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

describe('ACC-06: form_closed_or_readonly on closed/detached/readonly targets', () => {
  it('returns structured form_closed_or_readonly receipt when dialog closes or field becomes readonly', async () => {
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    dialog.innerHTML = '<form id="edit-f"><input name="title" /></form>'
    document.body.append(dialog)
    const input = dialog.querySelector('input') as HTMLInputElement

    const api = mockController()
    const page = new Page(api, () => input, redactor)
    const before = makeObservation(1)
    const after = makeObservation(2)
    const sent: ActionResultEnvelope[] = []

    const runtime = Object.assign(Object.create(EmbeddedWebRuntime.prototype, {
      redactor: {value: redactor},
      page: {value: page},
    }), {
      config: undefined,
      stored: {pageWorked: true, activeMessageId: 'turn-1', inFlight: null},
      writeGuardUntil: 0,
      currentObservation: before,
      _save: vi.fn(),
      _mayNavigate: () => false,
      _execute: (cmd: ActionEnvelope['command']) => executeCommand(cmd, () => page, {}),
      observe: vi.fn(async () => after),
      _deliver: vi.fn(async (res: ActionResultEnvelope) => { sent.push(res) }),
    }) as {
      _dispatch(...args: unknown[]): Promise<unknown>
      _settle(...args: unknown[]): Promise<void>
    }

    // Close dialog via display: none
    dialog.style.display = 'none'
    const action = makeAction('fill', {ref: 'e1', value: 'Acme'})
    const outcome = await runtime._dispatch(action, action, {})
    await runtime._settle(action, before, outcome)

    expect(sent[0]?.receipt).toMatchObject({
      ok: false,
      error_code: 'form_closed_or_readonly',
    })
    expect(api.inputText).not.toHaveBeenCalled()

    // Element Plus helper also rejects hidden dialog & disabled wrapper
    expect(isElFormOpenAndWritable(input)).toBe(false)
    dialog.style.display = 'block'
    expect(isElFormOpenAndWritable(input)).toBe(true)
    input.readOnly = true
    expect(isElFormOpenAndWritable(input)).toBe(false)
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

  it('propagates ACC-01 combobox fill option_unavailable + rejected_fields and ACC-06 closed handoff receipts end-to-end', async () => {
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    dialog.id = 'acc01-dlg'
    dialog.innerHTML = `
      <form>
        <label for="c-box">客户</label>
        <input id="c-box" name="customerId" role="combobox" aria-haspopup="listbox"
          aria-controls="c-box-pop" aria-expanded="true">
        <ul id="c-box-pop" role="listbox">
          <li role="option">蓝海集团</li>
        </ul>
      </form>
    `
    document.body.append(dialog)
    const input = document.getElementById('c-box') as HTMLInputElement
    const pop = document.getElementById('c-box-pop')!
    const opt = pop.querySelector('li')!
    for (const el of [input, pop, opt]) {
      el.getBoundingClientRect = () => ({width: 100, height: 24} as DOMRect)
    }

    const api = mockController()
    api.inputText = vi.fn(async () => {
      input.value = ''
      return {success: true}
    })
    const page = new Page(api, () => input, redactor)
    const before = makeObservation(1, {
      elements: [{
        ref: 'e1', role: 'combobox', name: '客户', tag: 'input', epoch: 1,
        attrs: {field: 'customerId', form_scope: 'dialog:acc01-dlg'},
      }],
    })
    const after = makeObservation(2)
    const sent: ActionResultEnvelope[] = []

    const runtime = Object.assign(Object.create(EmbeddedWebRuntime.prototype, {
      redactor: {value: redactor},
      page: {value: page},
    }), {
      config: undefined,
      stored: {pageWorked: true, activeMessageId: 'turn-e2e', inFlight: null},
      writeGuardUntil: 0,
      currentObservation: before,
      _save: vi.fn(),
      _mayNavigate: () => false,
      _execute: (cmd: ActionEnvelope['command']) => executeCommand(cmd, () => page, {}),
      observe: vi.fn(async () => after),
      _deliver: vi.fn(async (res: ActionResultEnvelope) => { sent.push(res) }),
    }) as {
      _dispatch(...args: unknown[]): Promise<unknown>
      _settle(...args: unknown[]): Promise<void>
      formFillTracker: FormFillTracker
    }

    const fillAction = makeAction('fill', {ref: 'e1', value: '不存在', commit: true}, {turn_id: 'turn-e2e'})
    const outcome = await runtime._dispatch(fillAction, fillAction, {})
    await runtime._settle(fillAction, before, outcome)

    expect(sent[0]?.receipt).toMatchObject({
      ok: false,
      error_code: 'option_unavailable',
      no_effect: true,
      candidate_labels: ['蓝海集团'],
      rejected_fields: {
        customerId: {reason: 'option_unavailable', candidates: ['蓝海集团']},
      },
    })
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
})
