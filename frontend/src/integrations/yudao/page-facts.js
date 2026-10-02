/** App-owned page facts. Load before the application's module entry.
 * Observes only same-origin CRM GET completion; retains no credentials, queries,
 * bodies or business values except the current detail's bounded visible title.
 * This is observation, never the application's API write fence.
 */
export function createPageFacts({view = window, doc = document} = {}) {
  let route = '', generation = 0, record = null, disposed = false
  const reads = new Map()
  const visible = element => Boolean(element && element.getBoundingClientRect().width
    && element.getBoundingClientRect().height && !element.closest('[hidden],[aria-hidden="true"]'))
  function current() {
    const next = view.location.pathname
    if (next !== route) { route = next; generation++; record = null; reads.clear() }
    return generation
  }
  function begin(input, method = 'GET') {
    if (disposed || String(method).toUpperCase() !== 'GET') return null
    let url
    try { url = new URL(input, view.location.href) } catch { return null }
    if (url.origin !== view.location.origin || !url.pathname.startsWith('/admin-api/crm/')) return null
    const heldGeneration = current()
    const detail = /^\/crm\/(customer|contract|business)\/detail\/([0-9]+)$/.exec(route)
    if (!detail) return null
    const isRecord = url.pathname === `/admin-api/crm/${detail[1]}/get`
    const ids = url.searchParams.getAll('id')
    const matchesRecord = !isRecord || ids.length === 1 && ids[0] === detail[2]
    const selected = Array.from(doc.querySelectorAll('[role="tab"][aria-selected="true"]'))
      .filter(visible)
    const panel = selected.length === 1 ? selected[0].getAttribute('aria-controls') || '' : ''
    const key = (isRecord ? '*' : panel) + '\n' + url.pathname
    // A bounded projection fails closed rather than dropping unfinished/error reads.
    if (!reads.has(key) && reads.size >= 64) { reads.set('overflow', {pending: 0, failed: true, panel: '*'}); return null }
    const prior = reads.get(key)
    const state = {pending: (prior?.pending || 0) + 1, failed: prior?.failed || false,
      conflicts: (prior?.conflicts || 0) + Number(!matchesRecord),
      panel: isRecord ? '*' : panel, serial: (prior?.serial || 0) + 1}
    reads.set(key, state)
    const serial = state.serial
    if (isRecord) record = null
    let settled = false
    return (status, body) => {
      if (settled || disposed) return
      settled = true
      if (current() !== heldGeneration) return
      const latest = reads.get(key)
      if (!latest) return
      latest.pending = Math.max(0, latest.pending - 1)
      latest.conflicts = Math.max(0, (latest.conflicts || 0) - Number(!matchesRecord))
      // Concurrent older completions cannot overwrite the newest request's verdict.
      if (latest.serial !== serial) return
      const ok = matchesRecord && status >= 200 && status < 300 && body?.code === 0
      latest.failed = !ok
      if (isRecord && ok) {
        const id = body?.data?.id, name = body?.data?.name
        if ((typeof id === 'number' && Number.isSafeInteger(id) || typeof id === 'string' && /^[0-9]+$/.test(id))
            && String(id) === detail[2] && typeof name === 'string' && name.trim()
            && name.length <= 200) record = {route, name: name.trim()}
        else latest.failed = true
      }
    }
  }
  function readPageBlocker() {
    current()
    if (!/^\/crm\/(customer|contract|business)\/detail\/[0-9]+$/.test(route)) return false
    const titles = Array.from(doc.querySelectorAll('.text-xl.font-bold')).filter(visible)
    if (titles.length !== 1 || record && titles[0].textContent?.trim() !== record.name) return true
    if (Array.from(doc.querySelectorAll('.el-message--error, .el-notification--error')).some(visible)) return true
    const selected = Array.from(doc.querySelectorAll('[role="tab"][aria-selected="true"]')).filter(visible)
    if (selected.length !== 1) return true
    const panel = selected[0].getAttribute('aria-controls') || ''
    const relevant = Array.from(reads.values()).filter(s => s.panel === '*' || !s.panel || s.panel === panel)
    if (relevant.some(s => s.failed || s.conflicts)) return true
    if (relevant.some(s => s.pending)) return 'pending'
    if (!record) return true
    if (Array.from(doc.querySelectorAll('.el-loading-mask')).some(visible)) return 'pending'
    return false
  }
  function readPageRecordRoute() {
    current()
    if (readPageBlocker() || record?.route !== route) return undefined
    return route
  }
  return {begin, readPageBlocker, readPageRecordRoute, dispose() {disposed = true; reads.clear(); record = null}}
}

export function installPageFacts(view = window, doc = document) {
  const facts = createPageFacts({view, doc})
  const originalFetch = view.fetch
  const originalOpen = view.XMLHttpRequest.prototype.open
  const originalSend = view.XMLHttpRequest.prototype.send
  const requests = new WeakMap()
  const fetch = async function(input, init) {
    const settle = facts.begin(typeof input === 'string' || input instanceof URL ? String(input) : input.url,
      init?.method || input?.method || 'GET')
    try {
      const response = await originalFetch.call(this, input, init)
      if (settle) {
        let body
        try { body = await response.clone().json() } catch { /* invalid envelopes fail closed */ }
        settle(response.status, body)
      }
      return response
    } catch (error) { settle?.(0, null); throw error }
  }
  const open = function(method, url, ...rest) {
    requests.delete(this)
    try {
      const target = new URL(String(url), view.location.href)
      if (target.origin === view.location.origin && target.pathname.startsWith('/admin-api/crm/')) {
        const ids = target.searchParams.getAll('id')
        const id = ids.length === 1 ? ids[0] : ''
        requests.set(this, {method, path: target.pathname,
          id: /^[0-9]{1,20}$/.test(id) ? id : ''})
      }
    } catch { /* The original implementation owns invalid URL errors. */ }
    return originalOpen.call(this, method, url, ...rest)
  }
  const send = function(...args) {
    const request = requests.get(this)
    requests.delete(this)
    const settle = request && facts.begin(request.path + (request.id ? '?id=' + request.id : ''), request.method)
    if (settle) this.addEventListener('loadend', () => {
      let body
      try { body = this.responseType === 'json' ? this.response : JSON.parse(this.responseText) } catch { /* blocked */ }
      settle(this.status, body)
    }, {once: true})
    try { return originalSend.apply(this, args) } catch (error) {settle?.(0, null); throw error}
  }
  view.fetch = fetch; view.XMLHttpRequest.prototype.open = open; view.XMLHttpRequest.prototype.send = send
  return {...facts, dispose() {
    facts.dispose()
    if (view.fetch === fetch) view.fetch = originalFetch
    if (view.XMLHttpRequest.prototype.open === open) view.XMLHttpRequest.prototype.open = originalOpen
    if (view.XMLHttpRequest.prototype.send === send) view.XMLHttpRequest.prototype.send = originalSend
  }}
}

// The explicit early module and the controller share the same browser module instance.
const installed = typeof window === 'undefined' ? null : installPageFacts()
export const readPageBlocker = () => installed ? installed.readPageBlocker() : true
export const readPageRecordRoute = () => installed?.readPageRecordRoute()
export const disposeInstalledPageFacts = () => installed?.dispose()
