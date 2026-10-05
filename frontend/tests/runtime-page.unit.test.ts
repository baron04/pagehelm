import {describe, expect, it, vi} from 'vitest'
import {Page, pageControllerOptions, type BrowserState, type PageControllerApi}
  from '../src/runtime/page'
import {createRedactor} from '../src/runtime/redaction'
import {formInstance} from '../src/runtime/formInstance'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'

function controller(): PageControllerApi {
  return {
    selectorMap: {},
    getBrowserState: vi.fn(),
    clickElement: vi.fn(),
    inputText: vi.fn(),
    selectOption: vi.fn(),
    scroll: vi.fn(),
    dispose: vi.fn(),
  }
}

describe('the product PageController extraction policy', () => {
  it('accepts explicit application loading while unknown blocker values fail closed', () => {
    const read = (value: unknown) => new Page(controller(), () => null, createRedactor(),
      undefined, undefined, undefined, undefined, () => value).readiness()
    expect(read('pending')).toEqual({blocked: false, pending: true})
    expect(read(false)).toEqual({blocked: false, pending: false})
    for (const value of [true, null, undefined, 'ready', 0, {}]) {
      expect(read(value)).toEqual({blocked: true, pending: false})
    }
    const failed = new Page(controller(), () => null, createRedactor(),
      undefined, undefined, undefined, undefined, () => { throw new Error('unavailable') })
    expect(failed.readiness()).toEqual({blocked: true, pending: false})
  })
  it('observes only the current viewport', () => {
    expect(pageControllerOptions([]).viewportExpansion).toBe(0)
  })

  it('includes generic accessible control state in the bounded observation', () => {
    expect(pageControllerOptions([]).includeAttributes).toEqual(
      expect.arrayContaining(['aria-selected', 'aria-checked', 'aria-expanded']))
  })
})

describe('Page.describe bounds an observation before it crosses the browser boundary', () => {
  it('projects an application query only with page identity and intact bounded nonsecret values', () => {
    const query = {path: '/api/contacts', parameters: {domain: 'example.test'}}
    const read = (value: unknown, path: string | undefined = '/contacts') => new Page(controller(), () => null,
      createRedactor(), undefined, undefined, undefined, undefined, undefined, undefined,
      () => path, () => value).describe({url: 'https://app.test/contacts', title: '', content: ''}, 1).observation
    expect(read(query).page_query).toEqual(query)
    expect(read(query, '').page_query).toBeUndefined()
    for (const value of [null, {path: '//other/api', parameters: {}},
      {path: '/api/contacts?token=x', parameters: {}},
      {path: '/api/contacts', parameters: {token: 'private'}},
      {path: '/api/contacts', parameters: {domain: 'x'.repeat(257)}},
      {path: '/api/contacts', parameters: {domain: ['one', 'two']}}]) {
      expect(read(value).page_query).toBeUndefined()
    }
  })
  it('removes wrapper-identified credentials from both text and element projections', () => {
    const wrapper = document.createElement('div')
    wrapper.setAttribute('name', 'api_key')
    wrapper.innerHTML = '<input>'
    document.body.append(wrapper)
    try {
      const input = wrapper.querySelector('input')!
      input.value = 'private-credential'
      const page = new Page(controller(), () => input, createRedactor())
      const reading = page.describe({url: 'https://app.test/form', title: 'Form',
        content: '[2]<input type=text value=private-credential>private-credential />'}, 1)
      expect(JSON.stringify(reading.observation)).not.toContain('private-credential')
      expect(reading.observation.elements[0]?.role).toBe('password')
      expect(input.value).toBe('private-credential')
    } finally { wrapper.remove() }
  })

  it('accepts a bounded application-reported rendered view without a switch control', () => {
    const read = (value: unknown) => new Page(controller(), () => null, createRedactor(),
      undefined, undefined, undefined, undefined, undefined, () => value).describe({
      url: 'https://app.test/items', title: 'Items', content: '',
    }, 1).observation
    expect(read('List').active_view).toBe('List')
    expect(read('List').selected_controls).toEqual([])
    for (const invalid of [null, 1, 'x'.repeat(121), 'password']) {
      expect(read(invalid).active_view).toBeUndefined()
    }
  })
  it('accepts only a bounded application record route as an observation', () => {
    const make = (route: unknown) => new Page(controller(), () => null,
      createRedactor(), undefined, undefined, undefined, () => route).describe({
        url: 'https://app.test/items/4', title: 'Item', content: '',
      }, 1).observation.record_route
    expect(make('/items/4')).toBe('/items/4')
    for (const bad of ['//other.test/x', '/items/4?token=x', '/a/../b',
      '/items/4#tab', '/items/4 password']) expect(make(bad)).toBeUndefined()
    expect(make(null)).toBeUndefined()
  })
  it('publishes only an explicit bounded application main-page identity', () => {
    const read = (hook?: () => unknown) => new Page(controller(), () => null,
      createRedactor(), undefined, undefined, undefined, undefined, undefined, undefined,
      hook).describe({url: 'https://app.test/help', title: 'Help',
        content: '[1]<a href=/contacts>Header menu />'}, 1).observation
    expect(read(() => '/help').page_route).toBe('/help')
    expect(read(() => '/old-page').page_route).toBe('/old-page')
    expect(read().page_route).toBeUndefined()
    expect(read(() => { throw new Error('unavailable') }).page_route).toBeUndefined()
    for (const route of [null, 1, '', '//other.test/help', '/help?token=x', '/a/../help',
      '/help#tab', '/a\\b', '/a b', '/' + 'x'.repeat(1000)]) {
      expect(read(() => route).page_route).toBeUndefined()
    }
    expect(read(() => '/help').record_route).toBeUndefined()
  })
  it('publishes only bounded, valid, non-sensitive application facet choices', () => {
    const choices = [
      {ref: 'fc_1', kind: 'group', name: 'Salesperson', active: false},
      {ref: 'fc_2', kind: 'filter', name: '  Open  ', active: true},
      {ref: 'bad ref', kind: 'group', name: 'Discard', active: false},
      {ref: 'fc_3', kind: 'group', name: 'password', active: false},
      ...Array.from({length: 40}, (_, index) => ({
        ref: `fc_${index + 10}`, kind: 'filter', name: `Choice ${index}`, active: false,
      })),
    ]
    const page = new Page(controller(), () => null, createRedactor(), undefined,
      () => choices, vi.fn())
    const reading = page.describe({url: 'https://app.test/orders', title: 'Orders',
      content: ''}, 1)
    expect(reading.observation.facet_candidates?.slice(0, 2)).toEqual([
      {ref: 'fc_1', kind: 'group', name: 'Salesperson', active: false},
      {ref: 'fc_2', kind: 'filter', name: 'Open', active: true},
    ])
    expect(reading.observation.facet_candidates?.length).toBeLessThanOrEqual(32)
    expect(reading.observation.facet_candidates?.some(item => item.name === 'password'))
      .toBe(false)
  })
  it('accepts bounded typed facets and ignores invalid application facts', () => {
    const page = new Page(controller(), () => null, createRedactor(), () => [
      {kind: 'filter', name: '  Open  '}, {kind: 'group', name: 'Customer'},
      {kind: 'candidate', name: 'Not applied'}, {kind: 'group', name: 'Customer'},
    ])
    const reading = page.describe({url: 'https://app.test/orders', title: 'Orders',
      content: ''}, 1)
    expect(reading.observation.page_facets).toEqual([
      {kind: 'filter', name: 'Open'}, {kind: 'group', name: 'Customer'},
    ])
    expect(new Page(controller(), () => null, createRedactor()).describe({
      url: 'https://app.test/orders', title: 'Orders', content: '',
    }, 1).observation.page_facets).toBeUndefined()
  })
  it('keeps 200 complete records, preserves the scroll hint and reports truncation', () => {
    const api = controller()
    const page = new Page(api, (_map, index) => ({
      tagName: 'BUTTON', textContent: `Action ${index}`, isConnected: true,
    }), createRedactor())
    const state: BrowserState = {
      url: 'https://app.test/large', title: 'Large', header: '[Start of page]',
      content: Array.from({length: 205}, (_, index) =>
        `[${index}]<button>Action ${index} />`).join('\n'),
      footer: '... 9000 pixels below - scroll to see more ...',
    }

    const reading = page.describe(state, 7)

    expect(reading.observation.elements).toHaveLength(200)
    expect(reading.observation.elements.at(-1)?.ref).toBe('e199')
    expect(reading.observation.content).not.toContain('[200]<button>')
    expect(reading.observation.content).toContain('scroll to see more')
    expect(reading.observation.truncated).toBe(true)
    expect(reading.fingerprints.size).toBe(200)
  })

  it('keeps the footer inside the character limit when non-interactive text is huge', () => {
    const page = new Page(controller(), () => null, createRedactor())
    const reading = page.describe({
      url: 'https://app.test/large', title: 'Large', header: 'header',
      content: 'x'.repeat(140_000), footer: 'footer-scroll-hint',
    }, 1)

    expect(reading.observation.content.length).toBeLessThanOrEqual(120_000)
    expect(reading.observation.content.endsWith('footer-scroll-hint')).toBe(true)
    expect(reading.observation.truncated).toBe(true)
  })
})

describe('Page refuses controls that are not writable at execution time', () => {
  it('commits a local numeric or date draft while ordinary typing keeps its current focus', async () => {
    const form = document.createElement('form')
    form.id = 'draft-types'
    form.innerHTML = '<label>Amount<input type="number"></label><label>Day<input type="date"></label>'
    document.body.append(form)
    try {
      for (const [index, input] of Array.from(form.querySelectorAll('input')).entries()) {
        const changed = vi.fn(), blurred = vi.fn()
        input.addEventListener('change', changed)
        input.addEventListener('blur', blurred)
        const api = controller()
        api.inputText = vi.fn(async (_i, value) => {input.focus(); input.value = value; return {success: true}})
        const page = new Page(api, () => input, createRedactor())
        const value = index === 0 ? '12.50' : '2026-10-05'
        await page.input('e1', value)
        expect(changed).not.toHaveBeenCalled()
        expect(document.activeElement).toBe(input)
        await page.input('e1', value, true)
        expect(changed).toHaveBeenCalledTimes(1)
        expect(blurred).toHaveBeenCalledTimes(1)
        const observed = [{ref: 'e1', role: index === 0 ? 'spinbutton' : 'textbox',
          name: '', tag: 'input', epoch: 1, attrs: {field_label: index === 0 ? 'Amount' : 'Day', form_scope: 'form:draft-types'}}]
        expect(page.verifyFormFields([{ref: 'e1', field: 'value', binding: 'label',
          label: index === 0 ? 'Amount' : 'Day', control: observed[0]!.role,
          scope: 'form:draft-types', value}], observed)).toBe(true)
        if (index === 0) {
          const binding = [{ref: 'e1', field: 'value', binding: 'label', label: 'Amount',
            control: 'spinbutton', scope: 'form:draft-types', value: '12.5'}]
          expect(page.verifyFormFields(binding, observed)).toBe(true)
          input.value = '12.5001'
          expect(page.verifyFormFields(binding, observed)).toBe(false)
          input.value = ''; binding[0]!.value = '0'
          expect(page.verifyFormFields(binding, observed)).toBe(false)
          input.value = '9007199254740992'; binding[0]!.value = '9007199254740993'
          expect(page.verifyFormFields(binding, observed)).toBe(false)
          input.value = '1.25e1'; binding[0]!.value = '12.50'
          expect(page.verifyFormFields(binding, observed)).toBe(true)
        }
      }
    } finally {form.remove()}
  })
  it('checks a scoped unnamed input and its native submit button without clicking', () => {
    const form = document.createElement('form')
    form.id = 'native-editor'
    form.innerHTML = '<label for="n">Name</label><input id="n"><button type="submit">Confirm</button>'
    document.body.append(form)
    try {
      const input = form.querySelector('input')!, button = form.querySelector('button')!
      input.value = 'Draft'
      const clicked = vi.fn(); button.addEventListener('click', clicked)
      const page = new Page(controller(), (_map, index) => index === 1 ? input : button, createRedactor())
      const observed = [
        {ref: 'e1', role: 'textbox', name: '', tag: 'input', epoch: 1,
          attrs: {field_label: 'Name', form_scope: 'form:native-editor'}},
        {ref: 'e2', role: 'button', name: 'Confirm', tag: 'button', epoch: 1,
          attrs: {form_scope: 'form:native-editor'}},
      ]
      const fields = [{ref: 'e1', field: 'name', label: 'Name', binding: 'label',
        scope: 'form:native-editor', control: 'textbox', value: 'Draft'}]
      const submit = {ref: 'e2', label: 'Confirm', scope: 'form:native-editor'}
      expect(page.verifyFormFields(fields, observed)).toBe(true)
      expect(page.verifyFormSubmit(submit, fields, observed)).toBe(true)
      expect(clicked).not.toHaveBeenCalled()
      input.name = 'different'
      expect(page.verifyFormFields(fields, observed)).toBe(false)
      input.removeAttribute('name')
      button.disabled = true
      expect(page.verifyFormSubmit(submit, fields, observed)).toBe(false)
      button.disabled = false
      expect(page.verifyFormSubmit(submit, fields, [...observed, {...observed[1]!, ref: 'e3'}])).toBe(false)
      form.id = 'replaced'
      expect(page.verifyFormFields(fields, observed)).toBe(false)
    } finally { form.remove() }
  })
  it('rechecks only actually entered controls outside the viewport in the same live form', async () => {
    const form = document.createElement('form')
    form.id = 'entered-editor'
    form.innerHTML = '<label for="off">Name</label><input id="off"><button type="submit">Save</button>'
    document.body.append(form)
    try {
      const input = form.querySelector('input')!
      vi.spyOn(input, 'getBoundingClientRect').mockReturnValue({width: 20, height: 20} as DOMRect)
      const api = controller()
      api.inputText = vi.fn(async (_i, value) => {input.value = value; return {success: true}})
      const page = new Page(api, () => input, createRedactor())
      const binding = [{field: 'name', label: 'Name', binding: 'label',
        scope: 'form:entered-editor', control: 'textbox', value: 'Draft'}]
      const observed = () => [{ref: 'e3', role: 'button', name: 'Save', tag: 'button', epoch: 4,
        attrs: {form_scope: 'form:entered-editor', form_instance: formInstance(input)!}}]
      expect(page.verifyFormFields(binding, observed())).toBe(false)
      await page.input('e1', 'Draft')
      expect(page.verifyFormFields(binding, observed())).toBe(true)
      expect(page.verifyFormFields(binding, [])).toBe(false)
      input.value = 'Changed'
      expect(page.verifyFormFields(binding, observed())).toBe(false)
      input.value = 'Draft'; input.disabled = true
      expect(page.verifyFormFields(binding, observed())).toBe(false)
      input.disabled = false; input.hidden = true
      expect(page.verifyFormFields(binding, observed())).toBe(false)
      input.hidden = false
      form.setAttribute('hidden', ''); form.removeAttribute('hidden')
      expect(page.verifyFormFields(binding, observed())).toBe(false)
      await page.input('e1', 'Draft')
      expect(page.verifyFormFields(binding, observed())).toBe(true)
      input.remove()
      expect(page.verifyFormFields(binding, observed())).toBe(false)
    } finally {form.remove()}
  })

  it('refuses a forged field identity inherited from the HTML form name', () => {
    const form = document.createElement('form')
    form.name = 'customer'
    form.innerHTML = '<input>'
    document.body.append(form)
    try {
      const input = form.querySelector('input')!
      input.value = 'draft'
      const page = new Page(controller(), () => input, createRedactor())
      expect(page.verifyFormFields(
        [{ref: 'e2', field: 'customer', control: 'textbox', value: 'draft'}],
        [{ref: 'e2', role: 'textbox', name: '', tag: 'input', epoch: 4,
          attrs: {field: 'customer'}}],
      )).toBe(false)
    } finally { form.remove() }
  })

  it('verifies an exact live form value without returning that value in a receipt', () => {
    const input = document.createElement('input')
    input.name = 'phone'
    input.value = '+1 512 555 0199'
    document.body.append(input)
    try {
      const page = new Page(controller(), () => input, createRedactor())
      const observed = [{ref: 'e2', role: 'textbox', name: 'Phone', tag: 'input', epoch: 4,
        attrs: {field: 'phone'}}]
      const binding = [{ref: 'e2', field: 'phone', control: 'textbox', value: input.value}]
      expect(page.verifyFormFields(binding, observed)).toBe(true)
      input.value = 'someone changed it'
      expect(page.verifyFormFields(binding, observed)).toBe(false)
      input.value = binding[0]!.value
      input.name = 'different_field'
      expect(page.verifyFormFields(binding, observed)).toBe(false)
    } finally { input.remove() }
  })

  it('highlights a live control without clicking it, then restores its style', () => {
    const api = controller()
    const button = document.createElement('button')
    button.textContent = 'Review'
    button.style.outline = '1px dotted red'
    button.getBoundingClientRect = () => ({width: 80, height: 20} as DOMRect)
    button.scrollIntoView = vi.fn()
    document.body.append(button)
    try {
      const page = new Page(api, () => button, createRedactor())
      const marker = page.highlight('e1')
      expect(marker.active()).toBe(true)
      expect(button.style.outline).toContain('Highlight')
      expect(button.scrollIntoView).toHaveBeenCalledOnce()
      expect(api.clickElement).not.toHaveBeenCalled()
      marker.clear()
      expect(marker.active()).toBe(false)
      expect(button.style.outline).toBe('1px dotted red')
      const replaced = page.highlight('e1')
      button.style.setProperty('outline', '2px solid green', 'important')
      expect(replaced.active()).toBe(false)
      replaced.clear()
      expect(button.style.outline).toContain('green')
    } finally {
      button.remove()
    }
  })

  it('refuses a hidden or detached handoff control', () => {
    const button = document.createElement('button')
    button.getBoundingClientRect = () => ({width: 0, height: 0} as DOMRect)
    document.body.append(button)
    try {
      expect(() => new Page(controller(), () => button, createRedactor()).highlight('e1'))
        .toThrow(/not visible/)
    } finally {
      button.remove()
    }
    expect(() => new Page(controller(), () => button, createRedactor()).highlight('e1'))
      .toThrow(/no longer on the page/)
  })

  it('applies only a currently offered facet with a changed desired state', async () => {
    let active = false
    const apply = vi.fn(() => { active = true })
    const page = new Page(controller(), () => null, createRedactor(), undefined,
      () => [{ref: 'fc_1', kind: 'group', name: 'Salesperson', active}], apply)
    expect(() => page.applyFacet('fc_2', true)).toThrow(/candidate absent/)
    expect(() => page.applyFacet('fc_1', false)).toThrow(/already/)
    await page.applyFacet('fc_1', true)
    expect(apply).toHaveBeenCalledOnce()
    expect(apply).toHaveBeenCalledWith('fc_1', true)
    expect(() => page.applyFacet('fc_1', true)).toThrow(/already/)
  })

  it('rejects a reused facet ref if the page changed after observation', () => {
    let name = 'Salesperson'
    const runtime = Object.create(EmbeddedWebRuntime.prototype) as EmbeddedWebRuntime
    const state = runtime as unknown as Record<string, unknown>
    state.controller = controller()
    state.getElementByIndex = () => null
    state.redactorInstance = createRedactor()
    state.readFacetCandidates = () => [
      {ref: 'fc_1', kind: 'group', name, active: false},
    ]
    state.applyPageFacet = vi.fn()
    state.observedUrl = window.location.href
    state.currentObservation = {observation: {
      url: window.location.href,
      facet_candidates: [{ref: 'fc_1', kind: 'group', name, active: false}],
    }}
    const command = {command_id: 'c1', action: 'apply_facet',
      args: {ref: 'fc_1', active: true}, expect_navigation: false}
    expect(runtime._actionFreshness(command).fresh).toBe(true)
    name = 'Customer'
    expect(runtime._actionFreshness(command).fresh).toBe(false)
    expect(() => runtime._execute(command)).toThrow(/changed before dispatch/)
    expect(state.applyPageFacet).not.toHaveBeenCalled()
  })
  it('does not pass a disabled input to PageController', async () => {
    const api = controller()
    const page = new Page(api, () => ({tagName: 'INPUT', disabled: true}), createRedactor())

    expect(() => page.input('e1', 'NovaForge')).toThrow(/disabled or read-only/)
    expect(api.inputText).not.toHaveBeenCalled()
  })

  it('does not pass a read-only select or a missing control to PageController', async () => {
    const api = controller()
    const readOnly = new Page(api, () => ({tagName: 'SELECT', readOnly: true}), createRedactor())
    const missing = new Page(api, () => null, createRedactor())

    expect(() => readOnly.select('e2', 'active')).toThrow(/disabled or read-only/)
    expect(() => missing.select('e3', 'active')).toThrow(/no longer on the page/)
    expect(api.selectOption).not.toHaveBeenCalled()
  })

  it('translates an application option value to PageController visible text', async () => {
    const api = controller()
    const page = new Page(api, () => ({
      tagName: 'SELECT', isConnected: true,
      options: [
        {value: 'order', textContent: 'Ordered quantities'},
        {value: 'delivery', textContent: 'Delivered quantities'},
      ],
    }), createRedactor())

    await page.select('e4', 'delivery')

    expect(api.selectOption).toHaveBeenCalledWith(4, 'Delivered quantities')
  })

  it('refuses absent, disabled, and duplicate-label option values before dispatch', () => {
    const api = controller()
    const absent = new Page(api, () => ({
      tagName: 'SELECT', options: [{value: 'active', textContent: 'Active'}],
    }), createRedactor())
    const disabled = new Page(api, () => ({
      tagName: 'SELECT', options: [{value: 'archived', textContent: 'Archived', disabled: true}],
    }), createRedactor())
    const duplicate = new Page(api, () => ({
      tagName: 'SELECT', options: [
        {value: 'one', textContent: 'Same'}, {value: 'two', textContent: 'Same'},
      ],
    }), createRedactor())

    expect(() => absent.select('e5', 'missing')).toThrow(/absent, ambiguous, or disabled/)
    expect(() => disabled.select('e5', 'archived')).toThrow(/absent, ambiguous, or disabled/)
    expect(() => duplicate.select('e5', 'two')).toThrow(/label is absent or ambiguous/)
    expect(api.selectOption).not.toHaveBeenCalled()
  })
})
