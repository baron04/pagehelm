/** Minimal PageController-compatible DOM adapter for the synthetic demo application. */

function visible(element) {
  const style = getComputedStyle(element)
  const box = element.getBoundingClientRect()
  return style.display !== 'none' && style.visibility !== 'hidden' &&
    (box.width > 0 || box.height > 0)
}

function escaped(value) {
  return String(value ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;').replaceAll('>', '&gt;').replace(/\s+/g, ' ').trim()
}

function attributes(element) {
  const names = [
    'href', 'action', 'method', 'target', 'formaction', 'formmethod', 'formtarget',
    'type', 'name', 'id', 'role', 'aria-label', 'aria-selected', 'aria-checked',
    'aria-expanded', 'aria-haspopup', 'placeholder', 'autocomplete',
  ]
  const found = []
  for (const name of names) {
    if (element.hasAttribute(name)) found.push(`${name}="${escaped(element.getAttribute(name))}"`)
  }
  if (['BUTTON', 'INPUT'].includes(element.tagName) && element.value) {
    found.push(`value="${escaped(element.value)}"`)
  }
  return found.length ? ` ${found.join(' ')}` : ''
}

function label(element) {
  return escaped(element.innerText || element.getAttribute('aria-label') ||
    element.getAttribute('placeholder') || (element.tagName === 'BUTTON' ? element.value : ''))
}

export function getElementByIndex(selectorMap, index) {
  const element = selectorMap.get(Number(index))
  if (!element || !element.isConnected) throw new Error(`element ${index} is no longer available`)
  return element
}

// The synthetic REST app reports a rendered view via its own main-element marker.
// This deliberately shares no Odoo selector or route inference with the product core.
export function readActiveView() {
  return document.querySelector('main#app')?.getAttribute('data-active-view') || undefined
}

// The application puts a route on the main content it actually rendered. The URL
// or shared header does not create this fact; replacing that content removes it.
export function readPageRoute() {
  const pages = Array.from(document.querySelectorAll('main#app [data-page-route]'))
    .filter(element => visible(element) && !element.closest('[hidden],[aria-hidden="true"]'))
  return pages.length === 1 ? pages[0].getAttribute('data-page-route') || undefined : undefined
}

// The record the app has rendered, once it has: its own route while it shows a record.
export function readPageRecordRoute() {
  const view = document.querySelector('main#app')?.getAttribute('data-active-view')
  return view === 'Detail' ? location.pathname : undefined
}

export class PageController {
  constructor(_options = {}) {
    this.selectorMap = new Map()
  }

  async getBrowserState() {
    const selectors = 'a,button,input,textarea,select,form'
    const elements = Array.from(document.querySelectorAll(selectors)).filter(visible)
    this.selectorMap = new Map(elements.map((element, index) => [index, element]))
    const records = elements.map((element, index) => {
      const tag = element.tagName.toLowerCase()
      return `[${index}]<${tag}${attributes(element)}>${label(element)} />`
    })
    const pageText = escaped(document.querySelector('main')?.innerText || document.body.innerText)
    return {
      url: location.href,
      title: document.title,
      header: `WebArea "${escaped(document.title)}"`,
      content: [pageText, ...records].filter(Boolean).join('\n'),
      footer: '',
    }
  }

  clickElement(index) {
    getElementByIndex(this.selectorMap, index).click()
    return {success: true, message: 'element clicked'}
  }

  inputText(index, value) {
    const element = getElementByIndex(this.selectorMap, index)
    element.focus()
    element.value = value
    element.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText'}))
    element.dispatchEvent(new Event('change', {bubbles: true}))
    return {success: true, message: 'input updated'}
  }

  selectOption(index, value) {
    const element = getElementByIndex(this.selectorMap, index)
    element.value = value
    element.dispatchEvent(new Event('input', {bubbles: true}))
    element.dispatchEvent(new Event('change', {bubbles: true}))
    return {success: true, message: 'selection updated'}
  }

  scroll({down, numPages}) {
    window.scrollBy(0, (down ? 1 : -1) * window.innerHeight * numPages)
    return {success: true, message: 'page scroll observed'}
  }

  dispose() {
    this.selectorMap.clear()
  }
}
