import {afterAll, beforeEach, describe, expect, it} from 'vitest'
import {createHash} from 'node:crypto'
// @ts-expect-error Framework-free application module is served separately from the Runtime.
import {createPageFacts, installPageFacts, disposeInstalledPageFacts, nativeRangeDateCell, readElementPageControl} from '../src/integrations/yudao/page-facts.js'

afterAll(() => disposeInstalledPageFacts())
beforeEach(() => {
  window.history.replaceState({}, '', '/crm/customer/detail/6')
  document.body.innerHTML = '<span class="text-xl font-bold">Synthetic Customer</span>'
    + '<div role="tab" aria-selected="true" aria-controls="panel-basic">基本信息</div>'
    + '<div role="tabpanel" id="panel-basic"></div>'
  for (const element of document.querySelectorAll('*')) {
    element.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
  }
})
function ready(facts: ReturnType<typeof createPageFacts>) {
  facts.begin('/admin-api/crm/customer/get?id=6')!(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
}

describe('native Yudao record and read completion facts', () => {
  it('reports order filters only after the latest successful application read', async () => {
    mountedPage('/erp/sale/order', 'SaleOrder')
    const facts = createPageFacts()
    const url = '/admin-api/erp/sale-order/page?pageNo=1&pageSize=10&customerId=7&status=0'
      + '&orderTime%5B0%5D=2026-10-01%2000%3A00%3A00&orderTime%5B1%5D=2026-10-31%2023%3A59%3A59'
    const old = facts.begin(url)!
    expect(facts.readPageQuery()).toBeUndefined()
    const latest = facts.begin(url.replace('status=0', 'status=20'))!
    old(200, {code:0,data:{list:[],total:0}})
    expect(facts.readPageQuery()).toBeUndefined()
    await latest(200, {code:0,data:{list:[],total:0}})
    expect(facts.readPageQuery()).toEqual({path:'/admin-api/erp/sale-order/page', parameters:{
      customerId:'7',status:'20',orderTimeStart:'2026-10-01 00:00:00',orderTimeEnd:'2026-10-31 23:59:59'}})
    facts.begin(url + '&remark=private')!(200,{code:0,data:{list:[]}})
    expect(facts.readPageQuery()).toBeUndefined()
    facts.begin(url)!(403,null)
    expect(facts.readPageQuery()).toBeUndefined()
  })
  it('binds any standard list read to query digests and paging without retaining raw values', async () => {
    mountedPage('/erp/sale/order', 'SaleOrder')
    const facts = createPageFacts()
    const url = '/admin-api/erp/sale-order/page?pageNo=2&pageSize=10&no=SO-SYNTHETIC&remark=synthetic'
    const finish = facts.begin(url)!
    expect(facts.readPageRead()).toBeUndefined()
    await finish(200, {code:0,data:{list:[{id:1}],total:25}})
    const hash = (value: unknown) => createHash('sha256').update(JSON.stringify(value)).digest('hex')
    expect(facts.readPageRead()).toEqual({path:'/admin-api/erp/sale-order/page', page:2,
      page_size:10,total:25,rows:1,revision:1,
      query_digest:hash([['no','SO-SYNTHETIC'],['remark','synthetic']]),
      query_fields:{no:hash(['SO-SYNTHETIC']),remark:hash(['synthetic'])}})
    expect(JSON.stringify(facts.readPageRead())).not.toContain('SO-SYNTHETIC')
    const next = facts.begin(url.replace('pageNo=2','pageNo=3'))!
    expect(facts.readPageRead()).toBeUndefined()
    await next(200,{code:0,data:{list:[],total:25}})
    expect(facts.readPageRead()?.revision).toBe(2)
    expect(facts.readPageRead()?.query_digest).toBe(hash([['no','SO-SYNTHETIC'],['remark','synthetic']]))
    const failure = facts.begin(url)!
    failure(200,{code:500,data:{list:[],total:25}})
    expect(facts.readPageRead()).toBeUndefined()
  })
  it('settles the current pending count when another list read starts before hashing completes', async () => {
    mountedPage('/erp/sale/order', 'SaleOrder')
    const facts = createPageFacts()
    const first = facts.begin('/admin-api/erp/sale-order/page?pageNo=1&pageSize=10')!
    const oldDigest = first(200, {code:0,data:{list:[],total:0}})
    const next = facts.begin('/admin-api/erp/sale-order/page?pageNo=2&pageSize=10')!
    const latestDigest = next(200, {code:0,data:{list:[],total:0}})
    await Promise.all([oldDigest, latestDigest])
    expect(facts.readPageBlocker()).toBe(false)
    expect(facts.readPageRead()).toMatchObject({revision:2,page:2,total:0,rows:0})
  })
  it('waits before the cold route starts any read or paints tabs, without granting identity', () => {
    const facts = createPageFacts()
    document.body.innerHTML = ''
    expect(facts.readPageBlocker()).toBe('pending')
    expect(facts.readPageRecordRoute()).toBeUndefined()
    facts.begin('/admin-api/crm/customer/get?id=6')!(403, null)
    expect(facts.readPageBlocker()).toBe(true)
    expect(facts.readPageRecordRoute()).toBeUndefined()
  })
  it('waits for a cold detail title while loading without granting identity or hiding read failure', () => {
    const facts = createPageFacts()
    const title = document.querySelector('span')!
    title.textContent = ''
    title.getBoundingClientRect = () => ({width: 0, height: 0} as DOMRect)
    const mask = document.createElement('div')
    mask.className = 'el-loading-mask'
    mask.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    document.body.append(mask)
    const settle = facts.begin('/admin-api/crm/customer/get?id=6')!
    expect(facts.readPageBlocker()).toBe('pending')
    expect(facts.readPageRecordRoute()).toBeUndefined()
    settle(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
    expect(facts.readPageBlocker()).toBe('pending')
    title.textContent = 'Synthetic Customer'
    title.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    mask.remove()
    expect(facts.readPageBlocker()).toBe(false)
    expect(facts.readPageRecordRoute()).toBe('/crm/customer/detail/6')
    document.body.append(mask)
    facts.begin('/admin-api/crm/customer/get?id=6')!(200, {code: 1020007001})
    expect(facts.readPageBlocker()).toBe(true)
    expect(facts.readPageRecordRoute()).toBeUndefined()
  })
  it('requires a successful exact-ID read and matching visible title', () => {
    const facts = createPageFacts()
    expect(facts.readPageBlocker()).toBe('pending')
    ready(facts)
    expect(facts.readPageRecordRoute()).toBe('/crm/customer/detail/6')
    document.querySelector('span')!.textContent = 'Another customer'
    expect(facts.readPageBlocker()).toBe(true)
    expect(facts.readPageRecordRoute()).toBeUndefined()
  })
  it('never converts a business permission error in HTTP 200 into a loaded record', () => {
    const facts = createPageFacts()
    facts.begin('/admin-api/crm/customer/get?id=6')!(200, {code: 1020007001})
    expect(facts.readPageBlocker()).toBe(true)
    expect(facts.readPageRecordRoute()).toBeUndefined()
  })
  it('binds a response to the requested record, and rejects missing or other IDs', () => {
    for (const data of [{id: 7, name: 'Synthetic Customer'}, {name: 'Synthetic Customer'},
      {id: true, name: 'Synthetic Customer'}, {id: 6, name: ''}]) {
      const facts = createPageFacts()
      facts.begin('/admin-api/crm/customer/get?id=6')!(200, {code: 0, data})
      expect(facts.readPageRecordRoute()).toBeUndefined()
    }
  })
  it('invalidates the current record when another record is read on the same route', () => {
    for (const url of ['/admin-api/crm/customer/get?id=7',
      '/admin-api/crm/customer/get', '/admin-api/crm/customer/get?id=6&id=7',
      '/admin-api/crm/customer/get?id=6&id=6']) {
      const facts = createPageFacts(); ready(facts)
      const settle = facts.begin(url)!
      expect(facts.readPageBlocker()).toBe(true)
      expect(facts.readPageRecordRoute()).toBeUndefined()
      // Even a response carrying the displayed record cannot repair a conflicting request.
      settle(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
      expect(facts.readPageRecordRoute()).toBeUndefined()
      ready(facts)
      expect(facts.readPageRecordRoute()).toBe('/crm/customer/detail/6')
    }
  })
  it('lets the latest exact read settle only after concurrent conflicting reads finish', () => {
    const facts = createPageFacts(); ready(facts)
    const wrong = facts.begin('/admin-api/crm/customer/get?id=7')!
    const latest = facts.begin('/admin-api/crm/customer/get?id=6')!
    latest(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
    expect(facts.readPageRecordRoute()).toBeUndefined()
    wrong(200, {code: 0, data: {id: 7, name: 'Other Customer'}})
    expect(facts.readPageRecordRoute()).toBe('/crm/customer/detail/6')
    const old = facts.begin('/admin-api/crm/customer/get?id=6')!
    const conflict = facts.begin('/admin-api/crm/customer/get?id=7')!
    conflict(200, {code: 0, data: {id: 7, name: 'Other Customer'}})
    old(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
    expect(facts.readPageRecordRoute()).toBeUndefined()
  })
  it('does not let XHR normalization turn duplicate IDs into an exact read', () => {
    class Request extends EventTarget {
      responseType = 'json'
      response = {code: 0, data: {id: 6, name: 'Synthetic Customer'}}
      status = 200
      open() {}
      send() { this.dispatchEvent(new Event('loadend')) }
    }
    const view = {location: window.location, fetch: window.fetch, XMLHttpRequest: Request}
    const originalOpen = Request.prototype.open
    const facts = installPageFacts(view, document)
    try {
      for (const query of ['id=6&id=7', 'id=6&id=6', 'id=7', '']) {
        ready(facts)
        const request = new Request()
        // @ts-expect-error The fake receives browser arguments through the installed wrapper.
        request.open('GET', '/admin-api/crm/customer/get?' + query)
        request.send()
        expect(facts.readPageRecordRoute()).toBeUndefined()
      }
      const exact = new Request()
      // @ts-expect-error The fake receives browser arguments through the installed wrapper.
      exact.open('GET', '/admin-api/crm/customer/get?id=6')
      exact.send()
      expect(facts.readPageRecordRoute()).toBe('/crm/customer/detail/6')
    } finally { facts.dispose() }
    expect(Request.prototype.open).toBe(originalOpen)
  })
  it('retains a lazy read failure after the visible toast disappears, until a fresh successful read', () => {
    const facts = createPageFacts(); ready(facts)
    const settle = facts.begin('/admin-api/crm/contact/page-by-customer?customerId=6')!
    expect(facts.readPageBlocker()).toBe('pending')
    settle(403, {error: 'page_write_blocked'})
    expect(facts.readPageBlocker()).toBe(true)
    // No toast or body text is needed to preserve the failed read.
    facts.begin('/admin-api/crm/contact/page-by-customer?customerId=6')!(200, {code: 0, data: {list: []}})
    expect(facts.readPageBlocker()).toBe(false)
  })
  it('does not apply one panels failure to another panel or forget it on return', () => {
    const facts = createPageFacts(); ready(facts)
    facts.begin('/admin-api/crm/contact/page-by-customer')!(500, null)
    document.querySelector('[role=tab]')!.setAttribute('aria-controls', 'panel-other')
    expect(facts.readPageBlocker()).toBe(false)
    document.querySelector('[role=tab]')!.setAttribute('aria-controls', 'panel-basic')
    expect(facts.readPageBlocker()).toBe(true)
  })
  it('keeps a concurrent request pending and prevents its older verdict replacing the latest failure', () => {
    const facts = createPageFacts(); ready(facts)
    const old = facts.begin('/admin-api/crm/contact/page-by-customer')!
    const latest = facts.begin('/admin-api/crm/contact/page-by-customer')!
    latest(403, null)
    old(200, {code: 0, data: {list: []}})
    expect(facts.readPageBlocker()).toBe(true)
  })
  it('reports loading as pending but rejects duplicate visible record titles', () => {
    const facts = createPageFacts(); ready(facts)
    const overlay = document.createElement('div'); overlay.className = 'el-loading-mask'
    overlay.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    document.body.append(overlay)
    expect(facts.readPageBlocker()).toBe('pending')
    overlay.remove()
    const second = document.querySelector('span')!.cloneNode(true) as HTMLElement
    second.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    document.body.append(second)
    expect(facts.readPageRecordRoute()).toBeUndefined()
  })
  it('withholds record identity during an exact pending read, then settles or blocks', () => {
    const facts = createPageFacts(); ready(facts)
    const settle = facts.begin('/admin-api/crm/customer/get?id=6')!
    expect(facts.readPageBlocker()).toBe('pending')
    expect(facts.readPageRecordRoute()).toBeUndefined()
    settle(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
    expect(facts.readPageBlocker()).toBe(false)
    expect(facts.readPageRecordRoute()).toBe('/crm/customer/detail/6')
    facts.begin('/admin-api/crm/customer/get?id=6')!(403, null)
    expect(facts.readPageBlocker()).toBe(true)
    expect(facts.readPageRecordRoute()).toBeUndefined()
  })
  it('never calls a concurrent conflicting record read normal loading', () => {
    const facts = createPageFacts(); ready(facts)
    const wrong = facts.begin('/admin-api/crm/customer/get?id=7')!
    const exact = facts.begin('/admin-api/crm/customer/get?id=6')!
    expect(facts.readPageBlocker()).toBe(true)
    wrong(200, {code: 0, data: {id: 7, name: 'Other Customer'}})
    expect(facts.readPageBlocker()).toBe('pending')
    exact(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
    expect(facts.readPageBlocker()).toBe(false)
    const late = facts.begin('/admin-api/crm/customer/get?id=6')!
    document.body.insertAdjacentHTML('beforeend', '<div class="el-message--error">Error</div>')
    document.querySelector('.el-message--error')!.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    expect(facts.readPageBlocker()).toBe(true)
    late(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
    expect(facts.readPageBlocker()).toBe(true)
  })
  it('ignores an old route completion, other origins, writes and non-application requests', () => {
    const facts = createPageFacts()
    expect(facts.begin('https://other.test/admin-api/crm/customer/get?id=6')).toBeNull()
    expect(facts.begin('/admin-api/crm/customer/update', 'PUT')).toBeNull()
    expect(facts.begin('/assets/application.js')).toBeNull()
    const old = facts.begin('/admin-api/crm/customer/get?id=6')!
    window.history.replaceState({}, '', '/crm/customer/detail/12')
    old(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
    expect(facts.readPageRecordRoute()).toBeUndefined()
    expect(facts.readPageBlocker()).toBe('pending')
  })
})

/** The native AppView / RouterView / KeepAlive shape, without importing Vue or
 * changing the host application. Route records hold public component proxies. */
type ComponentType = {name: string}
interface NativeVNode {
  type?: string | ComponentType
  el?: Element
  component?: NativeInstance
  children?: NativeVNode[]
  suspense?: {pendingBranch: object}
  key?: string
}
interface NativeInstance {
  type: ComponentType
  parent: NativeInstance | null
  isMounted?: boolean
  isUnmounted: boolean
  isDeactivated: boolean
  appContext: {app: NativeApp}
  subTree: NativeVNode
  vnode?: NativeVNode
}
interface NativeMatch {
  path: string
  components: {default: ComponentType}
  instances?: {default: {$: NativeInstance}}
}
interface NativeRoute {
  path: string
  fullPath: string
  matched: NativeMatch[]
  meta: {title: string}
}
interface NativeApp {
  config: {globalProperties: {$router?: {currentRoute: {value: NativeRoute}}}}
}
function mountedPage(path = '/system/role', componentName = 'SystemRole') {
  window.history.replaceState({}, '', path)
  document.body.innerHTML = '<div id="app"><header><button>Global menu</button></header>'
    + '<section><div id="destination">Mounted page</div></section></div>'
  for (const element of document.querySelectorAll('*')) {
    element.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
  }
  const section = document.querySelector('section')!, element = document.querySelector('#destination')!
  const component = {name: componentName}
  const app: NativeApp = {config: {globalProperties: {}}}
  const internal = (name: string, parent: NativeInstance | null = null): NativeInstance => ({type: {name}, parent,
    isMounted: true, isUnmounted: false, isDeactivated: false, appContext: {app}, subTree: {}})
  const appView = internal('AppView')
  appView.subTree = {type: 'section', el: section, children: []}
  const routerView = internal('RouterView', appView)
  const keepAlive = internal('KeepAlive', routerView)
  const instance: NativeInstance & {vnode: NativeVNode} = {...internal(componentName, keepAlive), type: component,
    subTree: {type: 'div', el: element, children: []}, vnode: {type: component, key: path, el: element}}
  instance.vnode.component = instance
  keepAlive.subTree = instance.vnode
  routerView.subTree = {type: keepAlive.type, component: keepAlive}
  const matched: NativeMatch = {path, components: {default: component}, instances: {default: {$: instance}}}
  const active: NativeRoute = {path, fullPath: path, matched: [matched], meta: {title: 'Source menu title'}}
  app.config.globalProperties.$router = {currentRoute: {value: active}}
  Object.assign(document.querySelector('#app')!, {__vue_app__: app})
  return {app, active, matched, instance, routerView, appView, keepAlive, component, element, section}
}

describe('native Yudao mounted navigation identity', () => {
  it('binds query controls to the one source-owned live form without production setupState', () => {
    const state=mountedPage('/erp/sale/order', 'SaleOrder')
    state.element.innerHTML='<form><input><button>Search</button></form>'
    const form=state.element.querySelector('form')!, input=form.querySelector('input')!
    const component={...state.instance,type:{name:'ElForm'},props:{model:{pageNo:1,pageSize:10}},
      subTree:{type:'form',el:form},vnode:{ctx:state.instance},parent:state.instance}
    state.instance.subTree.children=[{component} as unknown as NativeVNode]
    expect(readElementPageControl(input)).toEqual({kind:'query'})
    const itemRoot=document.createElement('div');itemRoot.className='el-form-item'
    input.replaceWith(itemRoot);itemRoot.append(input)
    const item={...state.instance,type:{name:'ElFormItem'},props:{prop:'no'},subTree:{type:'div',el:itemRoot}}
    Object.assign(component.subTree,{children:[{component:item}]})
    expect(readElementPageControl(input)).toEqual({kind:'query',field:'no'})
    component.vnode.ctx={} as typeof state.instance
    expect(readElementPageControl(input)).toBeUndefined()
    component.vnode.ctx=state.instance
    document.querySelector('header')!.append(form)
    expect(readElementPageControl(input)).toBeUndefined()
  })
  it('rejects ambiguous or replaced query forms and unmounted route owners', () => {
    const state=mountedPage('/erp/sale/order', 'SaleOrder')
    state.element.innerHTML='<form><input></form><form><input></form>'
    const forms=[...state.element.querySelectorAll('form')]
    const components=forms.map(form=>({...state.instance,type:{name:'ElForm'},
      props:{model:{pageNo:1,pageSize:10}},subTree:{type:'form',el:form},
      vnode:{ctx:state.instance},parent:state.instance}))
    state.instance.subTree.children=components.map(component=>({component})) as unknown as NativeVNode[]
    expect(readElementPageControl(forms[0]!.querySelector('input'))).toBeUndefined()
    state.instance.subTree.children=[{component:components[0]}] as unknown as NativeVNode[]
    expect(readElementPageControl(forms[0]!.querySelector('input'))).toEqual({kind:'query'})
    state.instance.isDeactivated=true
    expect(readElementPageControl(forms[0]!.querySelector('input'))).toBeUndefined()
  })
  it('binds pagination targets to the active page and preserves current/disabled controls', () => {
    const state=mountedPage('/erp/sale/order', 'SaleOrder')
    state.element.innerHTML='<div class="el-pagination"><button class="btn-prev" disabled></button>'
      +'<ul><li class="number">1</li><li class="number">2</li></ul></div>'
    const root=state.element.firstElementChild!
    const component={...state.instance,type:{name:'ElPagination'},props:{currentPage:1,pageSize:10,total:25},
      subTree:{type:'div',el:root},parent:state.instance}
    state.instance.subTree.children=[{component}] as unknown as NativeVNode[]
    const numbers=root.querySelectorAll('li')
    expect(readElementPageControl(numbers[1])).toEqual({kind:'pagination',page:2,pageSize:10})
    expect(readElementPageControl(numbers[0])).toBeUndefined()
    expect(readElementPageControl(root.querySelector('button'))).toBeUndefined()
    component.isUnmounted=true
    expect(readElementPageControl(numbers[1])).toBeUndefined()
  })
  it('retains only the successful applied ledger query, independent of draft inputs and other documents', () => {
    mountedPage('/fms/ledger/general', 'FmsLedgerGeneral')
    const facts = createPageFacts(), other = createPageFacts()
    const query = '/admin-api/fms/ledger/general/list?accountSetId=9&startMonth=2026-09&endMonth=2026-09&minLevel=1&maxLevel=1'
    const first = facts.begin(query)!
    expect(facts.readPageQuery()).toBeUndefined()
    first(200, {code: 0, data: []})
    expect(facts.readPageQuery()?.parameters.startMonth).toBe('2026-09')
    expect(other.readPageQuery()).toBeUndefined()
    // A draft control change is not a successful query.
    const draft = document.createElement('input'); draft.value = '2026-08'; document.body.append(draft)
    expect(facts.readPageQuery()?.parameters.startMonth).toBe('2026-09')
    const old = facts.begin(query)!, newest = facts.begin(query.replaceAll('2026-09', '2026-08'))!
    newest(200, {code: 0, data: []}); old(200, {code: 0, data: []})
    expect(facts.readPageQuery()?.parameters.startMonth).toBe('2026-08')
    const failed = facts.begin(query)!
    expect(facts.readPageQuery()).toBeUndefined()
    failed(200, {code: 123, data: []})
    expect(facts.readPageQuery()).toBeUndefined()
    facts.begin(query)!(200, {code: 0, data: []})
    window.history.replaceState({}, '', '/fms/ledger/detail')
    expect(facts.readPageQuery()).toBeUndefined()
  })
  it('does not retain malformed, duplicate, secret or incomplete ledger query parameters', () => {
    mountedPage('/fms/ledger/general', 'FmsLedgerGeneral')
    const query = '/admin-api/fms/ledger/general/list?accountSetId=9&startMonth=2026-09&endMonth=2026-09&minLevel=1&maxLevel=1'
    for (const input of [query + '&token=private', query + '&accountSetId=10',
      query.replace('accountSetId=9', 'accountSetId=no'), query.replace('&minLevel=1', '')]) {
      const facts = createPageFacts()
      facts.begin(input)!(200, {code: 0, data: []})
      expect(facts.readPageQuery()).toBeUndefined()
    }
  })
  it('keeps a mounted list unverified during its read and after a failed read, even without a toast', () => {
    mountedPage('/erp/sale/order', 'ErpSaleOrder')
    const facts = createPageFacts()
    const settle = facts.begin('/admin-api/erp/sale-order/page?pageNo=1')!
    expect(facts.readPageBlocker()).toBe('pending')
    expect(facts.readPageRoute()).toBeUndefined()
    settle(403, null)
    expect(facts.readPageBlocker()).toBe(true)
    expect(facts.readPageRoute()).toBeUndefined()
    // An actor-verified empty result is valid; failure is never an empty result.
    facts.begin('/admin-api/erp/sale-order/page?pageNo=1')!(200, {code: 0, data: {list: [], total: 0}})
    expect(facts.readPageBlocker()).toBe(false)
    expect(facts.readPageRoute()).toBe('/erp/sale/order')
  })
  it('rejects business-error envelopes and malformed responses on non-CRM pages', () => {
    mountedPage('/mall/trade/order', 'TradeOrder')
    for (const [status, body] of [[200, {code: 403}], [200, undefined], [0, null], [500, {code: 0}]]) {
      const facts = createPageFacts()
      facts.begin('/admin-api/trade/order/page')!(status, body)
      expect(facts.readPageBlocker()).toBe(true)
      expect(facts.readPageRoute()).toBeUndefined()
    }
  })
  it('requires all observed dependencies and preserves the latest verdict across concurrent completions', () => {
    mountedPage('/mall/trade/order', 'TradeOrder')
    const facts = createPageFacts()
    const old = facts.begin('/admin-api/trade/order/page')!
    const latest = facts.begin('/admin-api/trade/order/page')!
    const choices = facts.begin('/admin-api/trade/delivery/express/list-all-simple')!
    latest(200, {code: 0, data: {list: []}})
    old(403, null)
    expect(facts.readPageBlocker()).toBe('pending')
    choices(200, {code: 0, data: []})
    expect(facts.readPageRoute()).toBe('/mall/trade/order')
    const earlier = facts.begin('/admin-api/trade/order/page')!
    facts.begin('/admin-api/trade/order/page')!(403, null)
    earlier(200, {code: 0, data: {list: []}})
    expect(facts.readPageRoute()).toBeUndefined()
  })
  it('does not reuse read completion across a change to the same page query', () => {
    const state = mountedPage('/erp/sale/order', 'ErpSaleOrder')
    const facts = createPageFacts()
    const old = facts.begin('/admin-api/erp/sale-order/page?pageNo=1')!
    window.history.replaceState({}, '', '/erp/sale/order?pageNo=2')
    state.active.fullPath = '/erp/sale/order?pageNo=2'
    state.instance.vnode.key = state.active.fullPath
    const fresh = facts.begin('/admin-api/erp/sale-order/page?pageNo=2')!
    old(200, {code: 0, data: {list: []}})
    expect(facts.readPageBlocker()).toBe('pending')
    fresh(403, null)
    expect(facts.readPageRoute()).toBeUndefined()
  })
  it('fails closed on excessive read dependencies rather than dropping their unfinished state', () => {
    mountedPage()
    const facts = createPageFacts()
    for (let i = 0; i < 64; i++) facts.begin('/admin-api/synthetic/read-' + i)!(200, {code: 0})
    expect(facts.begin('/admin-api/synthetic/overflow')).toBeNull()
    expect(facts.readPageBlocker()).toBe(true)
    expect(facts.readPageRoute()).toBeUndefined()
  })
  it('observes non-CRM XHR failures without retaining the request query', () => {
    mountedPage('/erp/sale/order', 'ErpSaleOrder')
    class Request extends EventTarget {
      responseType = 'json'
      response = {code: 403}
      status = 200
      open() {}
      send() { this.dispatchEvent(new Event('loadend')) }
    }
    const facts = installPageFacts({location: window.location, fetch: window.fetch, XMLHttpRequest: Request}, document)
    try {
      const request = new Request()
      // @ts-expect-error Browser arguments are received by the installed wrapper.
      request.open('GET', '/admin-api/erp/sale-order/page?name=Synthetic')
      request.send()
      expect(facts.readPageRoute()).toBeUndefined()
      expect(facts.readPageBlocker()).toBe(true)
    } finally { facts.dispose() }
  })
  it('proves a simple parameter route only with the exact mounted record and successful identity read', () => {
    const state = mountedPage('/crm/customer/detail/6', 'CrmCustomerDetail')
    state.matched.path = '/crm/customer/detail/:id'
    state.element.innerHTML = '<span class="text-xl font-bold">Synthetic Customer</span>'
      + '<div role="tab" aria-selected="true" aria-controls="panel-basic">基本信息</div>'
    for (const element of state.element.querySelectorAll('*'))
      element.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    const facts = createPageFacts()
    expect(facts.readPageRoute()).toBeUndefined()
    ready(facts)
    expect(facts.readPageRoute()).toBe('/crm/customer/detail/6')
    state.matched.path = '/crm/customer/detail/:pathMatch(.*)*'
    expect(facts.readPageRoute()).toBeUndefined()
    state.matched.path = '/crm/contract/detail/:id'
    expect(facts.readPageRoute()).toBeUndefined()
  })
  it('does not grant a route from source, currentRoute, metadata, menu or header alone', () => {
    const state = mountedPage()
    delete state.matched.instances
    state.element.remove()
    expect(createPageFacts().readPageRoute()).toBeUndefined()
  })
  it('proves the exact visible component mounted inside the active AppView', () => {
    const state = mountedPage()
    const facts = createPageFacts()
    expect(facts.readPageRoute()).toBe('/system/role')
    // AppView may have a fragment root when its optional footer is enabled.
    state.appView.subTree = {children: [{type: 'section', el: state.section}, {type: 'footer'}]}
    expect(facts.readPageRoute()).toBe('/system/role')
    expect(facts.readPageRecordRoute()).toBeUndefined()
    facts.dispose()
    expect(facts.readPageRoute()).toBeUndefined()
  })
  it('refuses the old component while the URL or current route changes', () => {
    const state = mountedPage()
    const facts = createPageFacts()
    window.history.replaceState({}, '', '/system/post')
    expect(facts.readPageRoute()).toBeUndefined()
    state.active.path = '/system/post'; state.active.fullPath = '/system/post'
    state.matched.path = '/system/post'
    expect(facts.readPageRoute()).toBeUndefined()
    state.instance.vnode.key = '/system/post'
    state.matched.components.default = {name: 'SystemPost'}
    expect(facts.readPageRoute()).toBeUndefined()
  })
  it('refuses cached, detached, unmounted and header-only component instances', () => {
    for (const change of [
      (state: ReturnType<typeof mountedPage>) => {state.instance.isDeactivated = true},
      (state: ReturnType<typeof mountedPage>) => {state.instance.isMounted = false},
      (state: ReturnType<typeof mountedPage>) => {state.instance.isUnmounted = true},
      (state: ReturnType<typeof mountedPage>) => {state.element.remove()},
      (state: ReturnType<typeof mountedPage>) => {document.querySelector('header')!.append(state.element)},
      (state: ReturnType<typeof mountedPage>) => {state.keepAlive.subTree = {type: 'div', el: state.element}},
      (state: ReturnType<typeof mountedPage>) => {state.appView.type.name = 'ToolHeader'},
    ]) {
      const state = mountedPage(); change(state)
      expect(createPageFacts().readPageRoute()).toBeUndefined()
    }
  })
  it('refuses wildcard 404, concrete error and nested error component renders', () => {
    const wildcard = mountedPage('/help', 'Error404')
    wildcard.matched.path = '/:pathMatch(.*)*'
    expect(createPageFacts().readPageRoute()).toBeUndefined()
    for (const name of ['Error403', 'Error404', 'Error500', 'Error', 'NoFound', 'NoAccess']) {
      mountedPage('/help', name)
      expect(createPageFacts().readPageRoute()).toBeUndefined()
    }
    const state = mountedPage()
    state.instance.subTree.children = [{component: {...state.instance, type: {name: 'Error'}}}]
    expect(createPageFacts().readPageRoute()).toBeUndefined()
  })
  it('withholds a mounted route while loading or displaying a blocking error', () => {
    for (const markup of ['<div class="el-loading-mask"></div>', '<div class="el-message--error">Failed</div>',
      '<div class="el-notification--error">Failed</div>', '<div aria-busy="true"></div>']) {
      mountedPage()
      document.body.insertAdjacentHTML('beforeend', markup)
      const blocker = document.body.lastElementChild!
      blocker.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
      const facts = createPageFacts()
      expect(facts.readPageRoute()).toBeUndefined()
      blocker.setAttribute('hidden', '')
      expect(facts.readPageRoute()).toBe('/system/role')
    }
  })
  it('refuses custom keys, pending suspense, missing mount state and oversized render trees', () => {
    for (const change of [
      (state: ReturnType<typeof mountedPage>) => {state.instance.vnode.key = 'shared-view-key'},
      (state: ReturnType<typeof mountedPage>) => {state.routerView.subTree.suspense = {pendingBranch: {}}},
      (state: ReturnType<typeof mountedPage>) => {delete state.instance.isMounted},
      (state: ReturnType<typeof mountedPage>) => {state.instance.subTree.children = Array.from({length: 4096}, () => ({type: 'div'}))},
    ]) {
      const state = mountedPage(); change(state)
      expect(createPageFacts().readPageRoute()).toBeUndefined()
    }
  })
})


describe('native date range selection uses only an exact owned live calendar', () => {
  it('rejects other months, disabled or duplicate days and unrelated popups', () => {
    document.body.innerHTML = '<div class="el-date-editor--daterange"><input class="el-range-input" aria-controls="calendar" aria-haspopup="dialog"></div>'
      + '<div id="calendar" role="dialog"><div class="el-date-range-picker__content"><div class="el-date-range-picker__header">'
      + '<span role="button">2026 年</span><span role="button">10 月</span></div><table role="grid"><tbody><tr>'
      + '<td class="prev-month"><span class="el-date-table-cell__text">31</span></td>'
      + '<td class="available" aria-disabled="false"><span class="el-date-table-cell__text">31</span></td>'
      + '</tr></tbody></table></div></div>'
    const input = document.querySelector('input')!, cell = document.querySelector('td.available')!
    expect(nativeRangeDateCell(input,'2026-10-31')).toBe(cell)
    for (const value of ['2026-09-31','2026-11-31','2026-11-01','wrong']) {
      expect(nativeRangeDateCell(input,value)).toBeNull()
    }
    cell.setAttribute('aria-disabled','true')
    expect(nativeRangeDateCell(input,'2026-10-31')).toBeNull()
    cell.setAttribute('aria-disabled','false')
    cell.parentElement!.append(cell.cloneNode(true))
    expect(nativeRangeDateCell(input,'2026-10-31')).toBeNull()
    cell.parentElement!.lastChild!.remove()
    input.setAttribute('aria-controls','other')
    expect(nativeRangeDateCell(input,'2026-10-31')).toBeNull()
  })
})
