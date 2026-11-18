/** Yudao/Element Plus form surface: the read sources the application vouches for and its
 * own confirmation and submit-busy indicators. Generic Element Plus helpers live in
 * `../element-plus/form.js`; Yudao API paths and style identifiers stay here. */
import {
  hasElBusyIndicator,
  installElOverlayOffset,
  pressElConfirmButton,
  readElConfirmBox,
} from '../element-plus/form.js'

const FORM_SOURCE = /^\/admin-api\/(?:[A-Za-z0-9_-]+\/)+(?:get|page|list)(?:-[a-z0-9-]+)?$/
const ROW_SOURCE = /^\/admin-api\/(?:[A-Za-z0-9_-]+\/)+(?:page|list)(?:-[a-z0-9-]+)?$/

/** Whether this application produced `source` from one of its own admin-api reads:
 * a form's single record or page list, a row only ever a list. */
export function acceptReadSource(source, kind) {
  return typeof source === 'string' &&
    (kind === 'row' ? ROW_SOURCE : FORM_SOURCE).test(source)
}

export {readElConfirmBox as readConfirmBox, pressElConfirmButton as pressConfirmButton}

let submitRequestsInFlight = 0

function installSubmitTracker(win) {
  if (!win || win.__woaYudaoSubmitTrackerInstalled) return
  win.__woaYudaoSubmitTrackerInstalled = true
  const isAdminWrite = (url, method) => {
    if (!method || String(method).toUpperCase() === 'GET') return false
    try {
      const parsed = new URL(String(url || ''), win.location?.href || 'http://localhost')
      return parsed.pathname.startsWith('/admin-api/')
    } catch {
      return String(url || '').startsWith('/admin-api/')
    }
  }
  if (typeof win.fetch === 'function') {
    const originalFetch = win.fetch.bind(win)
    win.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : (input && input.url) || ''
      const method = (init && init.method) || (input && input.method) || 'GET'
      const tracked = isAdminWrite(url, method)
      if (tracked) submitRequestsInFlight++
      try {
        const promise = originalFetch(input, init)
        return tracked ? promise.finally(() => {
          submitRequestsInFlight = Math.max(0, submitRequestsInFlight - 1)
        }) : promise
      } catch (err) {
        if (tracked) submitRequestsInFlight = Math.max(0, submitRequestsInFlight - 1)
        throw err
      }
    }
  }
  if (typeof win.XMLHttpRequest === 'function') {
    const proto = win.XMLHttpRequest.prototype
    const open = proto.open, send = proto.send
    proto.open = function(method, url, ...rest) {
      this.__woaAdminWrite = isAdminWrite(url, method)
      return open.call(this, method, url, ...rest)
    }
    proto.send = function(...args) {
      const done = () => {
        if (!this.__woaAdminWriteCounted) return
        this.__woaAdminWriteCounted = false
        submitRequestsInFlight = Math.max(0, submitRequestsInFlight - 1)
      }
      if (this.__woaAdminWrite && !this.__woaAdminWriteCounted) {
        this.__woaAdminWriteCounted = true
        submitRequestsInFlight++
        this.addEventListener('loadend', done, {once: true})
      }
      try {
        return send.apply(this, args)
      } catch (err) {
        done()
        throw err
      }
    }
  }
}

if (typeof window !== 'undefined') installSubmitTracker(window)

/** Element Plus's own submit-in-flight indicators and active admin-api write requests. */
export function readSubmitBusy(doc = document) {
  installSubmitTracker(doc?.defaultView)
  return submitRequestsInFlight > 0 || hasElBusyIndicator(doc)
}

/** Keep Element Plus fixed overlays/drawers clear of the side-by-side assistant drawer. */
export function installOverlayOffset(doc = document) {
  return installElOverlayOffset(doc, {styleId: 'woa-yudao-overlay-offset'})
}
