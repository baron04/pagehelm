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

// A second, plain HTML integration supplies the same observation-only contract.
export function readElementNavigation(element) {
  if (!(element instanceof HTMLAnchorElement) || !element.isConnected ||
      !element.closest('nav') || element.closest('form,[role="dialog"]') ||
      element.hasAttribute('download') || element.target && element.target !== '_self') return undefined
  const path = element.getAttribute('href')
  return typeof path === 'string' && /^\/(?!\/)[^?#\\\s]*$/.test(path)
    ? {kind: 'page', path} : undefined
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

export function readPageRead() {
  const encoded = document.querySelector('#contact-filter')?.dataset.pageRead
  return encoded ? JSON.parse(encoded) : undefined
}

export function readElementPageControl(element) {
  if (location.pathname !== '/contacts/filter' || !element.closest('#contact-filter')) return undefined
  if (element.id === 'contact-filter-name') return {kind: 'query', field: 'name_contains'}
  if (element.tagName === 'BUTTON' && element.textContent === 'Search contacts') return {kind: 'query'}
  return undefined
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
    const selectors = 'a,button,input,textarea,select,form,[role="option"]'
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

// -- the application's own form surface -----------------------------------------
//: What this application vouches for: its one records read, its native form
//: fields, its own busy indicator. Core runtime carries none of this; without
//: these exports a form read has no vouched source and submits fail closed.
export function acceptReadSource(source, kind) {
  return (kind === 'form' || kind === 'row') && source === '/api/contacts'
}

export function readElementFormModel(element) {
  const form = element && (element.form || element.closest('form'))
  if (!form || !form.isConnected) return null
  const model = {}
  const items = []
  const withheld = []
  for (const control of form.elements) {
    if (!control.name) continue
    const label = control.labels && control.labels[0]
      ? control.labels[0].textContent.replace(/\s+/g, ' ').trim() : control.name
    if (control.type === 'password') { withheld.push(control.name); continue }
    if (control.type === 'submit' || control.type === 'button') continue
    const value = control.type === 'checkbox' ? control.checked : control.value
    model[control.name] = value
    items.push({prop: control.name, label, display: String(value)})
  }
  const recordId = form.dataset.recordId || ''
  const heading = (form.closest('section') || form.ownerDocument).querySelector('h1')
  return {
    model, items,
    mode: recordId ? 'update' : 'create',
    record_id: recordId,
    title: heading ? heading.textContent.replace(/\s+/g, ' ').trim() : '',
    source: '/api/contacts',
    withheld,
    invalid: [...form.elements]
      .filter(item => item.name && item.willValidate && !item.checkValidity())
      .map(item => item.name),
  }
}

export function restoreElementFormModel(element, request) {
  const form = element && (element.form || element.closest('form'))
  if (!form || !request || typeof request !== 'object') return Promise.resolve(false)
  const values = request.values && typeof request.values === 'object' && !Array.isArray(request.values)
    ? request.values : {}
  const keys = Object.keys(values).slice(0, 40)
  if (!keys.length) return Promise.resolve(form.checkValidity())
  for (const key of keys) {
    const control = form.elements.namedItem(key)
    if (!control || !control.name || control.type === 'password') return Promise.resolve(false)
    const value = String(values[key] ?? '')
    if (control.value !== value) {
      control.value = value
      control.dispatchEvent(new Event('input', {bubbles: true}))
      control.dispatchEvent(new Event('change', {bubbles: true}))
    }
  }
  // The write's truth is the model reread the runtime takes next; this answer
  // only vouches that the written value still stands in every named control.
  return Promise.resolve(keys.every(key => {
    const control = form.elements.namedItem(key)
    return Boolean(control) && control.value === String(values[key] ?? '')
  }))
}

export function readSubmitBusy(doc) {
  return Boolean((doc || document).querySelector('form[aria-busy="true"]'))
}

//: While the application's own read-only window is open, its writes carry the
//: marker the server armed -- the same convention the gateway documents.
if (typeof window !== 'undefined' && typeof window.fetch === 'function' && !window.__contactsFetchMarked) {
  window.__contactsFetchMarked = true
  let assistantWorking = false
  const realFetch = window.fetch.bind(window)
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || ''
    const marked = assistantWorking && url.startsWith('/api/') && init &&
      typeof init.method === 'string' && init.method.toUpperCase() !== 'GET'
    if (marked) {
      const headers = new Headers(init.headers || {})
      if (!headers.has('X-WOA-Assistant')) headers.set('X-WOA-Assistant', '1')
      init = {...init, headers}
    }
    const response = realFetch(input, init)
    if (url === '/api/page-window/begin') {
      response.then(reply => { if (reply.ok) assistantWorking = true }).catch(() => {})
    } else if (url === '/api/page-window/end') {
      assistantWorking = false
    }
    return response
  }
}
