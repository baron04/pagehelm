import {describe, expect, it, vi} from 'vitest'
import {Page, pageControllerOptions, type BrowserState, type PageControllerApi}
  from '../src/runtime/page'
import {createRedactor} from '../src/runtime/redaction'
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
  it('observes only the current viewport', () => {
    expect(pageControllerOptions([]).viewportExpansion).toBe(0)
  })

  it('includes generic accessible control state in the bounded observation', () => {
    expect(pageControllerOptions([]).includeAttributes).toEqual(
      expect.arrayContaining(['aria-selected', 'aria-checked', 'aria-expanded']))
  })
})

describe('Page.describe bounds an observation before it crosses the browser boundary', () => {
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
