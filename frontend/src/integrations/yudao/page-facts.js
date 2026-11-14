/** App-owned page facts. Load before the application's module entry.
 * Observes same-origin application GET completion; retains no credentials,
 * bodies or business values except a bounded visible detail title and reviewed
 * scalar applied-query parameters.
 * A navigation fact additionally requires the deployed AppView's live Vue mount.
 * This is observation, never the application's API write fence.
 */
const PAGE_LAYERS = '.el-overlay, .el-image-viewer__wrapper'
// Only layers carried across an assistant navigation block its destination. A form
// opened on the destination itself must remain observable for prefill and handoff.
const carriedLayers = new WeakMap()
const recordDetailRoute = route => /^\/(crm\/(?:customer|contract|business|contact|clue|product)|hrm\/employee)\/detail\/([0-9]+)$/.exec(route)
function carriedLayerBlocks(doc) {
  const layers = carriedLayers.get(doc)
  if (!layers) return false
  for (const element of layers) {
    const box = element.getBoundingClientRect()
    if (!element.isConnected || !box.width || !box.height ||
        element.closest('[hidden],[aria-hidden="true"]')) layers.delete(element)
  }
  if (!layers.size) carriedLayers.delete(doc)
  return layers.size > 0
}

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
  const patterns = {no: /^[A-Za-z0-9_-]{1,64}$/, customerId: /^[1-9][0-9]{0,19}$/, status: /^(0|10|20)$/,
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

function creationRangeList(route, path) {
  return route === '/infra/config' && path === '/admin-api/infra/config/page'
    || route === '/mall/trade/order' && path === '/admin-api/trade/order/page'
}

function creationRangeQuery(url) {
  const keys = {'createTime[0]': 'createTimeStart', 'createTime[1]': 'createTimeEnd'}
  const patterns = {createTimeStart: /^\d{4}-\d{2}-\d{2} 00:00:00$/,
    createTimeEnd: /^\d{4}-\d{2}-\d{2} 23:59:59$/}
  const parameters = {}, pairs = Array.from(url.searchParams)
  if (new Set(pairs.map(([key]) => key)).size !== pairs.length) return null
  for (const [raw, value] of pairs) {
    if (['pageNo', 'pageSize'].includes(raw)) continue
    const key = keys[raw] || raw
    if (!Object.hasOwn(patterns, key) || Object.hasOwn(parameters, key) || !patterns[key].test(value)) return null
    parameters[key] = value
  }
  if (Object.keys(parameters).length === 1) return null
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

/** Row identities of a list response: its record IDs, or none when any row lacks one. */
function listRows(rows) {
  const ids = rows.map(row => row && (typeof row.id === 'number' && Number.isSafeInteger(row.id)
    || typeof row.id === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(row.id)) ? String(row.id) : null)
  return {count: rows.length, ids: ids.every(id => id !== null) ? ids : null}
}

/** Whether a table's own records are this list response's rows. Without record IDs,
 * the row count and the page's one visible total must agree instead. */
function rendersList(list, data, totals) {
  if (data.length !== list.rows.count) return false
  if (!list.rows.ids) return totals.length === 1 && totals[0] === list.total
  const shown = data.map(row => row?.id === undefined || row?.id === null ? null : String(row.id))
  return shown.every(id => id !== null) && shown.sort().join('\n') === [...list.rows.ids].sort().join('\n')
}

export function createPageFacts({view = window, doc = document} = {}) {
  let route = '', locationKey = '', generation = 0, record = null, appliedLedger = null, listSerial = 0, disposed = false
  const reads = new Map()
  // The latest list request per endpoint. A list is the page's own query only while a
  // visible table of the page renders its rows (pageList): a cached page refreshing its
  // list on every route change, or another list on the page, never stands in for it.
  const lists = new Map()
  const visible = element => Boolean(element && element.getBoundingClientRect().width
    && element.getBoundingClientRect().height && !element.closest('[hidden],[aria-hidden="true"]'))
  function current() {
    const next = view.location.pathname + view.location.search + view.location.hash
    if (next !== locationKey) {
      locationKey = next; route = view.location.pathname; generation++; record = null; appliedLedger = null
      reads.clear(); lists.clear()
    }
    return generation
  }
  function pageList() {
    const tables = []
    for (const root of Array.from(doc.querySelectorAll('.el-table'))) {
      if (!visible(root) || root.closest('[role="dialog"],dialog')) continue
      const data = pageComponentForRoot(root, 'ElTable')?.instance?.props?.data
      if (Array.isArray(data)) tables.push(data)
    }
    if (!tables.length || tables.length > 8) return undefined
    const totals = Array.from(doc.querySelectorAll('.el-pagination__total'))
      .filter(element => visible(element) && !element.closest('[role="dialog"],dialog'))
      .map(element => Number((element.textContent || '').replace(/[^0-9]/g, '')))
    // Two endpoints both rendered are ambiguous: report neither.
    const matching = Array.from(lists.values()).filter(list => list.rows && tables.some(data => rendersList(list, data, totals)))
    return matching.length === 1 ? matching[0] : undefined
  }
  function begin(input, method = 'GET', readQuery) {
    if (disposed || String(method).toUpperCase() !== 'GET') return null
    let url
    try { url = new URL(input, view.location.href) } catch { return null }
    if (url.origin !== view.location.origin || !url.pathname.startsWith('/admin-api/')) return null
    const heldGeneration = current()
    readQuery = readQuery === undefined ? listReadQuery(url, view) : readQuery
    const ledger = route === '/fms/ledger/general' && url.pathname === '/admin-api/fms/ledger/general/list'
    const order = route === '/erp/sale/order' && url.pathname === '/admin-api/erp/sale-order/page'
    const creation = creationRangeList(route, url.pathname)
    let query = null
    if (ledger || order || creation) {
      if (ledger) appliedLedger = null
      // Application-owned scalar query observation for the first native report.
      // Retain only reviewed query fields, never headers, bodies or arbitrary URLs.
      query = ledger ? ledgerQuery(url) : order ? orderQuery(url) : creationRangeQuery(url)
    } else if (url.pathname.endsWith('/page') && route && route.indexOf('/detail/') < 0) {
      // Any other collection list read may be the page's applied query once a page
      // table renders it. Scalar projection stays on reviewed lists; this general
      // capture feeds applied-query facets for target settlement on every list.
      query = {path: url.pathname, parameters: Object.fromEntries(url.searchParams)}
    }
    // A newer request to an endpoint replaces its earlier list at send, failures included.
    let list = null
    if (url.pathname.endsWith('/page')) {
      list = {serial: ++listSerial, query: null, read: null, rows: null, total: null}
      lists.delete(url.pathname)
      if (lists.size >= 16) lists.delete(lists.keys().next().value)
      lists.set(url.pathname, list)
    }
    const detail = recordDetailRoute(route)
    const isRecord = Boolean(detail && url.pathname === `/admin-api/${detail[1]}/get`)
    const ids = url.searchParams.getAll('id')
    const matchesRecord = !isRecord || ids.length === 1 && ids[0] === detail[2]
    const selected = Array.from(doc.querySelectorAll('[role="tab"][aria-selected="true"]'))
      .filter(e => visible(e) && !e.closest('[role="dialog"],dialog'))
    const scoped = Boolean(detail && url.pathname.startsWith(`/admin-api/${detail[1].split('/')[0]}/`))
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
      if (ledger && ok && query && Array.isArray(body?.data)) appliedLedger = query
      if (list && lists.get(url.pathname) === list && ok && Array.isArray(body?.data?.list)) {
        list.rows = listRows(body.data.list)
        list.total = Number.isSafeInteger(body.data.total) ? body.data.total : null
        list.query = query
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
          if (digest && list && !disposed && current() === heldGeneration && reads.get(key)?.serial === serial
              && !latest.failed && lists.get(path) === list) {
            list.read = {path, ...digest, revision: serial,
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
    if (carriedLayerBlocks(doc)) return true
    const selected = Array.from(doc.querySelectorAll('[role="tab"][aria-selected="true"]')).filter(e => visible(e) && !e.closest('[role="dialog"],dialog'))
    const panel = selected[0]?.getAttribute('aria-controls') || ''
    const relevant = Array.from(reads.values()).filter(s => s.panel === '*' || !s.panel || s.panel === panel)
    if (relevant.some(s => s.failed || s.conflicts)) return true
    if (Array.from(doc.querySelectorAll('.el-message--error, .el-notification--error')).some(visible)) return true
    if (relevant.some(s => s.pending)) return 'pending'
    const loading = Array.from(doc.querySelectorAll('.el-loading-mask,.el-loading-spinner,[aria-busy="true"]')).some(visible)
    if (!recordDetailRoute(route)) return loading ? 'pending' : false
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
    if (!appliedLedger && !Array.from(lists.values()).some(list => list.query)) return undefined
    if (readPageBlocker() || readPageRoute() !== route) return undefined
    const query = appliedLedger || pageList()?.query
    return query ? {path: query.path, parameters: {...query.parameters}} : undefined
  }
  function readPageRead() {
    current()
    if (!Array.from(lists.values()).some(list => list.read)) return undefined
    if (readPageBlocker() || readPageRoute() !== route) return undefined
    const read = pageList()?.read
    return read ? {...read} : undefined
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
  // and hidden controls are not page conditions. The applied GET (the ledger's, or the
  // list a page table renders) must exist: a form the page has not yet read proves no
  // applied condition.
  function readPageFacets() {
    if (disposed || readPageBlocker() || !(appliedLedger || pageList()?.query)) return []
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
    dispose() {disposed = true; reads.clear(); lists.clear(); record = null; appliedLedger = null}}
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
        const creation = creationRangeList(view.location.pathname, target.pathname)
        // Retain only a bounded scalar projection; open alone starts no read.
        const query = ledger ? ledgerQuery(target) : order ? orderQuery(target) : creation ? creationRangeQuery(target) : null
        requests.set(this, {method, path: target.pathname, ledger, order, creation, query, readQuery: listReadQuery(target, view),
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
      if (request.ledger || request.order || request.creation) {
        const keys = {orderTimeStart: 'orderTime[0]', orderTimeEnd: 'orderTime[1]',
          createTimeStart: 'createTime[0]', createTimeEnd: 'createTime[1]'}
        if (!request.query) target.searchParams.set('invalidQuery', '1')
        for (const [key, value] of Object.entries(request.query?.parameters || {})) {
          target.searchParams.set(keys[key] || key, value)
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
/** How long the application is given to show a page it was asked to open, and then how
 * long the page's own component is given to be on the screen. Together they stay under the
 * time the Runtime allows an application before it loads the page itself. */
const OPEN_WAIT_MS = 3000
const MOUNT_WAIT_MS = 2500
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
 * Page-owned dialogs and drawers leave with the component that owns them. Do not turn
 * their presence into a document reload. A layout-owned overlay can outlive this route;
 * leave it alone, and let the fresh observation report that the destination is blocked.
 * Opening an address is not proof that its page state is ready or unobscured.
 *
 * The address changes before the page is there: a route's component is fetched when it is
 * first entered. A loaded document was looked at seconds after its address; here the page
 * is waited for, within a bound, so that the first look at it is at the page and not at
 * the empty layout around it.
 *
 * True only when the application now shows the wanted address, and it is another than
 * before. Everything else -- no router or tab store to ask, the address already shown, a
 * fragment, a navigation the router refused or took
 * elsewhere -- is false, and the caller loads the document.
 */
export async function openPage(url, {view = window, doc = document,
                                      mounted = () => Boolean(currentPageComponent())} = {}) {
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
  const layers = Array.from(doc.querySelectorAll(PAGE_LAYERS)).filter(element => {
    const box = element.getBoundingClientRect()
    return box.width && box.height
  })
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
  if (view.location.pathname + view.location.search === before) return false
  if (layers.length) carriedLayers.set(doc, new Set([...(carriedLayers.get(doc) || []), ...layers]))
  const built = Date.now() + MOUNT_WAIT_MS
  try {
    while (shown() === wanted.fullPath && !mounted() && Date.now() < built) {
      await new Promise(resolve => view.setTimeout(resolve, 25))
    }
  } catch { /* the page is open; whether it is built yet is the next observation's to say */ }
  return true
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
function pageComponentForRoot(root, name, allowDialog = false) {
  const owner = currentPageComponent()
  if (!owner) return undefined
  let ancestor = owner.parent, appView
  for (let depth = 0; ancestor && depth < 24; depth++, ancestor = ancestor.parent) {
    if ((ancestor.type?.name || ancestor.type?.__name) === 'AppView') { appView = ancestor; break }
  }
  const roots = appView?.subTree?.type === 'section' ? [appView.subTree]
    : Array.isArray(appView?.subTree?.children) ? appView.subTree.children : []
  const sections = roots.filter(node => node?.type === 'section' && node.el?.isConnected)
  if (sections.length !== 1 || !sections[0].el.contains(root)
      && !(allowDialog && root.closest('[role="dialog"],dialog'))) return undefined
  const live = item => item?.isMounted === true && item.isUnmounted === false && item.isDeactivated === false
  const stack = [owner.subTree], seen = new Set()
  let instance
  const owned = []
  while (stack.length && seen.size < (allowDialog ? 65536 : 8192)) {
    const node = stack.pop()
    if (!node || seen.has(node)) continue
    seen.add(node)
    if (!allowDialog && name !== 'ElForm' && node.el instanceof HTMLElement && node.el !== root && !node.el.contains(root)) continue
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

/** The native editor's rendered action and actual model must agree. This is a
 * current observation, never a grant to fill or submit. Unknown/custom modes
 * remain absent; no prompt, button intent or cached route supplies a mode.
 */
function nativeFormContext(element) {
  const dialog = element.closest('[role="dialog"],dialog')
  if (!dialog) return undefined
  const forms = Array.from(dialog.querySelectorAll('form.el-form')).filter(form =>
    form.getBoundingClientRect().width && form.getBoundingClientRect().height)
  // Native line editors may contain their own ElForm. The enclosing editor's
  // record owns the mode; a nested lines array is not another business record.
  const roots = forms.filter(form => !forms.some(other => other !== form && other.contains(form)))
  if (forms.length > 32 || roots.length !== 1 || Array.from(dialog.querySelectorAll('.el-loading-mask')).some(mask =>
    mask.getBoundingClientRect().width && mask.getBoundingClientRect().height)) return undefined
  const form = roots[0], bound = pageComponentForRoot(form, 'ElForm', true)
  const model = bound?.instance.props?.model
  if (!model || typeof model !== 'object' || Array.isArray(model)) return undefined
  const title = dialog.querySelector('.el-dialog__header')?.textContent?.trim()
  const id = model.id
  const dateRoot = element instanceof HTMLInputElement && element.closest('.el-date-editor')
  const input = dateRoot && form.contains(dateRoot) && pageComponentForRoot(dateRoot, 'ElInput', true)
  // ElDatePicker's root is a fragment. Start with its actual mounted ElInput
  // and follow framework ownership, never DOM classes as a precision claim.
  let picker, parent = input?.owner === bound.owner ? input.instance.parent : undefined
  for (let depth = 0; parent && depth < 12; depth++, parent = parent.parent) {
    if (parent.appContext !== bound.owner.appContext || parent.isMounted !== true ||
        parent.isUnmounted !== false || parent.isDeactivated !== false) break
    const name = parent.type?.name || parent.type?.__name
    if (name === 'ElForm') break
    if (name === 'ElDatePicker') {picker = parent; break}
  }
  // Precision comes from the mounted picker, not a placeholder or CSS class.
  // Custom display formats and ranges require their own native contract.
  const type = picker?.props?.type
  const precision = picker && ['date', 'datetime'].includes(type) && !picker.props.format
    ? {inputKind: type} : {}
  if (title === '新增' && (id === undefined || id === null || id === '')) {
    return {kind: 'form', mode: 'create', recordId: null, ...precision}
  }
  if (title === '编辑' && (typeof id === 'number' && Number.isSafeInteger(id) && id > 0
      || typeof id === 'string' && /^[1-9][0-9]{0,19}$/.test(id))) {
    return {kind: 'form', mode: 'update', recordId: String(id), ...precision}
  }
  return undefined
}

/** Local binding facts only; application effect review still owns action authority. */
export function readElementPageControl(element) {
  if (!(element instanceof HTMLElement) || !element.isConnected) return undefined
  if (element.closest('[role="dialog"],dialog')) return nativeFormContext(element)
  const nodeRoot = element.closest('.el-tree-node[role="treeitem"]')
  const hit = nodeRoot?.querySelector(':scope > .el-tree-node__content')
  if (nodeRoot && (element === nodeRoot || element === hit)) {
    const treeRoot = nodeRoot.closest('.el-tree[role="tree"]')
    const tree = treeRoot && pageComponentForRoot(treeRoot, 'ElTree')
    const node = pageComponentForRoot(nodeRoot, 'ElTreeNode')
    const wrapper = tree?.instance.vnode?.ctx
    const data = node?.instance.props?.node?.data
    // Only this application's reviewed department selector, rendered by the active
    // page. DOM data-key/text alone cannot turn another tree into a query control.
    if (tree && node && tree.owner === node.owner && wrapper?.type?.name === 'DeptTreeSelect'
        && wrapper.vnode?.ctx === tree.owner && wrapper.isMounted === true
        && wrapper.isUnmounted === false && wrapper.isDeactivated === false
        && tree.instance.props?.nodeKey === 'id' && tree.instance.props?.showCheckbox === false
        && tree.instance.props?.draggable === false && Number.isSafeInteger(data?.id) && data.id > 0
        && typeof data.name === 'string' && data.name.trim()
        && hit?.querySelector('.el-tree-node__label')?.textContent?.trim() === data.name.trim()
        && String(data.id) === nodeRoot.getAttribute('data-key')) {
      return {kind: 'query', field: 'deptId', queryValue: String(data.id),
        selected: node.instance.props.node.isCurrent === true}
    }
    return undefined
  }
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
    const value = typeof field === 'string' ? model[field] : undefined
    // Only an actor-rendered select's scalar native ID/code. Text and other
    // component state stay in the browser; this binds later applied-query proof.
    const choice = element.closest('.el-select') &&
      (typeof value === 'boolean' || Number.isSafeInteger(value) && value >= 0 ||
        typeof value === 'string' && /^(?:[0-9]{1,20}|true|false)$/.test(value))
    return {kind: 'query', ...(typeof field === 'string' ? {field} : {}),
      ...(choice ? {queryValue: String(value)} : {})}
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

/** Visible tree hit areas omitted by the generic controller. Never index a branch's
 * whole subtree: clicking its centre can hit a different descendant. */
export function withTreeQueryControls(selectorMap, html, root = document) {
  const lines = []
  const counts = new Map()
  for (const label of root.querySelectorAll('.el-tree-node__content > .el-tree-node__label')) {
    const name = label.textContent?.trim()
    if (name) counts.set(name, (counts.get(name) || 0) + 1)
  }
  let top = Math.max(0, ...selectorMap.keys())
  for (const area of root.querySelectorAll('.el-tree-node > .el-tree-node__content')) {
    const rect = area.getBoundingClientRect()
    if (!rect.width || !rect.height || rect.bottom <= 0 || rect.top >= window.innerHeight ||
        area.closest('[hidden],[aria-hidden="true"]') || area.closest('.el-tree-node')?.closest('[role="dialog"],dialog')) continue
    const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2))
    const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2))
    const at = root.elementFromPoint?.(x, y)
    if (at && at !== area && !area.contains(at)) continue
    const fact = readElementPageControl(area)
    if (!fact || lines.length >= 32) continue
    let label = area.querySelector('.el-tree-node__label')?.textContent?.trim()
    if (!label || label.length > 120 || /[<>\r\n]/.test(label)) continue
    if (counts.get(label) > 1) {
      const path = [label]
      let parent = area.parentElement?.parentElement?.closest('.el-tree-node')
      while (parent && path.length < 8) {
        const name = parent.querySelector(':scope > .el-tree-node__content .el-tree-node__label')?.textContent?.trim()
        if (!name || /[<>\r\n]/.test(name)) break
        path.unshift(name)
        parent = parent.parentElement?.closest('.el-tree-node')
      }
      label = path.join(' / ').slice(0, 300)
    }
    const existing = [...selectorMap].find(([, node]) => node.ref === area)
    const index = existing?.[0] ?? ++top
    if (existing) continue
    selectorMap.set(index, {ref: area, tagName: 'div', isInteractive: true, highlightIndex: index})
    lines.push(`[${index}]<div role=treeitem aria-selected=${fact.selected}>${label} />`)
  }
  return lines.length ? lines.join('\n') + '\n' + html : html
}

/** Observe the native user table's status switches, whose hidden input the generic
 * controller omits. This supplies a highlight target; it grants no click authority. */
export function withUserStatusControls(selectorMap, html, root = document) {
  const lines = []
  let top = Math.max(0, ...selectorMap.keys())
  for (const area of root.querySelectorAll('.el-table .el-switch')) {
    if (lines.length >= 32) break
    const rect = area.getBoundingClientRect()
    if (!rect.width || !rect.height || rect.bottom <= 0 || rect.top >= window.innerHeight ||
        area.closest('[hidden],[aria-hidden="true"],[role="dialog"],dialog')) continue
    const at = root.elementFromPoint?.(Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2)),
      Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2)))
    if (at && at !== area && !area.contains(at)) continue
    const binding = pageComponentForRoot(area, 'ElSwitch')
    const tableRoot = area.closest('.el-table'), table = pageComponentForRoot(tableRoot, 'ElTable')
    if (!binding || !table || binding.owner !== table.owner ||
        (binding.owner.type?.name || binding.owner.type?.__name) !== 'SystemUser') continue
    const rows = Array.from(tableRoot.querySelectorAll('.el-table__body-wrapper tbody > tr'))
    const tr = area.closest('tr'), position = rows.indexOf(tr), records = table.instance.props?.data
    const record = Array.isArray(records) && records.length === rows.length && records[position]
    const props = binding.instance.props, input = area.querySelector('input[role="switch"]')
    if (!record || !Number.isSafeInteger(record.id) || record.id <= 0 ||
        ![0, 1].includes(record.status) || props?.modelValue !== record.status ||
        props.activeValue !== 0 || props.inactiveValue !== 1 || props.disabled ||
        !input || input.getAttribute('aria-checked') !== String(record.status === 0) ||
        tr.querySelectorAll('.el-switch').length !== 1 ||
        tr.cells[1]?.textContent?.trim() !== String(record.id) ||
        tr.cells[2]?.textContent?.trim() !== record.username ||
        tr.cells[3]?.textContent?.trim() !== record.nickname) continue
    const label = `状态开关：${record.nickname}（${record.username}）`
    if (!record.nickname || !record.username || label.length > 120 || /[<>\r\n]/.test(label)) continue
    const index = ++top
    selectorMap.set(index, {ref: area, tagName: 'div', isInteractive: true, highlightIndex: index})
    lines.push(`[${index}]<div role=switch id=woa-user-status-${record.id} aria-checked=${record.status === 0}>${label} />`)
  }
  return lines.length ? lines.join('\n') + '\n' + html : html
}

/** Map one Element Plus select hit area to its own accessible form input. */
export function nativeControlElement(element) {
  // The controller may index an Element Plus form label while omitting its
  // readonly select input. Resolve only HTML's exact association inside one
  // query item or the same dialog form; keep the label as the click hit area.
  if (element instanceof HTMLLabelElement) {
    const control = element.control, item = element.closest('.el-form-item')
    const dialog = element.closest('[role="dialog"],dialog')
    const dialogField = dialog && control?.closest('[role="dialog"],dialog') === dialog
      && element.closest('form') && element.closest('form') === control.closest('form')
    if ((dialogField || !dialog && readElementPageControl(element)?.kind === 'query')
        && control instanceof HTMLInputElement && item && control.closest('.el-form-item') === item
        && item.querySelectorAll('input[role="combobox"]').length === 1
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

/** Bind visible table cells to their own observed row and column.
 * The generic controller may emit the row inside an indexed scrollable wrapper while
 * omitting horizontally clipped headers. Values elsewhere in the page are not evidence
 * that a cell in this row was observed. */
export function withObservedRowColumns(html, root = document, selectorMap = new Map()) {
  const shown = element => {
    const rect = element.getBoundingClientRect()
    return element.isConnected && rect.width > 0 && rect.height > 0 &&
      rect.bottom > 0 && rect.top < (window.innerHeight || 0) &&
      !element.closest('[hidden],[aria-hidden="true"]')
  }
  if ([...root.querySelectorAll('.el-dialog')].some(shown)) return html
  const text = element => (element.textContent || '').replace(/\s+/g, ' ').trim()
  const safeValue = value => value.length > 0 && value.length <= 80 && !/[\n\r]/.test(value)
  const sensitive = /password|token|secret|credential|api.?key|密码|密钥|令牌/i
  const facts = []
  for (const table of root.querySelectorAll('.el-table')) {
    if (table.closest('.el-form') || !shown(table) || facts.length >= 20) continue
    const heads = [...table.querySelectorAll('.el-table__header-wrapper thead tr:last-child th')]
    const rows = [...table.querySelectorAll('.el-table__body-wrapper tbody > tr')].filter(shown).slice(0, 20)
    if (!rows.length || heads.length > 40) continue
    // Find the controller record emitted by the closest indexed wrapper inside this
    // table. A number in another component, even an identical one, cannot prove this
    // row's cell was in the page observation.
    const wrappers = [...selectorMap].filter(([, node]) =>
      node.ref instanceof Element && table.contains(node.ref) && rows.every(row => node.ref.contains(row)))
      .sort((a, b) => a[1].ref === b[1].ref ? 0 : a[1].ref.contains(b[1].ref) ? 1 : -1)
    let observed = ''
    for (const [index, node] of wrappers) {
      const marker = new RegExp(`^\\s*\\*?\\[${index}\\]<${node.tagName || node.ref.tagName.toLowerCase()}\\b`, 'm')
      const start = marker.exec(html)
      if (!start) continue
      const end = html.indexOf(' />', start.index)
      if (end < 0 || end - start.index > 8192) continue
      observed = html.slice(start.index, end + 3)
      break
    }
    const body = observed.slice(observed.indexOf('>') + 1, -3)
    const identities = rows.map(row => [...row.children].map(text).find(value =>
      value.length >= 2 && value.length <= 80 && /[^\d.,-]/.test(value)))
    if (identities.some(identity => !identity) || new Set(identities).size !== rows.length) continue
    const positions = identities.map(identity => body.indexOf(identity))
    if (positions.some((position, index) => position < 0 || body.indexOf(identities[index], position + 1) >= 0 ||
        index > 0 && position <= positions[index - 1])) continue
    for (let rowIndex = 0; rowIndex < rows.length && facts.length < 20; rowIndex++) {
      const cells = [...rows[rowIndex].children]
      if (cells.length !== heads.length) continue
      const segment = body.slice(positions[rowIndex], positions[rowIndex + 1] ?? body.length)
      const lines = segment.split('\n').map(value => value.trim())
      const cellCounts = new Map()
      for (const value of cells.map(text).filter(safeValue)) {
        cellCounts.set(value, (cellCounts.get(value) || 0) + 1)
      }
      const observedCounts = new Map()
      for (const value of lines.filter(safeValue)) {
        observedCounts.set(value, (observedCounts.get(value) || 0) + 1)
      }
      const columns = []
      for (let i = 0; i < cells.length && columns.length < 20; i++) {
        const label = text(heads[i]), value = text(cells[i])
        if (label && label.length <= 40 && !sensitive.test(label) && safeValue(value) &&
            observedCounts.get(value) >= cellCounts.get(value)) columns.push(`${label}=${value}`)
      }
      if (columns.length) facts.push(`第${rowIndex + 1}行 ${columns.join('；')}`)
    }
  }
  return facts.length ? '可见列表行（每行的表头和值）: ' + facts.join(' | ').slice(0, 3000) + '\n' + html : html
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

/** One bounded native calendar step toward a date outside the displayed months. */
export function nativeRangeDateStep(element, value) {
  if (!(element instanceof HTMLInputElement) || element.disabled || element.readOnly ||
      !element.matches('.el-date-editor--daterange > .el-range-input') ||
      !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const [year, monthNumber, day] = value.split('-').map(Number)
  if (new Date(Date.UTC(year, monthNumber - 1, day)).toISOString().slice(0, 10) !== value) return null
  const id = element.getAttribute('aria-controls')
  if (!id || !/^[A-Za-z][A-Za-z0-9_:.-]{0,199}$/.test(id) || element.getAttribute('aria-haspopup') !== 'dialog') return null
  const popups = element.ownerDocument.querySelectorAll(`[id="${id}"]`)
  if (popups.length !== 1 || popups[0].getAttribute('role') !== 'dialog') return null
  const left = popups[0].querySelector('.el-date-range-picker__content.is-left')
  const right = popups[0].querySelector('.el-date-range-picker__content.is-right')
  if (!left || !right) return null
  const month = panel => {
    const labels = [...panel.querySelectorAll('.el-date-range-picker__header [role="button"]')]
      .map(e => (e.textContent || '').replace(/\s/g, ''))
    const year = /^(\d{4})年$/.exec(labels[0] || '')
    const number = /^(\d{1,2})月$/.exec(labels[1] || '')
    if (!year || !number || +number[1] < 1 || +number[1] > 12) return null
    return +year[1] * 12 + +number[1] - 1
  }
  const first = month(left), last = month(right)
  if (first === null || last !== first + 1) return null
  const target = +value.slice(0, 4) * 12 + +value.slice(5, 7) - 1
  const button = target < first
    ? left.querySelector('button.arrow-left[aria-label="上个月"]')
    : target > last ? right.querySelector('button.arrow-right[aria-label="下个月"]') : null
  return button instanceof HTMLButtonElement && !button.disabled && button.isConnected ? button : null
}

// Fixed component operations. The server still admits every ordinary action and
// checks the application's request/arrival receipts. These functions hold no plan,
// permissions, API client or source rules.
function liveVNodes(owner = currentPageComponent()) {
  if (!owner) return []
  const stack = [owner.subTree], seen = new Set(), nodes = []
  while (stack.length && seen.size < 65536) {
    const node = stack.pop()
    if (!node || seen.has(node)) continue
    seen.add(node)
    nodes.push(node)
    const c = node.component
    if (c?.isMounted && !c.isUnmounted && !c.isDeactivated && c.appContext === owner.appContext) stack.push(c.subTree)
    if (Array.isArray(node.children)) stack.push(...node.children)
    if (node.suspense?.activeBranch) stack.push(node.suspense.activeBranch)
  }
  return stack.length ? [] : nodes
}

function available(element) {
  return element instanceof HTMLElement && element.isConnected && element.getBoundingClientRect().width > 0
    && element.getBoundingClientRect().height > 0 && !element.matches(':disabled,.is-disabled,[aria-disabled="true"]')
    && !element.closest('[hidden],[aria-hidden="true"]')
}

/** Read a row from the same mounted table that renders it. A row index alone
 * never survives sorting, filtering, replacement or a credential/page change. */
export function boundRecord(element) {
  const row = element?.closest?.('.el-table__body-wrapper tbody > tr')
  const table = row?.closest('.el-table')
  if (!row || !available(element) || !available(row) || table.closest('form,[role="dialog"],dialog')) return undefined
  const binding = pageComponentForRoot(table, 'ElTable')
  // Vue may have queued new rows while callbacks still close over the old row.
  if (!binding || binding.owner.next || binding.instance.next ||
      !Number.isInteger(binding.owner.job?.flags) || !Number.isInteger(binding.instance.job?.flags) ||
      binding.owner.job?.flags & 1 || binding.instance.job?.flags & 1) return undefined
  // The body renders the store's filtered/sorted rows, not the input props.
  // Require the mounted body and the exact store it was rendered with.
  const bodies = [...table.querySelectorAll('.el-table__body-wrapper tbody')]
    .filter(item => item.closest('.el-table') === table)
  const body = bodies.length === 1 && pageComponentForRoot(bodies[0], 'ElTableBody')
  if (!body || body.owner !== binding.owner || body.instance.next ||
      !Number.isInteger(body.instance.job?.flags) || body.instance.job.flags & 1) return undefined
  const store = body.instance.props?.store
  const stores = [binding.instance.store, binding.instance.proxy?.store, binding.instance.setupState?.store].filter(Boolean)
  if (!store || stores.some(item => item !== store)) return undefined
  const data = store.states?.data?.value
  const rows = [...table.querySelectorAll('.el-table__body-wrapper tbody > tr')]
    .filter(item => item.closest('.el-table') === table)
  const index = rows.indexOf(row)
  // Tree/expanded/virtual tables need their own component contract. Never align
  // a DOM row against a differently shaped data array.
  if (!Array.isArray(data) || rows.length !== data.length || index < 0 ||
      rows.some(item => item.matches('.el-table__row[class*="el-table__row--level-"]') ||
        item.querySelector(':scope > .el-table__expanded-cell')) ||
      !Number.isSafeInteger(data[index]?.id) || data[index].id < 1 ||
      data.filter(item => item?.id === data[index].id).length !== 1) return undefined
  const heads = [...table.querySelectorAll('.el-table__header-wrapper thead tr:last-child th')]
  const cells = [...row.children]
  if (heads.length !== cells.length || heads.length > 80) return undefined
  const text = item => (item.textContent || '').replace(/\s+/g, ' ').trim()
  const scalars = Object.values(data[index]).filter(value => ['string', 'number'].includes(typeof value)).map(String)
  const values = heads.flatMap((head, i) => {
    const label = text(head), value = text(cells[i])
    return label && label.length <= 80 && value.length <= 300 &&
      !/password|token|secret|credential|密码|密钥|令牌/i.test(label) &&
      !cells[i].querySelector('button,input,textarea,select') &&
      (!cells[i].querySelector('a') || scalars.includes(value)) ? [[label, value]] : []
  })
  return {id: String(data[index].id), table: String(binding.instance.uid), values}
}

async function boundRangeInput(element, text, invoked) {
  const root = element.closest('.el-date-editor--daterange,.el-date-editor--datetimerange')
  if (!root) return false
  const binding = pageComponentForRoot(root, 'PickerRangeTrigger')
  const inputs = [...root.querySelectorAll(':scope > .el-range-input')]
  const end = inputs.indexOf(element)
  if (!binding || inputs.length !== 2 || end < 0) return false
  let picker = binding.instance.parent
  for (let depth = 0; picker && depth < 12 && picker.type?.name !== 'ElDatePicker'; depth++) picker = picker.parent
  if (picker?.type?.name !== 'ElDatePicker' || !['daterange', 'datetimerange'].includes(picker.props?.type) ||
      !picker.isMounted || picker.isUnmounted || picker.isDeactivated ||
      typeof picker.exposed?.handleOpen !== 'function') return false
  const datetime = picker.props.type === 'datetimerange'
  const parts = /^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}):(\d{2}):(\d{2}))?$/.exec(text)
  if (!parts) throw new Error('the native date range requires a date or full date and time')
  const [year, month, day] = parts.slice(1, 4).map(Number)
  if (new Date(Date.UTC(year, month - 1, day)).toISOString().slice(0, 10) !== text.slice(0, 10) ||
      parts[4] && (!datetime || Number(parts[4]) > 23 || Number(parts[5]) > 59 || Number(parts[6]) > 59)) {
    throw new Error('the native date range refuses this date or time')
  }
  // Opening and selecting are real component callbacks, not synthetic DOM clicks.
  // The range panel owns formatting and real update/change events. Date-only
  // queries on a datetime range mean inclusive natural days, not native defaults.
  try { invoked() } catch { /* Diagnostics cannot stop the component callback. */ }
  picker.exposed.handleOpen()
  await Promise.resolve()
  // The component's opening watcher saves its pre-edit model on nextTick.
  // Let that native baseline settle before pick closes the range again.
  await new Promise(resolve => window.requestAnimationFrame(resolve))
  const tables = liveVNodes(binding.owner).filter(node => {
    if (node.component?.type?.__name !== 'basic-date-table' || !node.component.isMounted ||
        node.component.isUnmounted || node.component.isDeactivated ||
        node.component.props?.selectionMode !== 'range' || typeof node.props?.onPick !== 'function') return false
    let parent = node.component.parent
    for (let depth = 0; parent && depth < 12; depth++, parent = parent.parent) if (parent === picker) return true
    return false
  })
  const handlers = new Set(tables.map(node => node.props.onPick))
  if (!available(element) || element.readOnly || currentPageComponent() !== binding.owner ||
      readElementPageControl(element)?.kind !== 'query' || handlers.size !== 1 || !tables.length) {
    throw new Error('the mounted native date range contract changed after opening')
  }
  const props = tables[0].component.props
  const base = props.date
  if (props.disabled || typeof base?.startOf !== 'function' || typeof base?.toDate !== 'function') {
    throw new Error('the mounted native date range is unavailable')
  }
  let value = base.startOf('day').date(1).year(year).month(month - 1).date(day)
  const time = parts[4] ? parts.slice(4).map(Number) : [end === 1 ? 23 : 0, end === 1 ? 59 : 0, end === 1 ? 59 : 0]
  if (datetime) value = value.hour(time[0]).minute(time[1]).second(time[2])
  if (!value.isValid() || value.format(parts[4] ? 'YYYY-MM-DD HH:mm:ss' : 'YYYY-MM-DD') !== text ||
      props.disabledDate?.(value.toDate())) {
    throw new Error('the native date range refuses this date')
  }
  const selected = {minDate: end === 0 ? value : props.minDate, maxDate: end === 1 ? value : props.maxDate}
  if (!selected.minDate) throw new Error('the native date range has no selected start date')
  if (end === 1 && selected.maxDate.isBefore(selected.minDate)) throw new Error('the native date range ends before it starts')
  // A datetime panel can also have calendar time defaults. Its own time editor
  // callbacks retain the exact query times after calendar selection applies them.
  let timeEditors, panel
  if (datetime && picker.props.defaultTime) {
    panel = tables[0].component.parent
    if (panel?.type?.__name !== 'panel-date-range' ||
        picker.props.format && picker.props.format !== 'YYYY-MM-DD HH:mm:ss' ||
        picker.props.timeFormat && picker.props.timeFormat !== 'HH:mm:ss') {
      throw new Error('the native datetime range time contract is unavailable')
    }
    const wraps = [...panel.subTree.el.querySelectorAll('.el-date-range-picker__time-header .el-date-range-picker__editors-wrap')]
    const nodes = liveVNodes(binding.owner)
    timeEditors = wraps.map(wrap => {
      const root = wrap.querySelectorAll('.el-input')[1]
      const matches = nodes.filter(node => node.el === root && node.component?.type?.name === 'ElInput'
        && node.component.parent === panel && node.component.isMounted &&
        !node.component.isUnmounted && !node.component.isDeactivated)
      return matches.length === 1 ? matches[0] : undefined
    })
    if (timeEditors.length !== 2 || timeEditors.some(editor => typeof editor?.props?.onInput !== 'function' ||
        typeof editor.props?.onChange !== 'function')) {
      throw new Error('the native datetime range time editors are unavailable')
    }
  }
  try { invoked() } catch { /* Diagnostics cannot stop the component callback. */ }
  await tables[0].props.onPick(selected, datetime ? !!timeEditors : true)
  if (timeEditors) {
    for (const [i, date] of [selected.minDate, selected.maxDate].entries()) {
      if (date) {
        if (timeEditors[i].component.props.disabled || timeEditors[i].component.props.readonly ||
            !timeEditors[i].el.isConnected || timeEditors[i].el.closest('[hidden],[aria-hidden="true"]') ||
            currentPageComponent() !== binding.owner) {
          throw new Error('the native datetime range time editor changed after selection')
        }
        try { invoked() } catch { /* Diagnostics cannot stop the component callback. */ }
        await timeEditors[i].component.vnode.props.onInput(date.format('HH:mm:ss'))
      }
    }
    if (selected.maxDate) {
      for (const editor of timeEditors) {
        try { invoked() } catch { /* Diagnostics cannot stop the component callback. */ }
        await editor.component.vnode.props.onChange()
      }
      await Promise.resolve()
      const buttons = [...panel.subTree.el.querySelectorAll('.el-picker-panel__footer button.is-plain')]
      const confirms = liveVNodes(binding.owner).filter(node => node.el === buttons[0] &&
        node.component?.type?.name === 'ElButton' && node.component.parent === panel && node.component.isMounted &&
        !node.component.isUnmounted && !node.component.isDeactivated && !node.component.props.disabled)
      if (buttons.length !== 1 || confirms.length !== 1 || !buttons[0].isConnected ||
          buttons[0].closest('[hidden],[aria-hidden="true"]') ||
          typeof confirms[0].props?.onClick !== 'function' || currentPageComponent() !== binding.owner) {
        throw new Error('the native datetime range confirmation is unavailable')
      }
      try { invoked() } catch { /* Diagnostics cannot stop the component callback. */ }
      await confirms[0].props.onClick()
    }
  }
  await Promise.resolve()
  return true
}

/** Invoke the exact rendered handler, including component update/change hooks.
 * Unknown structures return false and use the existing admitted DOM path. */
export async function boundInput(element, text, invoked = () => {}) {
  if (!available(element) || element.readOnly || readElementPageControl(element)?.kind !== 'query' ||
      !element.matches('input:not([type="password"]):not([type="file"]),textarea')) return false
  if (element.matches('.el-date-editor--daterange > .el-range-input,.el-date-editor--datetimerange > .el-range-input')) {
    return boundRangeInput(element, text, invoked)
  }
  if (element.getAttribute('role') === 'combobox') return false
  const nodes = liveVNodes().filter(node => typeof node.type === 'string' && node.el === element
    && typeof node.props?.onInput === 'function')
  if (nodes.length !== 1) return false
  const node = nodes[0]
  try { invoked() } catch { /* Diagnostic collection is not part of the input. */ }
  element.value = text
  const event = new Event('input', {bubbles: false})
  Object.defineProperties(event, {target: {value: element}, currentTarget: {value: element}})
  await node.props.onInput(event)
  if (typeof node.props.onChange === 'function') await node.props.onChange(event)
  await Promise.resolve()
  return true
}

export async function boundClick(element, invoked = () => {}) {
  if (!available(element)) return false
  const query = readElementPageControl(element)
  const row = boundRecord(element)
  const list = element.closest('[role="listbox"]')
  const owners = list?.id ? [...document.querySelectorAll('input[role="combobox"][aria-controls]')]
    .filter(input => input.getAttribute('aria-controls') === list.id && available(input) &&
      ['query', 'page_size'].includes(readElementPageControl(input)?.kind)) : []
  const family = element.closest('[role="tab"],.el-pagination') || query?.kind === 'query'
    || row && element.matches('button,a') || element.getAttribute('role') === 'option' && owners.length === 1
  // Modal editors, business controls and unclassified structures retain the
  // ordinary path; binding cannot widen their server-side admission.
  if (!family || element.closest('[role="dialog"],dialog')) return false
  const nodes = liveVNodes().filter(node => typeof node.type === 'string' &&
    node.el instanceof HTMLElement && (node.el === element || node.el.contains(element)) &&
    typeof node.props?.onClick === 'function')
    .sort((a, b) => a.el === b.el ? 0 : a.el.contains(b.el) ? 1 : -1)
  if (!nodes.length || nodes.length > 1 && nodes[0].el === nodes[1].el) return false
  const node = nodes[0]
  const event = new MouseEvent('click', {bubbles: false, cancelable: true})
  Object.defineProperties(event, {target: {value: element}, currentTarget: {value: node.el}})
  try { invoked() } catch { /* Diagnostic collection is not part of the click. */ }
  await node.props.onClick(event)
  // A callback may navigate; never replay it when arrival is still pending.
  await Promise.resolve()
  return true
}
