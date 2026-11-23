/** App-owned page facts for Yudao. Load before the application's module entry.
 * Observes same-origin `/admin-api/` GET completion; retains no credentials,
 * bodies or business values except a bounded visible detail title and reviewed
 * scalar applied-query parameters.
 * A navigation fact additionally requires the deployed AppView's live Vue mount.
 * Generic Element Plus component detection/binding lives in `../element-plus/`.
 * This is observation, never the application's API write fence.
 */
import {annotateElDescriptions} from '../element-plus/descriptions.js'
import {annotateActiveDialogElTrees} from '../element-plus/tree.js'
import {
  bindElDateRangeInput,
  isElMonthRangeInput,
  isElRangePickerInput,
  isElRangePopupOpen,
  nativeMonthRangeCell,
  nativeMonthRangeStep,
  nativeRangeDateCell,
  nativeRangeDateStep,
  readElDatePickerPrecision,
} from '../element-plus/date-picker.js'
import {
  bindElClick,
  bindElInput,
  hasVisibleElError,
  hasVisibleElLoading,
  readElFormFacets,
  readElFormModel,
  readElPaginationControl,
  readElPaginationTotals,
  restoreElFormModel,
  sameElFormStructure,
} from '../element-plus/form.js'
import {
  collectElTreeQueryControls,
  findElMenuInstance,
  resolveElControlElement,
} from '../element-plus/select.js'
import {
  annotateElLineControls,
  annotateObservedElTableRows,
  boundElTableRecord,
  collectElFormLineControls,
  collectVisibleElTableData,
  readElTableRowToggle,
} from '../element-plus/table.js'
import {
  findOwnedElComponent,
  isElVisible,
  isLiveComponent,
} from '../element-plus/vue.js'

const PAGE_LAYERS = '.el-overlay, .el-image-viewer__wrapper'
// Only layers carried across an assistant navigation block its destination. A form
// opened on the destination itself must remain observable for prefill and handoff.
const carriedLayers = new WeakMap()
const recordDetailRoute = route => /^\/(crm\/(?:customer|contract|business|contact|clue|product)|hrm\/employee)\/detail\/([0-9]+)$/.exec(route)
const genericDetailRoute = route => /^\/([a-z0-9-]+(?:\/[a-z0-9-]+)*)\/detail\/([0-9]+)$/i.exec(route)
const SIMPLE_LOOKUP_PATH = /(?:\/|-)(?:simple-list|list-all-simple|simple)$/i
const PERMISSION_TOAST_TEXT = /^(?:没有该操作权限|无权限|权限不足)[!！。.\s]*$/
const FORM_ID = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}$/

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

const BOUNDED_QUERY_KEY = /^[A-Za-z][A-Za-z0-9_]*(?:\[[0-9]{1,3}\])?$/
const SENSITIVE_QUERY_KEY = /(?:pass(?:word)?|pwd|secret|token|credential|auth|cookie|session|(?:^|[_-])key$|(?:api|access|private|secret|app|encrypt|sign)key)/i
const LIST_READ_PATH = /(?:\/|[a-z0-9]-)(?:page|list)(?:-[a-z0-9-]+)?$/
const isListReadPath = pathname => LIST_READ_PATH.test(pathname) && !SIMPLE_LOOKUP_PATH.test(pathname)

function boundedListQuery(url) {
  if (!isListReadPath(url.pathname)) return null
  const pairs = Array.from(url.searchParams)
  const effectivePairs = pairs.length > 16
    ? pairs.filter(([key, value]) => value !== '' || key === 'pageNo' || key === 'pageSize')
    : pairs
  if (effectivePairs.length > 24 || new Set(pairs.map(([key]) => key)).size !== pairs.length) return null
  const parameters = {}
  for (const [key, value] of effectivePairs) {
    if (!BOUNDED_QUERY_KEY.test(key) || key.length > 64 || SENSITIVE_QUERY_KEY.test(key)) return null
    if (typeof value !== 'string' || value.length > 128 ||
        Array.from(value).some(c => c.charCodeAt(0) <= 31 || c.charCodeAt(0) === 127)) return null
    parameters[key] = value
  }
  return {path: url.pathname, parameters}
}

// Retain a digest and paging numbers, never arbitrary query values.
function listReadQuery(url, view) {
  const pairs = Array.from(url.searchParams)
  const page = url.searchParams.get('pageNo'), size = url.searchParams.get('pageSize')
  if (!isListReadPath(url.pathname) || pairs.length > 32 ||
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
  // Bind the nearest rendered AppView under the current route's mounted parent.
  // Do not exhaustively scan menus or descend into the page's thousands of VNodes.
  const parent = active.matched.slice(0, -1).reverse().map(item => item.instances?.default?.$)
    .find(item => isLiveComponent(item) && item.appContext?.app === app)
  const ancestors = [parent ? parent.subTree : root], visited = new Set()
  let appView
  while (ancestors.length && visited.size < 256) {
    const node = ancestors.shift()
    if (!node || visited.has(node)) continue
    visited.add(node)
    if (node.suspense?.pendingBranch) return undefined
    if (node.component) {
      const item = node.component
      if (!isLiveComponent(item)) continue
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
      if (!isLiveComponent(item)) continue
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

/** Flatten nested `children` rows (for hierarchical tables) up to a bounded node count. */
function flattenTableRows(data) {
  const out = []
  const stack = [...data].reverse()
  while (stack.length && out.length < 4096) {
    const row = stack.pop()
    if (!row || typeof row !== 'object') continue
    out.push(row)
    if (Array.isArray(row.children) && row.children.length) {
      for (let i = row.children.length - 1; i >= 0; i--) stack.push(row.children[i])
    }
  }
  return stack.length ? null : out
}

/** Whether a table's own records are this list response's rows. Without record IDs,
 * the row count and the page's one visible total must agree instead (or an unpaged
 * array list when no pagination total is rendered). */
function rendersList(list, data, totals) {
  let rowsData = data
  if (list.rows.ids && data.length !== list.rows.count &&
      data.some(row => Array.isArray(row?.children) && row.children.length > 0)) {
    const flat = flattenTableRows(data)
    if (flat) rowsData = flat
  }
  if (rowsData.length !== list.rows.count) return false
  if (!list.rows.ids) {
    if (list.unpaged && totals.length === 0) return true
    return totals.length === 1 && totals[0] === list.total
  }
  const shown = rowsData.map(row => row?.id === undefined || row?.id === null ? null : String(row.id))
  return shown.every(id => id !== null) && shown.sort().join('\n') === [...list.rows.ids].sort().join('\n')
}

export function createPageFacts({view = window, doc = document} = {}) {
  let route = '', locationKey = '', generation = 0, record = null, appliedLedger = null, listSerial = 0, disposed = false
  const reads = new Map(), formReads = new Map()
  // The latest list request per endpoint. A list is the page's own query only while a
  // visible table of the page renders its rows (pageList): a cached page refreshing its
  // list on every route change, or another list on the page, never stands in for it.
  const lists = new Map()
  function current() {
    const next = view.location.pathname + view.location.search + view.location.hash
    if (next !== locationKey) {
      locationKey = next; route = view.location.pathname; generation++; record = null; appliedLedger = null
      reads.clear(); lists.clear(); formReads.clear()
    }
    return generation
  }
  function pageList() {
    const tables = collectVisibleElTableData(doc, pageComponentForRoot)
    if (!tables.length || tables.length > 8) return undefined
    const totals = readElPaginationTotals(doc)
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
    if (/\/get(?:-[a-z0-9-]+)?$/.test(url.pathname)) formReads.delete(url.pathname)
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
    } else if (isListReadPath(url.pathname) && route && route.indexOf('/detail/') < 0) {
      // Any other collection list read may be the page's applied query once a page
      // table renders it. Scalar projection stays bounded and non-sensitive.
      query = boundedListQuery(url)
    }
    // A newer request to an endpoint replaces its earlier list at send, failures included.
    let list = null
    if (isListReadPath(url.pathname)) {
      list = {serial: ++listSerial, path: url.pathname, query: null, read: null, rows: null, total: null, unpaged: false}
      lists.delete(url.pathname)
      if (lists.size >= 16) lists.delete(lists.keys().next().value)
      lists.set(url.pathname, list)
    }
    const detail = recordDetailRoute(route)
    const genericDetail = genericDetailRoute(route)
    const isRecord = Boolean(detail && url.pathname === `/admin-api/${detail[1]}/get`)
    const isPrimaryDetail = Boolean(isRecord || (genericDetail && url.pathname === `/admin-api/${genericDetail[1]}/get`))
    const ids = url.searchParams.getAll('id')
    const matchesRecord = !isRecord || ids.length === 1 && ids[0] === detail[2]
    const selected = Array.from(doc.querySelectorAll('[role="tab"][aria-selected="true"]'))
      .filter(e => isElVisible(e) && !e.closest('[role="dialog"],dialog'))
    const scoped = Boolean(detail && url.pathname.startsWith(`/admin-api/${detail[1].split('/')[0]}/`))
    const panel = scoped && selected.length === 1 ? selected[0].getAttribute('aria-controls') || '' : ''
    const readPanel = isRecord || !scoped ? '*' : panel
    const key = readPanel + '\n' + url.pathname
    // A bounded projection fails closed rather than dropping unfinished/error reads.
    if (!reads.has(key) && reads.size >= 64) { reads.set('overflow', {pending: 0, failed: true, panel: '*'}); return null }
    const prior = reads.get(key)
    const state = {pending: (prior?.pending || 0) + 1, failed: prior?.failed || false,
      auxForbidden: prior?.auxForbidden || false,
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
      if (/\/get(?:-[a-z0-9-]+)?$/.test(url.pathname)) {
        formReads.delete(url.pathname)
        if (ok && body?.data?.id !== undefined && String(body.data.id) === url.searchParams.get('id')) {
          if (formReads.size >= 32) formReads.delete(formReads.keys().next().value)
          formReads.set(url.pathname, String(body.data.id))
        }
      }
      latest.failed = !ok
      const isRbacForbidden = matchesRecord && !isPrimaryDetail && body?.error !== 'page_write_blocked'
        && (status === 403 || (status >= 200 && status < 300 && body?.code === 403))
      latest.auxForbidden = Boolean(!ok && isRbacForbidden
        && (SIMPLE_LOOKUP_PATH.test(url.pathname) || (genericDetail && (!detail || !scoped))))
      if (ledger && ok && query && Array.isArray(body?.data)) appliedLedger = query
      const records = Array.isArray(body?.data?.list) ? body.data.list : Array.isArray(body?.data) ? body.data : null
      if (list && lists.get(url.pathname) === list && ok && records) {
        list.rows = listRows(records)
        list.total = Number.isSafeInteger(body.data.total) ? body.data.total : null
        list.unpaged = Array.isArray(body.data)
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
  function primaryReady() {
    if (recordDetailRoute(route)) return Boolean(record && record.route === route)
    const gd = genericDetailRoute(route)
    if (gd) return formReads.get(`/admin-api/${gd[1]}/get`) === gd[2]
    return Boolean(appliedLedger || pageList())
  }
  function hasBlockingElError(allowPermissionToast) {
    if (!hasVisibleElError(doc)) return false
    if (!allowPermissionToast) return true
    const errors = Array.from(doc.querySelectorAll('.el-message--error, .el-notification--error')).filter(isElVisible)
    return errors.some(el => !PERMISSION_TOAST_TEXT.test((el.textContent || '').trim()))
  }
  function readPageBlocker() {
    current()
    if (disposed) return true
    if (carriedLayerBlocks(doc)) return true
    const selected = Array.from(doc.querySelectorAll('[role="tab"][aria-selected="true"]'))
      .filter(e => isElVisible(e) && !e.closest('[role="dialog"],dialog'))
    const panel = selected[0]?.getAttribute('aria-controls') || ''
    const relevant = Array.from(reads.values()).filter(s => s.panel === '*' || !s.panel || s.panel === panel)
    const canExemptAux = primaryReady()
    if (relevant.some(s => s.conflicts || (s.failed && !(canExemptAux && s.auxForbidden)))) return true
    const hasAuxFailure = canExemptAux && relevant.some(s => s.failed && s.auxForbidden)
    if (hasBlockingElError(hasAuxFailure)) return true
    if (relevant.some(s => s.pending)) return 'pending'
    const loading = hasVisibleElLoading(doc)
    if (!recordDetailRoute(route)) return loading ? 'pending' : false
    const titles = Array.from(doc.querySelectorAll('.text-xl.font-bold')).filter(isElVisible)
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
  function readFormSource(id) {
    current()
    if (disposed) return undefined
    if (!id) return pageList()?.path
    const paths = [...formReads].filter(([, held]) => held === String(id)).map(([path]) => path)
    return paths.length === 1 ? paths[0] : undefined
  }
  function readRowSource(element) {
    current()
    const table = element?.closest?.('.el-table')
    const data = table && pageComponentForRoot(table, 'ElTable')?.instance?.props?.data
    if (disposed || !isElVisible(table) || !Array.isArray(data) || readPageBlocker()) return undefined
    const matching = Array.from(lists.values()).filter(list => list.rows?.ids && rendersList(list, data, []))
    return matching.length === 1 ? matching[0].path : undefined
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
      const name = item => item?.type?.name || item?.type?.__name || ''
      const errorComponent = item => /^(Error(?:403|404|500)?|NoFound|NoAccess)$/.test(name(item))
      if (!isLiveComponent(instance) || !component || instance.type !== component
          || instance.vnode?.type !== component || instance.vnode?.component !== instance
          || instance.appContext?.app !== app || errorComponent(instance)
          // The reviewed AppView keys its destination by fullPath unless a
          // custom viewKey overrides it. An unrelated key cannot prove arrival.
          || instance.vnode.key !== active.fullPath) return undefined
      let routerView, appView, ancestor = instance.parent
      for (let depth = 0; ancestor && depth < 24; depth++, ancestor = ancestor.parent) {
        if (!isLiveComponent(ancestor) || errorComponent(ancestor)) return undefined
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
      const sections = roots.filter(node => node?.type === 'section' && node.el?.isConnected && isElVisible(node.el))
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
            (!isLiveComponent(node.component) || errorComponent(node.component))) return undefined
        if (node.el?.nodeType === 1 && node.el.isConnected && main.contains(node.el) && isElVisible(node.el)) content = true
        if (node.component) render.push(node.component.subTree)
        if (Array.isArray(node.children)) {
          if (node.children.length + render.length > renderLimit) return undefined
          render.push(...node.children)
        }
      }
      if (render.length || !content || hasVisibleElLoading(doc) || readPageBlocker()) return undefined
      return route
    } catch { return undefined }
  }
  function readPageFacets() {
    if (disposed || readPageBlocker() || !(appliedLedger || pageList()?.query)) return []
    return readElFormFacets(doc, isElVisible)
  }
  return {begin, readPageBlocker, readPageRecordRoute, readPageRoute, readPageQuery, readPageRead,
    readPageFacets, readRowSource, readFormSource,
    dispose() {disposed = true; reads.clear(); lists.clear(); formReads.clear(); record = null; appliedLedger = null}}
}

/** What this page's own API requests carry while the assistant is working it. */
export const ASSISTANT_HEADER = 'X-WOA-Assistant'
/** Which of the actor's guard windows a marked request belongs to. */
export const WINDOW_HEADER = 'X-WOA-Window'
/** The window names the guard accepts (`WINDOW_KEY` in page_guard.py). */
const WINDOW_KEY = /^[A-Za-z0-9._-]{1,128}$/

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
  // The window name travels with the mark so one page's guard work is never another's.
  let assistantUntil = 0, assistantWindow = ''
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
    if (step === 'end') {
      // Only an end of this page's own window lifts the mark; an end another window
      // asked for, or one that cannot be read, leaves it -- and an unreadable body
      // may be ours, in which case not lifting is the unsafe direction.
      let ending = ''
      try { ending = JSON.parse(init?.body || '{}')?.window ?? '' } catch { ending = null }
      if (ending === null || ending === assistantWindow) {
        assistantUntil = 0; assistantWindow = ''
      }
    }
    const settle = facts.begin(url, init?.method || input?.method || 'GET')
    if (!step && assistantWorking()) {
      try {
        const target = new URL(url, view.location.href)
        if (target.origin === view.location.origin && target.pathname.startsWith('/admin-api/')) {
          const headers = new view.Headers(init?.headers || (typeof input === 'object' && input.headers) || {})
          headers.set(ASSISTANT_HEADER, '1')
          if (assistantWindow) headers.set(WINDOW_HEADER, assistantWindow)
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
        assistantWindow = assistantUntil && typeof window_.window === 'string'
            && WINDOW_KEY.test(window_.window) ? window_.window : ''
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
        const isList = isListReadPath(target.pathname)
        // Retain only a bounded scalar projection; open alone starts no read.
        const query = ledger ? ledgerQuery(target) : order ? orderQuery(target) : creation ? creationRangeQuery(target)
          : isList ? boundedListQuery(target) : null
        requests.set(this, {method, path: target.pathname, ledger, order, creation, isList, query,
          readQuery: listReadQuery(target, view),
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
      } else if (request.isList) {
        if (!request.query) target.searchParams.set('_invalidQuery', '1')
        else {
          for (const [key, value] of Object.entries(request.query.parameters || {})) {
            target.searchParams.set(key, value)
          }
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
      try {
        this.setRequestHeader(ASSISTANT_HEADER, '1')
        if (assistantWindow) this.setRequestHeader(WINDOW_HEADER, assistantWindow)
      } catch { /* the request is already sent */ }
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

const OPEN_WAIT_MS = 6000

function shownMatchesWanted(shownFullPath, wanted, origin) {
  if (!shownFullPath) return false
  if (shownFullPath === wanted.fullPath) return true
  try {
    const cur = new URL(shownFullPath, origin)
    const req = new URL(wanted.fullPath, origin)
    if (cur.pathname !== req.pathname) return false
    for (const key of new Set(req.searchParams.keys())) {
      const a = cur.searchParams.getAll(key), b = req.searchParams.getAll(key)
      if (a.length !== b.length || a.some((v, i) => v !== b[i])) return false
    }
    return true
  } catch {
    return false
  }
}

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
  try {
    wanted = router.resolve(target.pathname + target.search)
    between = router.resolve({path: '/redirect' + wanted.path, query: wanted.query})
  } catch { return false }
  if (!wanted?.matched?.length) return false
  const deadline = Date.now() + OPEN_WAIT_MS
  if (wanted.fullPath === shown()) {
    try {
      while (shown() === wanted.fullPath && !mounted() && Date.now() < deadline) {
        await new Promise(resolve => view.setTimeout(resolve, 25))
      }
    } catch { /* next observation verifies readiness */ }
    return true
  }
  const layers = Array.from(doc.querySelectorAll(PAGE_LAYERS)).filter(element => {
    const box = element.getBoundingClientRect()
    return box.width && box.height
  })
  const pushTarget = between?.name === 'Redirect' ? between : wanted
  try {
    tabs.delAllViews()
    await new Promise(resolve => view.setTimeout(resolve, 0))
    if (await router.push(pushTarget)) return false
    while (!shownMatchesWanted(shown(), wanted, view.location.origin)) {
      if (Date.now() > deadline) return false
      await new Promise(resolve => view.setTimeout(resolve, 25))
    }
  } catch { return false }
  const currentShown = shown()
  if (!shownMatchesWanted(currentShown, wanted, view.location.origin) ||
      view.location.pathname + view.location.search !== currentShown) return false
  if (layers.length) carriedLayers.set(doc, new Set([...(carriedLayers.get(doc) || []), ...layers]))
  try {
    while (shownMatchesWanted(shown(), wanted, view.location.origin) && !mounted() && Date.now() < deadline) {
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
  const app = document.getElementById('app')?.__vue_app__
  const instance = findElMenuInstance(root, app?._container?._vnode)
  if (!instance) return undefined
  const kind = instance.type.name === 'ElSubMenu' ? 'branch' : 'page'
  const title = kind === 'branch' ? root.querySelector(':scope > .el-sub-menu__title') : root
  if (!title || (element !== root && element !== title)) return undefined
  if (root.getAttribute('role') !== 'menuitem') return undefined
  const path = instance.props?.index
  if (typeof path !== 'string' || path.length > 500 || !/^\/(?!\/)[^?#\\\s]*$/.test(path)) return undefined
  const seen = new Set()
  let owner = instance.parent
  while (owner && seen.size < 48) {
    if (!isLiveComponent(owner) || owner.appContext !== instance.appContext || seen.has(owner)) return undefined
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
      !isLiveComponent(instance) ||
      instance.appContext?.app !== app || instance.type !== matched.components?.default ||
      instance.vnode?.component !== instance || instance.vnode?.key !== active.fullPath) return undefined
  return instance
}

/** Bind a real Element Plus component root to the active page's mounted VNode tree. */
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
      && !(allowDialog && root.closest('[role="dialog"],dialog,.el-popover'))) return undefined
  return findOwnedElComponent({owner, root, componentName: name, allowDialog})
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
  const precision = readElDatePickerPrecision(element, form, pageComponentForRoot, bound.owner)
  if (title === '新增' && (id === undefined || id === null || id === '')) {
    return {kind: 'form', mode: 'create', recordId: null, ...precision}
  }
  if (title === '编辑' && (typeof id === 'number' && Number.isSafeInteger(id) && id > 0
      || typeof id === 'string' && /^[1-9][0-9]{0,19}$/.test(id))) {
    return {kind: 'form', mode: 'update', recordId: String(id), ...precision}
  }
  return undefined
}

export function readElementFormModel(element) {
  return readElFormModel(element, {
    findComponent: pageComponentForRoot,
    readSource: id => installed?.readFormSource(id),
  })
}

export {sameElFormStructure as sameFormStructure}

export async function restoreElementFormModel(element, request) {
  return restoreElFormModel(element, request, {
    findComponent: pageComponentForRoot,
    readModel: readElementFormModel,
  })
}

/** The table row a row control belongs to, read from the table that renders it. */
export function readElementRowRecord(element) {
  const row = element instanceof HTMLElement ? boundRecord(element) : undefined
  if (!row || !FORM_ID.test(row.id)) return undefined
  const named = row.values.find(([label]) => /名称|名字|标题|姓名|name/i.test(label))
  return {
    id: row.id,
    name: named ? named[1].slice(0, 200) : '',
    values: row.values.slice(0, 40),
    source: installed?.readRowSource(element),
    toggle: readElTableRowToggle(element, row, pageComponentForRoot),
  }
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
        && wrapper.vnode?.ctx === tree.owner && isLiveComponent(wrapper)
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
        model.pageNo < 1 || !Number.isSafeInteger(model.pageSize) || model.pageSize < 1) {
      if (form.classList.contains('el-form--inline')) {
        const inlineBinding = binding ?? pageComponentForRoot(form, 'ElForm', true)
        const inlineModel = inlineBinding?.instance.props?.model
        if (inlineBinding && (inlineModel === undefined || inlineModel === null)) {
          return {kind: 'query'}
        }
      }
      return undefined
    }
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
  const popover = element.closest('.el-popover')
  if (popover && !popover.closest('[role="dialog"],dialog')) {
    const owner = currentPageComponent()
    if (!owner) return undefined
    const itemRoot = element.closest('.el-form-item')
    const anchorTarget = itemRoot
      ? pageComponentForRoot(itemRoot, 'ElFormItem', true)
      : element.matches('button,a,[role="button"]')
        ? pageComponentForRoot(element, 'ElButton', true)
        : undefined
    let formInst
    for (let cur = anchorTarget?.instance?.parent, depth = 0; cur && depth < 24; depth++, cur = cur.parent) {
      if (cur.appContext !== owner.appContext || !isLiveComponent(cur)) break
      if ((cur.type?.name || cur.type?.__name) === 'ElForm') {
        formInst = cur
        break
      }
    }
    const formEl = formInst?.subTree?.el
    const formBinding = formEl instanceof HTMLElement ? pageComponentForRoot(formEl, 'ElForm') : undefined
    const model = formBinding?.instance?.props?.model
    if (!formBinding || formBinding.owned.length !== 1 || formBinding.owned[0] !== formInst ||
        !model || typeof model !== 'object' || !Number.isSafeInteger(model.pageNo) ||
        model.pageNo < 1 || !Number.isSafeInteger(model.pageSize) || model.pageSize < 1) {
      return undefined
    }
    const item = itemRoot && anchorTarget?.instance
    const field = item?.props?.prop
    const value = typeof field === 'string' ? model[field] : undefined
    const choice = element.closest('.el-select') &&
      (typeof value === 'boolean' || Number.isSafeInteger(value) && value >= 0 ||
        typeof value === 'string' && /^(?:[0-9]{1,20}|true|false)$/.test(value))
    return {kind: 'query', ...(typeof field === 'string' ? {field} : {}),
      ...(choice ? {queryValue: String(value)} : {})}
  }
  return readElPaginationControl(element, pageComponentForRoot)
}

export function withTreeQueryControls(selectorMap, html, root = document) {
  return collectElTreeQueryControls(selectorMap, html, root, readElementPageControl)
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
  // Other native scalar switches expose their mounted column property and exact values.
  for (const area of root.querySelectorAll('.el-table .el-switch')) {
    if (lines.length >= 32) break
    if ([...selectorMap.values()].some(value => value.ref === area)) continue
    const rect = area.getBoundingClientRect()
    if (!rect.width || !rect.height || rect.bottom <= 0 || rect.top >= window.innerHeight) continue
    const row = boundRecord(area), toggle = row && readElTableRowToggle(area, row, pageComponentForRoot)
    if (!toggle) continue
    const label = `${toggle.label}开关：编号 ${row.id}`
    const index = ++top
    selectorMap.set(index, {ref: area, tagName: 'div', isInteractive: true, highlightIndex: index})
    lines.push(`[${index}]<div role=switch aria-label="${label}" aria-checked=${toggle.checked}>${label} />`)
  }
  return lines.length ? lines.join('\n') + '\n' + html : html
}

export function nativeControlElement(element) {
  return resolveElControlElement(element, {
    isQueryLabel: node => readElementPageControl(node)?.kind === 'query',
    isBranchMenu: node => readElementNavigation(node)?.kind === 'branch',
  })
}

export {
  collectElFormLineControls as lineControls,
  isElMonthRangeInput,
  isElRangePickerInput,
  isElRangePopupOpen,
  nativeMonthRangeCell,
  nativeMonthRangeStep,
  nativeRangeDateCell,
  nativeRangeDateStep,
}

export function withLineControls(selectorMap, html, lines) {
  return annotateElLineControls(selectorMap, html, lines,
    line => `明细第${line.row}行·${line.column}`.replace(/\s+/g, ''))
}

export function withObservedRowColumns(html, root = document, selectorMap = new Map()) {
  return annotateObservedElTableRows(html, root, selectorMap, {
    formatRow: (rowIndex, columns) => `第${rowIndex + 1}行 ${columns.join('；')}`,
    formatSummary: facts => '可见列表行（每行的表头和值）: ' + facts.join(' | ').slice(0, 3000),
  })
}

export function withObservedSurfaceDetails(html, root = document) {
  let next = annotateElDescriptions(html, root, {
    formatSummary: pairs => '可见详情字段: ' + pairs.map(([k, v]) => `${k}=${v}`).join('；').slice(0, 2400),
  })
  next = annotateActiveDialogElTrees(next, root, {
    findComponent: pageComponentForRoot,
    formatTree: tree => {
      const prefix = tree.label ? `「${tree.label}」` : ''
      const parts = []
      if (tree.checked.length) {
        const truncated = tree.checkedCount > tree.checked.length
          ? `（展示前${tree.checked.length}项，完整列表请展开或滚动核对）` : ''
        parts.push(`弹窗树已勾选${prefix}（共${tree.checkedCount}项）: ${tree.checked.join('、')}${truncated}`)
      }
      if (tree.halfChecked.length) {
        parts.push(`弹窗树半选父节点${prefix}（共${tree.halfCheckedCount}项）: ${tree.halfChecked.join('、')}`)
      }
      return parts.join(' | ')
    },
  })
  return next
}

export function boundRecord(element) {
  return boundElTableRecord(element, pageComponentForRoot)
}

export async function boundInput(element, text, invoked = () => {}) {
  return bindElInput(element, text, invoked, {
    readControl: readElementPageControl,
    bindRangeInput: (el, value, cb) => bindElDateRangeInput(el, value, cb, {
      findComponent: pageComponentForRoot,
      isAllowedControl: node => readElementPageControl(node)?.kind === 'query',
      currentPageOwner: currentPageComponent,
    }),
    currentPageOwner: currentPageComponent,
  })
}

export async function boundClick(element, invoked = () => {}) {
  return bindElClick(element, invoked, {
    readControl: readElementPageControl,
    readRow: boundRecord,
    currentPageOwner: currentPageComponent,
  })
}
