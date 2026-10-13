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

// Retain a digest and paging numbers, never arbitrary query values.
function listReadQuery(url, view) {
  const pairs = Array.from(url.searchParams)
  const page = url.searchParams.get('pageNo'), size = url.searchParams.get('pageSize')
  if (!url.pathname.endsWith('/page') || pairs.length > 32 ||
      !/^[1-9][0-9]*$/.test(page || '') || !/^[1-9][0-9]*$/.test(size || '') ||
      !Number.isSafeInteger(Number(page)) || !Number.isSafeInteger(Number(size)) ||
      url.searchParams.getAll('pageNo').length !== 1 || url.searchParams.getAll('pageSize').length !== 1 ||
      !view.crypto?.subtle) return null
  const filters = pairs.filter(([key]) => !['pageNo', 'pageSize'].includes(key))
  filters.sort(([a, av], [b, bv]) => a < b ? -1 : a > b ? 1 : av < bv ? -1 : av > bv ? 1 : 0)
  const canonical = JSON.stringify(filters)
  if (canonical.length > 8192) return null
  const hash = text => view.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
    .then(bytes => Array.from(new Uint8Array(bytes), n => n.toString(16).padStart(2, '0')).join(''))
  const fields = new Map()
  for (const [key, value] of filters) fields.set(key, [...(fields.get(key) || []), value])
  const digest = Promise.all([hash(canonical), Promise.all([...fields].map(async ([key, values]) =>
    [key, await hash(JSON.stringify(values))]))]).then(([query_digest, entries]) =>
    ({query_digest, query_fields: Object.fromEntries(entries)}))
  return {page: Number(page), size: Number(size), digest: digest.catch(() => null)}
}

/** Vue may leave the route record's instance table empty after a query replacement.
 * Recover only the one exact keyed component in the application's active VNode tree;
 * never inspect KeepAlive caches or infer arrival from the URL/component name.
 */
function matchedPageInstance(app, active, matched) {
  const registered = matched?.instances?.default?.$
  if (registered) return registered
  const component = matched?.components?.default
  const root = app?._container?._vnode
  if (!component || !root) return undefined
  const live = item => item?.isMounted === true && item.isUnmounted === false && item.isDeactivated === false
  // Bind the nearest rendered AppView under the current route's mounted parent.
  // Do not exhaustively scan menus or descend into the page's thousands of VNodes.
  const parent = active.matched.slice(0, -1).reverse().map(item => item.instances?.default?.$)
    .find(item => live(item) && item.appContext?.app === app)
  const ancestors = [parent ? parent.subTree : root], visited = new Set()
  let appView
  while (ancestors.length && visited.size < 256) {
    const node = ancestors.shift()
    if (!node || visited.has(node)) continue
    visited.add(node)
    if (node.suspense?.pendingBranch) return undefined
    if (node.component) {
      const item = node.component
      if (!live(item)) continue
      if (item.appContext?.app !== app) return undefined
      if ((item.type?.name || item.type?.__name) === 'AppView') { appView = item; break }
      ancestors.push(item.subTree)
    }
    if (Array.isArray(node.children)) {
      if (node.children.length + ancestors.length > 256) return undefined
      ancestors.push(...node.children)
    }
  }
  if (!appView) return undefined
  const stack = [appView.subTree], seen = new Set(), candidates = new Set()
  while (stack.length && seen.size < 64) {
    const node = stack.pop()
    if (!node || seen.has(node)) continue
    seen.add(node)
    if (node.suspense?.pendingBranch) return undefined
    if (node.component) {
      const item = node.component
      if (!live(item)) continue
      if (item.appContext?.app !== app) return undefined
      if (item.type === component && node.type === component && node.key === active.fullPath
          && item.vnode === node && node.component === item) {
        candidates.add(item)
        continue
      }
      stack.push(item.subTree)
    }
    if (Array.isArray(node.children)) {
      if (node.children.length + stack.length > 64) return undefined
      stack.push(...node.children)
    }
  }
  return !stack.length && candidates.size === 1 ? candidates.values().next().value : undefined
}

export function createPageFacts({view = window, doc = document} = {}) {
  let route = '', locationKey = '', generation = 0, record = null, appliedQuery = null, appliedRead = null, disposed = false
  const reads = new Map()
  const visible = element => Boolean(element && element.getBoundingClientRect().width
    && element.getBoundingClientRect().height && !element.closest('[hidden],[aria-hidden="true"]'))
  function current() {
    const next = view.location.pathname + view.location.search + view.location.hash
    if (next !== locationKey) {
      locationKey = next; route = view.location.pathname; generation++; record = null; appliedQuery = null; appliedRead = null; reads.clear()
    }
    return generation
  }
  function begin(input, method = 'GET', readQuery) {
    if (disposed || String(method).toUpperCase() !== 'GET') return null
    let url
    try { url = new URL(input, view.location.href) } catch { return null }
    if (url.origin !== view.location.origin || !url.pathname.startsWith('/admin-api/')) return null
    const heldGeneration = current()
    readQuery = readQuery === undefined ? listReadQuery(url, view) : readQuery
    if (url.pathname.endsWith('/page')) appliedRead = null
    const ledger = route === '/fms/ledger/general' && url.pathname === '/admin-api/fms/ledger/general/list'
    const order = route === '/erp/sale/order' && url.pathname === '/admin-api/erp/sale-order/page'
    let query = null
    if (ledger || order) {
      appliedQuery = null
      // Application-owned scalar query observation for the first native report.
      // Retain only reviewed query fields, never headers, bodies or arbitrary URLs.
      query = ledger ? ledgerQuery(url) : orderQuery(url)
    } else if (url.pathname.endsWith('/page') && route && route.indexOf('/detail/') < 0) {
      // Any other collection page's own list read: the tracked GET is the page's
      // applied query. Scalar projection stays ledger/order-only; this general
      // capture feeds applied-query facets for target settlement on every list.
      appliedQuery = null
      query = {path: url.pathname, parameters: Object.fromEntries(url.searchParams)}
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
          (order && ok && query && Array.isArray(body?.data?.list)) ||
          (query && ok && url.pathname.endsWith('/page') && Array.isArray(body?.data?.list))) {
        appliedQuery = query
      }
      if (isRecord && ok) {
        const id = body?.data?.id, name = body?.data?.name
        if ((typeof id === 'number' && Number.isSafeInteger(id) || typeof id === 'string' && /^[0-9]+$/.test(id))
            && String(id) === detail[2] && typeof name === 'string' && name.trim()
            && name.length <= 200) record = {route, name: name.trim()}
        else latest.failed = true
      }
      if (readQuery && ok && Array.isArray(body?.data?.list) &&
          Number.isSafeInteger(body.data.total) && body.data.total >= body.data.list.length &&
          body.data.list.length <= readQuery.size) {
        latest.pending++
        const total = body.data.total, rows = body.data.list.length, path = url.pathname
        return readQuery.digest.then(digest => {
          if (digest && !disposed && current() === heldGeneration && reads.get(key)?.serial === serial && !latest.failed) {
            appliedRead = {path, ...digest, revision: serial,
              page: readQuery.page, page_size: readQuery.size, total, rows}
          }
        }).finally(() => {
          // Another request can replace this endpoint's state while hashing.
          // Its inherited pending count still includes this digest operation.
          if (!disposed && current() === heldGeneration) {
            const active = reads.get(key)
            if (active) active.pending = Math.max(0, active.pending - 1)
          }
        })
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
  function readPageRead() {
    current()
    if (!appliedRead || readPageBlocker() || readPageRoute() !== route) return undefined
    return {...appliedRead}
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
      const instance = matchedPageInstance(app, active, matched)
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
      // A list showing fifty or a hundred rows renders tens of thousands of nodes; at
      // 8192 such a page could never prove it had arrived.
      const renderLimit = 65536
      let content = false
      // Bound element wrappers and child components together; truncation must
      // not drop a blocking error and still grant arrival.
      while (render.length && rendered.size < renderLimit) {
        const node = render.pop()
        if (!node || rendered.has(node)) continue
        rendered.add(node)
        if (node.suspense?.pendingBranch || node.component &&
            (!mounted(node.component) || errorComponent(node.component))) return undefined
        if (node.el?.nodeType === 1 && node.el.isConnected && main.contains(node.el) && visible(node.el)) content = true
        if (node.component) render.push(node.component.subTree)
        if (Array.isArray(node.children)) {
          if (node.children.length + render.length > renderLimit) return undefined
          render.push(...node.children)
        }
      }
      if (render.length || !content || Array.from(doc.querySelectorAll(
        '.el-loading-mask,.el-message--error,.el-notification--error,[aria-busy="true"]')).some(visible)
          || readPageBlocker()) return undefined
      return route
    } catch { return undefined }
  }
  // Applied query facets for target settlement: one entry per query-form control
  // whose selection or value is visibly non-default. Names are the rendered field
  // labels — what a person sees and what set_target declares. Pagination, dialogs
  // and hidden controls are not page conditions. The applied GET (appliedQuery)
  // must exist: a form the page has not yet read proves no applied condition.
  function readPageFacets() {
    if (disposed || !appliedQuery || readPageBlocker()) return []
    const facts = []
    for (const item of Array.from(doc.querySelectorAll('.el-form-item')).filter(visible)) {
      if (item.closest('[role="dialog"],dialog')) continue
      const label = item.querySelector('.el-form-item__label')?.textContent?.trim()
      if (!label) continue
      const filled = item.querySelector(
        '.el-select__selected-item:not(.is-hidden), .el-radio.is-checked, .el-checkbox.is-checked')
        || Array.from(item.querySelectorAll('input:not([type="hidden"]), textarea'))
            .filter(visible).some(input => input.value !== '')
      if (filled && facts.length < 16) facts.push({kind: 'filter', name: label})
    }
    return facts
  }
  return {begin, readPageBlocker, readPageRecordRoute, readPageRoute, readPageQuery, readPageRead,
    readPageFacets,
    dispose() {disposed = true; reads.clear(); record = null; appliedQuery = null; appliedRead = null}}
}

/** What this page's own API requests carry while the assistant is working it. */
export const ASSISTANT_HEADER = 'X-WOA-Assistant'

export function installPageFacts(view = window, doc = document) {
  const facts = createPageFacts({view, doc})
  const originalFetch = view.fetch
  const originalOpen = view.XMLHttpRequest.prototype.open
  const originalSend = view.XMLHttpRequest.prototype.send
  const requests = new WeakMap()
  // While the assistant works this page, the requests this page sends say so, and only
  // those are held read-only by the deployment. The mark starts when the window is
  // confirmed and stops the moment its end is asked for; another tab, another device and
  // this page before or after never carry it, so a person's own saves are never held.
  let assistantUntil = 0
  const assistantWorking = () => Date.now() < assistantUntil
  const guardStep = url => {
    try {
      const target = new URL(String(url), view.location.href)
      const step = target.origin === view.location.origin && /^\/woa\/page-guard\/(begin|end)$/.exec(target.pathname)
      return step ? step[1] : ''
    } catch { return '' }
  }
  const fetch = async function(input, init) {
    const url = typeof input === 'string' || input instanceof URL ? String(input) : input.url
    const step = guardStep(url)
    if (step === 'end') assistantUntil = 0
    const settle = facts.begin(url, init?.method || input?.method || 'GET')
    if (!step && assistantWorking()) {
      try {
        const target = new URL(url, view.location.href)
        if (target.origin === view.location.origin && target.pathname.startsWith('/admin-api/')) {
          const headers = new view.Headers(init?.headers || (typeof input === 'object' && input.headers) || {})
          headers.set(ASSISTANT_HEADER, '1')
          init = {...init, headers}
        }
      } catch { /* the original implementation owns invalid requests */ }
    }
    try {
      const response = await originalFetch.call(this, input, init)
      if (step === 'begin') {
        let window_
        try { window_ = response.ok ? await response.clone().json() : null } catch { window_ = null }
        assistantUntil = window_?.active === true && typeof window_.expires_at === 'number'
          ? window_.expires_at * 1000 : 0
      }
      if (settle) {
        let body
        try { body = await response.clone().json() } catch { /* invalid envelopes fail closed */ }
        await settle(response.status, body)
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
        requests.set(this, {method, path: target.pathname, ledger, order, query, readQuery: listReadQuery(target, view),
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
    const settle = request && facts.begin(target.href, request.method, request.readQuery)
    if (settle) this.addEventListener('loadend', () => {
      let body
      try { body = this.responseType === 'json' ? this.response : JSON.parse(this.responseText) } catch { /* blocked */ }
      settle(this.status, body)
    }, {once: true})
    if (request && assistantWorking()) {
      try { this.setRequestHeader(ASSISTANT_HEADER, '1') } catch { /* the request is already sent */ }
    }
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
export const readPageRead = () => installed?.readPageRead()
export const readPageFacets = () => installed ? installed.readPageFacets() : []
export const disposeInstalledPageFacts = () => installed?.dispose()
/** How long the application is given to show a page it was asked to open. */
const OPEN_WAIT_MS = 3000

/**
 * Open one of the application's own pages through its router, in this document.
 *
 * Loading a document for it throws away the assistant's open window, the person's place
 * and the seconds the application takes to start, so the application's router is asked
 * instead. The router's history entries are its own (`back`, `current`, `position`):
 * nothing here writes to the History API, which is what left the router without them.
 *
 * What a loaded document gave is kept: the page is built fresh, and no other page lives on
 * behind it. The layout keeps every visited page alive with the filters and the tab it was
 * left with, and such a page goes on reading its own list whenever the route changes; read
 * on another page's address, that list would be taken for that page's. So the kept pages
 * are dropped first, as a document load drops them (the tab strip is not stored), and the
 * page is entered through the application's own `/redirect` route, the one its tab refresh
 * uses: a read the page being left starts on the way out belongs to the address in
 * between, and is never the destination's.
 *
 * True only when the application now shows the wanted address, and it is another than
 * before. Everything else -- no router or tab store to ask, the address already shown, a
 * fragment, a navigation the router refused or took elsewhere -- is false, and the caller
 * loads the document.
 */
export async function openPage(url, {view = window, doc = document} = {}) {
  const globals = doc.getElementById('app')?.__vue_app__?.config?.globalProperties
  const router = globals?.$router, tabs = globals?.$pinia?._s?.get?.('tagsView')
  if (typeof router?.push !== 'function' || typeof router.resolve !== 'function' ||
      typeof tabs?.delAllViews !== 'function') return false
  let target, wanted, between
  try { target = new URL(String(url), view.location.href) } catch { return false }
  if (target.origin !== view.location.origin || target.hash) return false
  const shown = () => router.currentRoute?.value?.fullPath
  const before = view.location.pathname + view.location.search
  try {
    wanted = router.resolve(target.pathname + target.search)
    between = router.resolve({path: '/redirect' + wanted.path, query: wanted.query})
  } catch { return false }
  if (!wanted?.matched?.length || wanted.fullPath === shown() || between?.name !== 'Redirect') return false
  try {
    tabs.delAllViews()
    await new Promise(resolve => view.setTimeout(resolve, 0))
    if (await router.push(between)) return false
    const until = Date.now() + OPEN_WAIT_MS
    while (shown() !== wanted.fullPath) {
      if (Date.now() > until) return false
      await new Promise(resolve => view.setTimeout(resolve, 25))
    }
  } catch { return false }
  return view.location.pathname + view.location.search !== before
}

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
/** Current page component, excluding cached/deactivated route instances. */
function currentPageComponent() {
  const app = document.getElementById('app')?.__vue_app__
  const active = app?.config?.globalProperties?.$router?.currentRoute?.value
  const matched = active?.matched?.at(-1), instance = matchedPageInstance(app, active, matched)
  if (!matched || matched.redirect || active.path !== location.pathname ||
      instance?.isMounted !== true || instance.isUnmounted !== false || instance.isDeactivated !== false ||
      instance.appContext?.app !== app || instance.type !== matched.components?.default ||
      instance.vnode?.component !== instance || instance.vnode?.key !== active.fullPath) return undefined
  return instance
}

/** Bind a real framework root to the active page's mounted VNode tree. */
function pageComponentForRoot(root, name) {
  const owner = currentPageComponent()
  if (!owner) return undefined
  let ancestor = owner.parent, appView
  for (let depth = 0; ancestor && depth < 24; depth++, ancestor = ancestor.parent) {
    if ((ancestor.type?.name || ancestor.type?.__name) === 'AppView') { appView = ancestor; break }
  }
  const roots = appView?.subTree?.type === 'section' ? [appView.subTree]
    : Array.isArray(appView?.subTree?.children) ? appView.subTree.children : []
  const sections = roots.filter(node => node?.type === 'section' && node.el?.isConnected)
  if (sections.length !== 1 || !sections[0].el.contains(root)) return undefined
  const live = item => item?.isMounted === true && item.isUnmounted === false && item.isDeactivated === false
  const stack = [owner.subTree], seen = new Set()
  let instance
  const owned = []
  while (stack.length && seen.size < 8192) {
    const node = stack.pop()
    if (!node || seen.has(node)) continue
    seen.add(node)
    if (name !== 'ElForm' && node.el instanceof HTMLElement && node.el !== root && !node.el.contains(root)) continue
    if (node.component && live(node.component)) {
      if ((node.component.type?.name || node.component.type?.__name) === name &&
          node.component.appContext === owner.appContext) {
        if (node.component.vnode?.ctx === owner && !node.component.subTree?.el?.closest('[role="dialog"],dialog')) {
          owned.push(node.component)
        }
        if (node.component.subTree?.el === root) {
          if (instance && instance !== node.component) return undefined
          instance = node.component
        }
      }
      stack.push(node.component.subTree)
    }
    if (Array.isArray(node.children)) stack.push(...node.children)
  }
  return stack.length || !instance ? undefined : {owner, instance, owned}
}

/** Local binding facts only; application effect review still owns action authority. */
export function readElementPageControl(element) {
  if (!(element instanceof HTMLElement) || !element.isConnected || element.closest('[role="dialog"],dialog')) return undefined
  const form = element.closest('form')
  if (form) {
    const binding = pageComponentForRoot(form, 'ElForm')
    const model = binding?.instance.props?.model
    // Production script-setup closes over its variables; setupState is not a
    // reliable identity. The source-reviewed single query form must be rendered
    // by this exact active component, not a child editor or cached view.
    if (!binding || binding.owned.length !== 1 || binding.owned[0] !== binding.instance ||
        !model || typeof model !== 'object' || !Number.isSafeInteger(model.pageNo) ||
        model.pageNo < 1 || !Number.isSafeInteger(model.pageSize) || model.pageSize < 1) return undefined
    const itemRoot = element.closest('.el-form-item')
    const item = itemRoot && pageComponentForRoot(itemRoot, 'ElFormItem')
    const field = item?.instance.props?.prop
    return {kind: 'query', ...(typeof field === 'string' ? {field} : {})}
  }
  const root = element.closest('.el-pagination')
  // The rows-per-page choice of the same pagination: its own select, nothing else in it.
  const sizes = root && element.closest('.el-pagination__sizes')
  if (sizes && root.contains(sizes)) {
    const owner = pageComponentForRoot(root, 'ElPagination')
    const size = owner?.instance.props?.pageSize
    // Only the select's own hit area or input: what is inside them is not a control.
    if (!owner || !Number.isSafeInteger(size) || size < 1 ||
        !element.matches('.el-select__wrapper,input[role="combobox"]') ||
        element.matches(':disabled,.is-disabled,[aria-disabled="true"]')) return undefined
    return {kind: 'page_size', pageSize: size}
  }
  const target = element.closest('button.btn-prev,button.btn-next,li.number')
  if (!root || !target || !root.contains(target) || target.matches(':disabled,.is-disabled,[aria-disabled="true"]')) return undefined
  const binding = pageComponentForRoot(root, 'ElPagination')
  if (!binding) return undefined
  const instance = binding.instance
  const {currentPage, pageSize, total} = instance.props
  if (![currentPage, pageSize, total].every(Number.isSafeInteger) || currentPage < 1 || pageSize < 1 || total < 0) return undefined
  const page = target.matches('.btn-prev') ? currentPage - 1 : target.matches('.btn-next') ? currentPage + 1 : Number(target.textContent.trim())
  if (!Number.isSafeInteger(page) || page < 1 || page > Math.ceil(total / pageSize) || page === currentPage) return undefined
  return {kind: 'pagination', page, pageSize}
}

/** Map one Element Plus select hit area to its own accessible form input. */
export function nativeControlElement(element) {
  // The controller may index a query label while omitting its readonly select
  // input. Resolve only HTML's exact association inside the current owned query
  // item; keep the original label as the click hit area.
  if (element instanceof HTMLLabelElement && readElementPageControl(element)?.kind === 'query') {
    const control = element.control, item = element.closest('.el-form-item')
    if (control instanceof HTMLInputElement && item && control.closest('.el-form-item') === item
        && /^[A-Za-z][A-Za-z0-9_:.-]{0,199}$/.test(control.id)
        && document.querySelectorAll(`[id="${control.id}"]`).length === 1
        && control.getAttribute('role') === 'combobox' && control.getAttribute('aria-haspopup') === 'listbox'
        && !control.disabled && control.getAttribute('aria-disabled') !== 'true'
        && control.closest('.el-select__wrapper')?.classList.contains('is-disabled') === false) return control
  }
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

/**
 * The controls of an editable table inside a form, each with the row and the column it
 * belongs to. A line table gives its controls no label of their own, and the generic
 * controller leaves a searchable select inside a scrolling table unindexed (its input sits
 * under the placeholder, and its wrapper is not a distinct control once the table itself
 * is indexed). `area` is what is pressed; `control` is the native input it owns.
 */
export function lineControls(root = document) {
  const shown = element => {
    const rect = element.getBoundingClientRect()
    return element.isConnected && rect.width > 0 && rect.height > 0 &&
      rect.bottom > 0 && rect.top < (window.innerHeight || 0) && !element.closest('[hidden],[aria-hidden="true"]')
  }
  const text = element => (element.textContent || '').replace(/\s+/g, ' ').trim()
  const found = []
  for (const table of root.querySelectorAll('.el-form .el-table')) {
    const heads = [...table.querySelectorAll('.el-table__header-wrapper thead tr:last-child th')].map(text)
    const rows = [...table.querySelectorAll('.el-table__body-wrapper tbody > tr')]
    rows.forEach((row, at) => {
      for (const cell of row.children) {
        const column = cell instanceof HTMLTableCellElement ? heads[cell.cellIndex] : ''
        if (!column) continue
        for (const control of cell.querySelectorAll('input:not([type="hidden"]):not([type="file"]),textarea')) {
          if (control.disabled || control.closest('.is-disabled')) continue
          const wrapper = control.closest('.el-select__wrapper')
          const area = wrapper && cell.contains(wrapper) ? wrapper : control
          if (!shown(area)) continue
          const hint = wrapper && wrapper.querySelector('.el-select__placeholder')
          found.push({area, control, row: at + 1, column,
            select: Boolean(wrapper) && control.getAttribute('role') === 'combobox',
            ...(hint ? {[hint.classList.contains('is-transparent') ? 'placeholder' : 'value']: text(hint)} : {})})
        }
      }
    })
  }
  return found
}

const attribute = value => /[\s"'<>=]/.test(value) ? `"${value.replace(/"/g, "'")}"` : value

/**
 * Say of each line-table control which row and column it is, and index the selects the
 * generic controller left out. Returns the page text with those records; `selectorMap`
 * gains the added indices. Nothing on the page is changed.
 */
export function withLineControls(selectorMap, html, lines) {
  if (!lines.length) return html
  const texts = html.split('\n'), indexOf = new Map()
  for (const [index, node] of selectorMap) indexOf.set(node.ref, index)
  const at = index => texts.findIndex(line => new RegExp(`^\\s*\\*?\\[${index}\\]<`).test(line))
  let top = Math.max(-1, ...selectorMap.keys())
  const anchors = new Map(), missing = []
  for (const line of lines) {
    const label = attribute(`明细第${line.row}行·${line.column}`.replace(/\s+/g, ''))
    const index = indexOf.get(line.control) ?? indexOf.get(line.area)
    if (index === undefined) { missing.push([line, label]); continue }
    const position = at(index)
    if (position < 0) continue
    if (!anchors.has(line.row)) anchors.set(line.row, index)
    // Only a record without text of its own: `[n]<input ... />`.
    const record = /^(\s*\*?\[\d+\]<\w+)([^>]*?)\s*\/>$/.exec(texts[position])
    if (record && !/\stitle=/.test(record[2])) texts[position] = `${record[1]}${record[2]} title=${label} />`
  }
  for (const [line, label] of missing) {
    const index = ++top
    selectorMap.set(index, {ref: line.area, tagName: 'input', isInteractive: true, highlightIndex: index})
    const anchor = anchors.has(line.row) ? at(anchors.get(line.row)) : -1
    const indent = anchor >= 0 ? /^\s*/.exec(texts[anchor])[0] : ''
    const said = line.value ? ` value=${attribute(line.value)}` : line.placeholder ? ` placeholder=${attribute(line.placeholder)}` : ''
    const record = `${indent}[${index}]<input${line.select ? ' role=combobox aria-haspopup=listbox' : ''} title=${label}${said} />`
    if (anchor >= 0) texts.splice(anchor, 0, record)
    else texts.push(record)
  }
  return texts.join('\n')
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
