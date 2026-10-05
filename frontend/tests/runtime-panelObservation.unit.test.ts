import {describe, expect, it} from 'vitest'
import {Page, type PageControllerApi} from '../src/runtime/page'
import {createRedactor} from '../src/runtime/redaction'

describe('assistant self-occlusion during synchronous page extraction', () => {
  async function read(fail = false, yieldBeforeSample = false) {
    const panel = document.createElement('div'), appDialog = document.createElement('dialog')
    const shadow = panel.attachShadow({mode: 'open'})
    const button = document.createElement('button')
    button.style.setProperty('pointer-events', 'auto', 'important')
    shadow.append(button)
    document.body.append(panel, appDialog)
    const events = new EventTarget()
    let sample = '', application = ''
    const controller = {
      selectorMap: {}, addEventListener: events.addEventListener.bind(events),
      removeEventListener: events.removeEventListener.bind(events),
      async getBrowserState() {
        events.dispatchEvent(new Event('beforeUpdate'))
        if (yieldBeforeSample) await Promise.resolve()
        sample = button.style.getPropertyValue('pointer-events')
        application = appDialog.style.getPropertyValue('pointer-events')
        if (fail) throw new Error('extractor failed')
        events.dispatchEvent(new Event('afterUpdate'))
        expect(button.style.getPropertyValue('pointer-events')).toBe('auto')
        return {url: location.href, title: '', content: ''}
      },
    } as unknown as PageControllerApi
    const page = new Page(controller, () => null, createRedactor(), undefined, undefined,
      undefined, undefined, undefined, undefined, undefined, undefined, undefined,
      undefined, undefined, undefined, panel)
    try {
      if (fail) await expect(page.read()).rejects.toThrow('extractor failed')
      else await page.read()
      expect(sample).toBe(yieldBeforeSample ? 'auto' : 'none')
      expect(application).toBe('')
      expect(button.style.getPropertyValue('pointer-events')).toBe('auto')
      expect(button.style.getPropertyPriority('pointer-events')).toBe('important')
      expect(panel.style.getPropertyValue('pointer-events')).toBe('')
      // A stale event after the sample cannot disable the restored panel.
      events.dispatchEvent(new Event('beforeUpdate'))
      expect(button.style.getPropertyValue('pointer-events')).toBe('auto')
    } finally { panel.remove(); appDialog.remove() }
  }
  it('excludes only the owned panel and restores exact styles before returning', async () => { await read() })
  it('restores panel interaction on extraction errors', async () => { await read(true) })
  it('ends the exclusion before an asynchronous controller continues', async () => { await read(false, true) })
})
