import {describe, expect, it, vi} from 'vitest'
import {Page, pageControllerOptions, type BrowserState, type PageControllerApi}
  from '../src/runtime/page'
import {createRedactor} from '../src/runtime/redaction'

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
})

describe('Page.describe bounds an observation before it crosses the browser boundary', () => {
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
