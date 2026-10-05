/** App-owned page facts. Load before the application's module entry.
 * Observes same-origin application GET completion; retains no credentials,
 * bodies or business values except a bounded visible detail title and reviewed
 * scalar applied-query parameters.
 * A navigation fact additionally requires the deployed AppView's live Vue mount.
 * This is observation, never the application's API write fence.
 */
function ledgerQuery(url) {
  // Application-owned projection, not a schema or permission in the core.
  const patterns = {accountSetId: /^[1-9][0-9]{0,19}$/, startMonth: /^\d{4}-(0[1-9]|1[0-2])$/,
    endMonth: /^\d{4}-(0[1-9]|1[0-2])$/, minLevel: /^[1-9][0-9]?$/, maxLevel: /^[1-9][0-9]?$/}
  const pairs = Array.from(url.searchParams)
  if (pairs.length === Object.keys(patterns).length && new Set(pairs.map(([key]) => key)).size === pairs.length &&
      pairs.every(([key, value]) => Object.hasOwn(patterns, key) && patterns[key].test(value))) {
    return {path: url.pathname, parameters: Object.fromEntries(pairs)}
  }
  return null
}

function orderQuery(url) {
  const patterns = {customerId: /^[1-9][0-9]{0,19}$/, status: /^(0|10|20)$/,
    orderTimeStart: /^\d{4}-\d{2}-\d{2} 00:00:00$/, orderTimeEnd: /^\d{4}-\d{2}-\d{2} 23:59:59$/}
  const keys = {'orderTime[0]': 'orderTimeStart', 'orderTime[1]': 'orderTimeEnd'}
  const parameters = {}, pairs = Array.from(url.searchParams)
  if (new Set(pairs.map(([key]) => key)).size !== pairs.length) return null
  for (const [raw, value] of pairs) {
    if (['pageNo', 'pageSize'].includes(raw)) continue
    const key = keys[raw] || raw
    if (!Object.hasOwn(patterns, key) || Object.hasOwn(parameters, key) || !patterns[key].test(value)) return null
    parameters[key] = value
  }
  return {path: url.pathname, parameters}
}

export function createPageFacts({view = window, doc = document} = {}) {
  let route = '', locationKey = '', generation = 0, record = null, appliedQuery = null, disposed = false
  const reads = new Map()
  const visible = element => Boolean(element && element.getBoundingClientRect().width
    && element.getBoundingClientRect().height && !element.closest('[hidden],[aria-hidden="true"]'))
  function current() {
    const next = view.location.pathname + view.location.search + view.location.hash
    if (next !== locationKey) {
      locationKey = next; route = view.location.pathname; generation++; record = null; appliedQuery = null; reads.clear()
    }
    return generation
  }
  function begin(input, method = 'GET') {
    if (disposed || String(method).toUpperCase() !== 'GET') return null
    let url
    try { url = new URL(input, view.location.href) } catch { return null }
    if (url.origin !== view.location.origin || !url.pathname.startsWith('/admin-api/')) return null
    const heldGeneration = current()
    const ledger = route === '/fms/ledger/general' && url.pathname === '/admin-api/fms/ledger/general/list'
    const order = route === '/erp/sale/order' && url.pathname === '/admin-api/erp/sale-order/page'
    let query = null
    if (ledger || order) {
      appliedQuery = null
      // Application-owned scalar query observation for the first native report.
      // Retain only reviewed query fields, never headers, bodies or arbitrary URLs.
      query = ledger ? ledgerQuery(url) : orderQuery(url)
    }
    const detail = /^\/crm\/(customer|contract|business)\/detail\/([0-9]+)$/.exec(route)
    const isRecord = Boolean(detail && url.pathname === `/admin-api/crm/${detail[1]}/get`)
    const ids = url.searchParams.getAll('id')
    const matchesRecord = !isRecord || ids.length === 1 && ids[0] === detail[2]
    const selected = Array.from(doc.querySelectorAll('[role="tab"][aria-selected="true"]'))
      .filter(visible)
    const scoped = Boolean(detail && url.pathname.startsWith('/admin-api/crm/'))
    const panel = scoped && selected.length === 1 ? selected[0].getAttribute('aria-controls') || '' : ''
    const readPanel = isRecord || !scoped ? '*' : panel
    const key = readPanel + '\n' + url.pathname
    // A bounded projection fails closed rather than dropping unfinished/error reads.
    if (!reads.has(key) && reads.size >= 64) { reads.set('overflow', {pending: 0, failed: true, panel: '*'}); return null }
    const prior = reads.get(key)
    const state = {pending: (prior?.pending || 0) + 1, failed: prior?.failed || false,
      conflicts: (prior?.conflicts || 0) + Number(!matchesRecord),
      panel: readPanel, serial: (prior?.serial || 0) + 1}
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
      if ((ledger && ok && query && Array.isArray(body?.data)) ||
          (order && ok && query && Array.isArray(body?.data?.list))) appliedQuery = query
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
    if (disposed) return true
    const selected = Array.from(doc.querySelectorAll('[role="tab"][aria-selected="true"]')).filter(visible)
    const panel = selected[0]?.getAttribute('aria-controls') || ''
    const relevant = Array.from(reads.values()).filter(s => s.panel === '*' || !s.panel || s.panel === panel)
    if (relevant.some(s => s.failed || s.conflicts)) return true
    if (Array.from(doc.querySelectorAll('.el-message--error, .el-notification--error')).some(visible)) return true
    if (relevant.some(s => s.pending)) return 'pending'
    const loading = Array.from(doc.querySelectorAll('.el-loading-mask,[aria-busy="true"]')).some(visible)
    if (!/^\/crm\/(customer|contract|business)\/detail\/[0-9]+$/.test(route)) return loading ? 'pending' : false
    const titles = Array.from(doc.querySelectorAll('.text-xl.font-bold')).filter(visible)
    if (titles.length > 1 || record && titles.length === 1 && titles[0].textContent?.trim()
        && titles[0].textContent.trim() !== record.name) return true
    if (selected.length > 1) return true
    // A route can render before its first GET or loading mask exists. Unknown
    // identity remains pending (bounded by the executor), never ready. Explicit
    // failed/conflicting reads and visible errors above still fail immediately.
    if (loading || !record) return 'pending'
    if (selected.length !== 1 || titles.length !== 1 || titles[0].textContent?.trim() !== record.name) return true
    return false
  }
  function readPageRecordRoute() {
    current()
    if (readPageBlocker() || record?.route !== route) return undefined
    return route
  }
  function readPageQuery() {
    current()
    if (!appliedQuery || readPageBlocker() || readPageRoute() !== route) return undefined
    return {path: appliedQuery.path, parameters: {...appliedQuery.parameters}}
  }
  function readPageRoute() {
    if (disposed) return undefined
    current()
    try {
      const app = doc.querySelector('#app')?.__vue_app__
      const active = app?.config?.globalProperties?.$router?.currentRoute?.value
      const matched = active?.matched?.at(-1)
      // Simple parameter routes may identify the active record. A wildcard,
      // redirect or unsupported route expression cannot prove a destination.
      // The live proxy and its fullPath key below bind the concrete instance.
      if (!matched || active.path !== route || matched.redirect || typeof matched.path !== 'string') return undefined
      const segments = matched.path.split('/')
      if (segments.some(segment => /[:()*?]/.test(segment) && !/^:[A-Za-z_][A-Za-z0-9_]*$/.test(segment))) return undefined
      const pattern = segments.map(segment => segment.startsWith(':') ? '[^/]+'
        : segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('/')
      if (!new RegExp('^' + pattern + '$').test(route)) return undefined
      const instance = matched.instances?.default?.$
      const component = matched.components?.default
      const mounted = item => item?.isMounted === true && item.isUnmounted === false && item.isDeactivated === false
      const name = item => item?.type?.name || item?.type?.__name || ''
      const errorComponent = item => /^(Error(?:403|404|500)?|NoFound|NoAccess)$/.test(name(item))
      if (!mounted(instance) || !component || instance.type !== component
          || instance.vnode?.type !== component || instance.vnode?.component !== instance
          || instance.appContext?.app !== app || errorComponent(instance)
          // The reviewed AppView keys its destination by fullPath unless a
          // custom viewKey overrides it. An unrelated key cannot prove arrival.
          || instance.vnode.key !== active.fullPath) return undefined
      let routerView, appView, ancestor = instance.parent
      for (let depth = 0; ancestor && depth < 24; depth++, ancestor = ancestor.parent) {
        if (!mounted(ancestor) || errorComponent(ancestor)) return undefined
        if (!routerView && name(ancestor) === 'RouterView') routerView = ancestor
        if (routerView && name(ancestor) === 'AppView') { appView = ancestor; break }
      }
      if (!routerView || !appView) return undefined
      // Search only the active VNode branch, never KeepAlive's cached instances.
      // Unknown/oversized trees fail closed, without retaining component state.
      const stack = [routerView.subTree], seen = new Set()
      let found = false
      while (stack.length && seen.size < 64) {
        const node = stack.pop()
        if (!node || seen.has(node)) continue
        seen.add(node)
        if (node.component === instance) { found = true; break }
        if (node.suspense?.pendingBranch) return undefined
        if (node.component) stack.push(node.component.subTree)
        if (Array.isArray(node.children)) {
          if (node.children.length + stack.length > 64) return undefined
          stack.push(...node.children)
        }
      }
      if (!found) return undefined
      const roots = appView.subTree?.type === 'section' ? [appView.subTree]
        : Array.isArray(appView.subTree?.children) ? appView.subTree.children : []
      const sections = roots.filter(node => node?.type === 'section' && node.el?.isConnected && visible(node.el))
      if (sections.length !== 1) return undefined
      const main = sections[0].el
      const render = [instance.subTree], rendered = new Set()
      let content = false
      // Bound element wrappers and child components together; truncation must
      // not drop a blocking error and still grant arrival.
      while (render.length && rendered.size < 4096) {
        const node = render.pop()
        if (!node || rendered.has(node)) continue
        rendered.add(node)
        if (node.suspense?.pendingBranch || node.component &&
            (!mounted(node.component) || errorComponent(node.component))) return undefined
        if (node.el?.nodeType === 1 && node.el.isConnected && main.contains(node.el) && visible(node.el)) content = true
        if (node.component) render.push(node.component.subTree)
        if (Array.isArray(node.children)) {
          if (node.children.length + render.length > 4096) return undefined
          render.push(...node.children)
        }
      }
      if (render.length || !content || Array.from(doc.querySelectorAll(
        '.el-loading-mask,.el-message--error,.el-notification--error,[aria-busy="true"]')).some(visible)
          || readPageBlocker()) return undefined
      return route
    } catch { return undefined }
  }
  return {begin, readPageBlocker, readPageRecordRoute, readPageRoute, readPageQuery,
    dispose() {disposed = true; reads.clear(); record = null; appliedQuery = null}}
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
      if (target.origin === view.location.origin && target.pathname.startsWith('/admin-api/')) {
        const ids = target.searchParams.getAll('id'), id = ids.length === 1 ? ids[0] : ''
        const ledger = view.location.pathname === '/fms/ledger/general' && target.pathname === '/admin-api/fms/ledger/general/list'
        const order = view.location.pathname === '/erp/sale/order' && target.pathname === '/admin-api/erp/sale-order/page'
        // Retain only a bounded scalar projection; open alone starts no read.
        const query = ledger ? ledgerQuery(target) : order ? orderQuery(target) : null
        requests.set(this, {method, path: target.pathname, ledger, order, query,
          id: /^[0-9]{1,20}$/.test(id) ? id : ''})
      }
    } catch { /* The original implementation owns invalid URL errors. */ }
    return originalOpen.call(this, method, url, ...rest)
  }
  const send = function(...args) {
    const request = requests.get(this)
    requests.delete(this)
    let target
    if (request) {
      target = new URL(request.path, view.location.href)
      if (request.ledger || request.order) {
        const keys = {orderTimeStart: 'orderTime[0]', orderTimeEnd: 'orderTime[1]'}
        if (!request.query) target.searchParams.set('invalidQuery', '1')
        for (const [key, value] of Object.entries(request.query?.parameters || {})) {
          target.searchParams.set(request.order && keys[key] || key, value)
        }
      } else if (request.id) target.searchParams.set('id', request.id)
    }
    const settle = request && facts.begin(target.href, request.method)
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
export const readPageRoute = () => installed?.readPageRoute()
export const readPageQuery = () => installed?.readPageQuery()
export const disposeInstalledPageFacts = () => installed?.dispose()
/** Observe only the mounted application's default menu, not arbitrary menu-shaped DOM. */
export function readElementNavigation(element) {
  if (!(element instanceof HTMLElement) || !element.isConnected || element.closest('form,[role="dialog"]')) return undefined
  const root = element.closest('li.el-menu-item,li.el-sub-menu')
  if (!root || root.classList.contains('is-disabled')) return undefined
  const live = item => item?.isMounted === true && item.isUnmounted === false && item.isDeactivated === false
  // Production Vue omits DOM component backreferences. Walk the mounted VNodes
  // instead; component identity is not inferred from CSS or a stale cache.
  const app = document.getElementById('app')?.__vue_app__
  const stack = [app?._container?._vnode], nodes = new Set()
  let instance
  while (stack.length && nodes.size < 8192) {
    const node = stack.pop()
    if (!node || nodes.has(node)) continue
    nodes.add(node)
    if (node.el instanceof HTMLElement && node.el !== root && !node.el.contains(root)) continue
    if (node.component) {
      const candidate = node.component
      if (!live(candidate)) continue
      if (['ElMenuItem', 'ElSubMenu'].includes(candidate.type?.name) && candidate.subTree?.el === root) {
        if (instance && instance !== candidate) return undefined
        instance = candidate
      }
      stack.push(candidate.subTree)
    }
    if (Array.isArray(node.children)) stack.push(...node.children)
  }
  if (stack.length) return undefined
  const seen = new Set()
  if (!live(instance) || instance.subTree?.el !== root) return undefined
  const kind = instance.type.name === 'ElSubMenu' ? 'branch' : 'page'
  const title = kind === 'branch' ? root.querySelector(':scope > .el-sub-menu__title') : root
  if (!title || (element !== root && element !== title)) return undefined
  if (root.getAttribute('role') !== 'menuitem') return undefined
  const path = instance.props?.index
  if (typeof path !== 'string' || path.length > 500 || !/^\/(?!\/)[^?#\\\s]*$/.test(path)) return undefined
  let owner = instance.parent
  while (owner && seen.size < 48) {
    if (!live(owner) || owner.appContext !== instance.appContext || seen.has(owner)) return undefined
    seen.add(owner)
    if (owner.type?.name === 'Menu') {
      if (owner.props?.menuSelect || owner.props?.rootOnly || owner.props?.menus !== undefined ||
          !owner.subTree?.el?.contains(root)) return undefined
      return {kind, path, ...(kind === 'branch' ? {expanded: root.getAttribute('aria-expanded') === 'true'} : {})}
    }
    owner = owner.parent
  }
  return undefined
}
/** Map one Element Plus select hit area to its own accessible form input. */
export function nativeControlElement(element) {
  if (readElementNavigation(element)?.kind === 'branch' && element.matches('li.el-sub-menu')) {
    return element.querySelector(':scope > .el-sub-menu__title')
  }
  if (element instanceof HTMLElement && element.matches('.el-radio,.el-radio-button')) {
    const radios = element.querySelectorAll('input[type="radio"]')
    return radios.length === 1 && radios[0].closest('.el-radio,.el-radio-button') === element
      ? radios[0] : element
  }
  if (!(element instanceof HTMLElement) || !element.matches('.el-select__wrapper') ||
      element.classList.contains('is-disabled') || element.getAttribute('aria-disabled') === 'true') return element
  const inputs = element.querySelectorAll('input[role="combobox"][aria-haspopup="listbox"][aria-controls]')
  return inputs.length === 1 && inputs[0].closest('.el-select__wrapper') === element
    ? inputs[0] : element
}

/** Locate an exact enabled native calendar day without touching Vue state. */
export function nativeRangeDateCell(element, value) {
  if (!(element instanceof HTMLInputElement) || element.disabled || element.readOnly ||
      !element.matches('.el-date-editor--daterange > .el-range-input') ||
      !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const id = element.getAttribute('aria-controls')
  if (!id || !/^[A-Za-z][A-Za-z0-9_:.-]{0,199}$/.test(id) || element.getAttribute('aria-haspopup') !== 'dialog') return null
  const popups = element.ownerDocument.querySelectorAll(`[id="${id}"]`)
  if (popups.length !== 1 || popups[0].getAttribute('role') !== 'dialog') return null
  const [year, month, day] = value.split('-').map(Number)
  if (new Date(Date.UTC(year, month - 1, day)).toISOString().slice(0,10) !== value) return null
  const matches = [...popups[0].querySelectorAll('.el-date-range-picker__content')].flatMap(panel => {
    const labels = [...panel.querySelectorAll('.el-date-range-picker__header [role="button"]')]
      .map(e => (e.textContent || '').replace(/\s/g, ''))
    if (!labels.includes(`${year}年`) || !labels.includes(`${month}月`)) return []
    return [...panel.querySelectorAll('table[role="grid"] td.available')].filter(cell =>
      cell.getAttribute('aria-disabled') !== 'true' && !cell.classList.contains('disabled') &&
      cell.querySelector('.el-date-table-cell__text')?.textContent?.trim() === String(day))
  })
  return matches.length === 1 ? matches[0] : null
}
