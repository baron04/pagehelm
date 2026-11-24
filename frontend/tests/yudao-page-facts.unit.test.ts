import {afterAll, beforeEach, describe, expect, it, vi} from 'vitest'
import {createHash} from 'node:crypto'
import {execFileSync} from 'node:child_process'
import {resolve} from 'node:path'
import {tryOpenAnswerInPlace} from '../src/runtime/answerNavigation'
import {Page, type PageControllerApi} from '../src/runtime/page'
import {createRedactor} from '../src/runtime/redaction'
// @ts-expect-error Framework-free application module is served separately from the Runtime.
import {createPageFacts, installPageFacts, disposeInstalledPageFacts, nativeRangeDateCell, nativeRangeDateStep, readElementPageControl, nativeControlElement, openPage, withTreeQueryControls, withUserStatusControls, readElementFormModel, restoreElementFormModel, sameFormStructure, boundRecord, boundInput, boundClick} from '../src/integrations/yudao/page-facts.js'

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
  it.each(['hrm/employee', 'crm/contact', 'crm/clue', 'crm/product'])('binds %s detail reads and excludes editor tabs', (entity) => {
    window.history.replaceState({}, '', `/${entity}/detail/6`)
    const facts = createPageFacts()
    expect(facts.readPageBlocker()).toBe('pending')
    expect(facts.readPageRecordRoute()).toBeUndefined()
    facts.begin(`/admin-api/${entity}/get?id=6`)!(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
    expect(facts.readPageRecordRoute()).toBe(`/${entity}/detail/6`)
    const editor = document.createElement('div')
    editor.setAttribute('role', 'dialog')
    editor.innerHTML = '<div role="tab" aria-selected="true">Editor fields</div>'
    document.body.append(editor)
    editor.firstElementChild!.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    expect(facts.readPageRecordRoute()).toBe(`/${entity}/detail/6`)
    facts.begin(`/admin-api/${entity}/get?id=7`)!(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
    expect(facts.readPageRecordRoute()).toBeUndefined()
  })
  it.each([
    ['/infra/config', 'Config', '/admin-api/infra/config/page'],
    ['/mall/trade/order', 'TradeOrder', '/admin-api/trade/order/page'],
  ])('retains the applied creation date range through XHR on %s', async (route, component, path) => {
    pageTable(mountedPage(route, component), [])
    class Request extends EventTarget {
      responseType = 'json'; response = {code: 0, data: {list: [], total: 0}}; status = 200
      open() {}
      send() { this.dispatchEvent(new Event('loadend')) }
    }
    const view = {location: window.location, fetch: window.fetch, XMLHttpRequest: Request, crypto: window.crypto}
    const facts = installPageFacts(view, document)
    const call = (query: string) => {
      const request = new Request()
      // @ts-expect-error The fake receives browser arguments through the installed wrapper.
      request.open('GET', path + query)
      expect(facts.readPageQuery()).toBeUndefined()
      request.send()
    }
    const query = '?pageNo=1&pageSize=10&createTime%5B0%5D=2026-10-01%2000%3A00%3A00'
      + '&createTime%5B1%5D=2026-10-31%2023%3A59%3A59'
    try {
      call(query)
      await vi.waitFor(() => {
        expect(facts.readPageQuery()).toEqual({path, parameters: {
          createTimeStart: '2026-10-01 00:00:00', createTimeEnd: '2026-10-31 23:59:59'}})
        expect(facts.readPageRead()?.query_fields).toHaveProperty('createTime[0]')
      })
      // A new request invalidates the old projection at send, including failures.
      const failure = new Request(); failure.status = 403
      // @ts-expect-error The fake receives browser arguments through the installed wrapper.
      failure.open('GET', path + query)
      failure.send()
      expect(facts.readPageQuery()).toBeUndefined()
      for (const invalid of [query + '&createTime%5B0%5D=2026-10-02%2000%3A00%3A00',
        query.replace('2023%3A59%3A59', '2000%3A00%3A00'),
        query.slice(0, query.indexOf('&createTime%5B1%5D=')), query + '&token=synthetic-private']) {
        call(invalid)
        await vi.waitFor(() => expect(facts.readPageRead()).toBeDefined())
        expect(facts.readPageQuery()).toBeUndefined()
      }
      call('?pageNo=1&pageSize=10')
      await vi.waitFor(() => expect(facts.readPageQuery()).toEqual({path, parameters: {}}))
    } finally { facts.dispose() }
  })
  it('reports order filters only after the latest successful application read', async () => {
    pageTable(mountedPage('/erp/sale/order', 'SaleOrder'), [])
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
  it('retains the sales business number through XHR only after its successful list read', async () => {
    pageTable(mountedPage('/erp/sale/order', 'SaleOrder'), [{id: 17}])
    class Request extends EventTarget {
      responseType = 'json'; response = {code:0,data:{list:[{id:17}],total:1}}; status = 200
      open() {}
      send() {this.dispatchEvent(new Event('loadend'))}
    }
    const facts = installPageFacts({location:window.location,fetch:window.fetch,
      XMLHttpRequest:Request,crypto:window.crypto},document)
    const path = '/admin-api/erp/sale-order/page'
    const call = (query: string, status = 200) => {
      const request = new Request(); request.status = status
      // @ts-expect-error The installed wrapper receives native browser arguments.
      request.open('GET',path + '?pageNo=1&pageSize=10&' + query)
      request.send()
    }
    try {
      call('no=SO-SYNTHETIC-17')
      await vi.waitFor(() => {
        expect(facts.readPageQuery()).toEqual({path,parameters:{no:'SO-SYNTHETIC-17'}})
        expect(facts.readPageRead()).toMatchObject({path,total:1,rows:1,query_fields:{
          no:createHash('sha256').update(JSON.stringify(['SO-SYNTHETIC-17'])).digest('hex')}})
      })
      call('no=SO-SYNTHETIC-17',403)
      expect(facts.readPageQuery()).toBeUndefined()
      for (const query of ['no=SO-17&no=SO-18','no=SO-17&token=private','no='+'x'.repeat(65)]) {
        call(query)
        await vi.waitFor(() => expect(facts.readPageRead()).toBeDefined())
        expect(facts.readPageQuery()).toBeUndefined()
      }
    } finally {facts.dispose()}
  })
  it('binds any standard list read to query digests and paging without retaining raw values', async () => {
    const table = pageTable(mountedPage('/erp/sale/order', 'SaleOrder'), [{id: 1}])
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
    table.data = []
    expect(facts.readPageRead()?.revision).toBe(2)
    expect(facts.readPageRead()?.query_digest).toBe(hash([['no','SO-SYNTHETIC'],['remark','synthetic']]))
    const failure = facts.begin(url)!
    failure(200,{code:500,data:{list:[],total:25}})
    expect(facts.readPageRead()).toBeUndefined()
  })
  it('tracks a customer list tab as a newer read with a changed scene query', async () => {
    const table = pageTable(mountedPage('/crm/customer', 'CrmCustomer'), [{id: 1}])
    const facts = createPageFacts()
    const path = '/admin-api/crm/customer/page'
    const first = facts.begin(`${path}?pageNo=1&pageSize=10&sceneType=1`)!
    await first(200, {code:0, data:{list:[{id:1}], total:2}})
    const original = facts.readPageRead()!
    expect(facts.readPageQuery()).toEqual({path, parameters:{pageNo:'1', pageSize:'10', sceneType:'1'}})
    const next = facts.begin(`${path}?pageNo=1&pageSize=10&sceneType=2`)!
    expect(facts.readPageRead()).toBeUndefined()
    await next(200, {code:0, data:{list:[{id:2}], total:2}})
    table.data = [{id: 2}]
    expect(facts.readPageRead()).toMatchObject({path, revision:original.revision + 1})
    expect(facts.readPageRead()?.query_digest).not.toBe(original.query_digest)
    expect(facts.readPageQuery()).toEqual({path, parameters:{pageNo:'1', pageSize:'10', sceneType:'2'}})
  })
  it('never takes a cached page refreshing its list on a route change for this page\'s list', async () => {
    pageTable(mountedPage('/erp/finance/account', 'ErpAccount'), [{id: 1}, {id: 2}])
    const facts = createPageFacts()
    const own = facts.begin('/admin-api/erp/account/page?pageNo=1&pageSize=10')!
    // The cached customer list reloads itself on every route change; its response lands last.
    const cached = facts.begin('/admin-api/crm/customer/page?pageNo=1&pageSize=10&sceneType=1')!
    await own(200, {code:0, data:{list:[{id:1}, {id:2}], total:2}})
    await cached(200, {code:0, data:{list:[{id:101}, {id:102}, {id:103}], total:240}})
    expect(facts.readPageQuery()).toEqual({path:'/admin-api/erp/account/page', parameters:{pageNo:'1', pageSize:'10'}})
    expect(facts.readPageRead()).toMatchObject({path:'/admin-api/erp/account/page', total:2, rows:2})
  })
  it('binds an editor only to a unique successful current-route record source', async () => {
    mountedPage('/crm/contact', 'Contact')
    const facts = createPageFacts()
    const own = '/admin-api/crm/contact/get'
    await facts.begin(own + '?id=5')!(200, {code: 0, data: {id: 5}})
    expect(facts.readFormSource(5)).toBe(own)
    await facts.begin('/admin-api/other/record/get?id=5')!(200, {code: 0, data: {id: 5}})
    expect(facts.readFormSource(5)).toBeUndefined()
    window.history.replaceState({}, '', '/system/post')
    expect(facts.readFormSource(5)).toBeUndefined()
    const pending = facts.begin(own + '?id=5')!
    expect(facts.readFormSource(5)).toBeUndefined()
    await pending(500, null)
    expect(facts.readFormSource(5)).toBeUndefined()
  })
  it.each(['page', 'list', 'page-by-state'])('binds a row to its own table source %s, refusing ambiguous IDs', async endpoint => {
    const state = mountedPage('/erp/finance/account', 'ErpAccount')
    pageTable(state, [{id: 1}, {id: 2}])
    const table = state.element.querySelector('.el-table')!
    const button = document.createElement('button'); table.append(button)
    const facts = createPageFacts()
    const source = `/admin-api/erp/account/${endpoint}`
    await facts.begin(source)!(200, {code: 0, data: endpoint === 'list' ? [{id: 1}, {id: 2}] : {list: [{id: 1}, {id: 2}], total: 2}})
    await facts.begin('/admin-api/crm/customer/page')!(200, {code: 0, data: {list: [{id: 3}], total: 1}})
    expect(facts.readRowSource(button)).toBe(source)
    await facts.begin('/admin-api/crm/customer/page')!(200, {code: 0, data: {list: [{id: 1}, {id: 2}], total: 2}})
    expect(facts.readRowSource(button)).toBeUndefined()
  })
  it('reports no list until a visible table of the page renders its records', async () => {
    const state = mountedPage('/erp/finance/account', 'ErpAccount')
    const facts = createPageFacts()
    await facts.begin('/admin-api/erp/account/page?pageNo=1&pageSize=10')!(200,
      {code:0, data:{list:[{id:1}], total:1}})
    expect(facts.readPageQuery()).toBeUndefined()
    expect(facts.readPageRead()).toBeUndefined()
    const table = pageTable(state, [{id:7}])
    expect(facts.readPageRead()).toBeUndefined()
    table.data = [{id:1}]
    expect(facts.readPageRead()).toMatchObject({path:'/admin-api/erp/account/page', total:1, rows:1})
    const root = state.element.querySelector('.el-table')! as HTMLElement
    root.getBoundingClientRect = () => ({width:0, height:0} as DOMRect)
    expect(facts.readPageRead()).toBeUndefined()
    root.getBoundingClientRect = () => ({width:50, height:20} as DOMRect)
    // A table inside a dialog (an editor's lines) is not the page's list.
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    state.element.append(dialog)
    dialog.append(root)
    expect(facts.readPageRead()).toBeUndefined()
    state.element.append(root)
    dialog.remove()
    expect(facts.readPageRead()).toMatchObject({path:'/admin-api/erp/account/page'})
  })
  it('reports neither list when page tables render two endpoints at once', async () => {
    const state = mountedPage('/mes/pro/task', 'ProTask')
    pageTable(state, [{id:1}])
    pageTable(state, [{id:9}])
    const facts = createPageFacts()
    await facts.begin('/admin-api/mes/pro/task/page?pageNo=1&pageSize=10')!(200, {code:0, data:{list:[{id:1}], total:1}})
    await facts.begin('/admin-api/mes/pro/work-order/page?pageNo=1&pageSize=10')!(200,
      {code:0, data:{list:[{id:9}], total:1}})
    expect(facts.readPageQuery()).toBeUndefined()
    expect(facts.readPageRead()).toBeUndefined()
  })
  it('keeps a shared endpoint to its newest request, and only while the table renders it', async () => {
    pageTable(mountedPage('/crm/customer/pool', 'CrmCustomerPool'), [{id:5}])
    const facts = createPageFacts()
    const path = '/admin-api/crm/customer/page'
    // The cached customer list's watcher sends first; the pool's own read follows.
    const cached = facts.begin(`${path}?pageNo=1&pageSize=10&sceneType=1`)!
    const own = facts.begin(`${path}?pageNo=1&pageSize=10&pool=true`)!
    await cached(200, {code:0, data:{list:[{id:101}], total:240}})
    await own(200, {code:0, data:{list:[{id:5}], total:1}})
    expect(facts.readPageQuery()).toEqual({path, parameters:{pageNo:'1', pageSize:'10', pool:'true'}})
    // Sent the other way round, the newest request renders nowhere: report nothing rather than it.
    const early = facts.begin(`${path}?pageNo=1&pageSize=10&pool=true`)!
    const late = facts.begin(`${path}?pageNo=1&pageSize=10&sceneType=1`)!
    await early(200, {code:0, data:{list:[{id:5}], total:1}})
    await late(200, {code:0, data:{list:[{id:101}], total:240}})
    expect(facts.readPageQuery()).toBeUndefined()
    expect(facts.readPageRead()).toBeUndefined()
  })
  it('matches records without IDs only by their row count and the one visible total', async () => {
    pageTable(mountedPage('/erp/finance/account', 'ErpAccount'), [{name:'一般户'}, {name:'现金'}], 2)
    const facts = createPageFacts()
    await facts.begin('/admin-api/erp/account/page?pageNo=1&pageSize=10')!(200,
      {code:0, data:{list:[{name:'一般户'}, {name:'现金'}], total:2}})
    expect(facts.readPageRead()).toMatchObject({path:'/admin-api/erp/account/page', total:2, rows:2})
    await facts.begin('/admin-api/crm/customer/page?pageNo=1&pageSize=10')!(200,
      {code:0, data:{list:[{name:'甲'}, {name:'乙'}], total:240}})
    expect(facts.readPageRead()).toMatchObject({path:'/admin-api/erp/account/page', total:2})
  })
  it('settles the current pending count when another list read starts before hashing completes', async () => {
    pageTable(mountedPage('/erp/sale/order', 'SaleOrder'), [])
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
  it('marks this page\'s own API requests only while the assistant is working it', async () => {
    const sent: Array<[string, Record<string, string>]> = []
    class Request extends EventTarget {
      responseType = 'json'; response = {code: 0, data: {}}; status = 200
      headers: Record<string, string> = {}; url = ''
      open(_method: string, url: string) { this.url = url }
      setRequestHeader(name: string, value: string) { this.headers[name] = value }
      send() { sent.push([this.url, this.headers]); this.dispatchEvent(new Event('loadend')) }
    }
    let answer: unknown = {active: true, expires_at: Date.now() / 1000 + 60}
    const fetched: Array<[string, string | null]> = []
    const view = {location: window.location, XMLHttpRequest: Request, Headers,
      fetch: async (input: string, init?: RequestInit) => {
        fetched.push([input, new Headers(init?.headers).get('X-WOA-Assistant')])
        return new Response(JSON.stringify(answer), {status: 200})
      }}
    const facts = installPageFacts(view, document)
    const call = (url: string) => { const r = new Request(); r.open('POST', url); r.send(); return sent[sent.length - 1]![1] }
    try {
      // Before any window: the person's own request, unmarked.
      expect(call('/admin-api/crm/customer/update')).toEqual({})
      await view.fetch('/woa/page-guard/begin', {method: 'POST'})
      expect(call('/admin-api/crm/customer/update')).toEqual({'X-WOA-Assistant': '1'})
      await view.fetch('/admin-api/infra/file/presigned-url')
      expect(fetched[fetched.length - 1]).toEqual(['/admin-api/infra/file/presigned-url', '1'])
      // Nothing outside the application's API is marked.
      expect(call('/other/path')).toEqual({})
      // The mark stops the moment the window's end is asked for.
      await view.fetch('/woa/page-guard/end', {method: 'POST'})
      expect(call('/admin-api/crm/customer/update')).toEqual({})
      // A window the application did not confirm marks nothing.
      answer = {active: false}
      await view.fetch('/woa/page-guard/begin', {method: 'POST'})
      expect(call('/admin-api/crm/customer/update')).toEqual({})
    } finally { facts.dispose() }
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
  it('isolates auxiliary subcard and simple-list 403s once primary detail or list succeeds while blocking primary 403s', () => {
    window.history.replaceState({}, '', '/member/user/detail/105')
    document.body.innerHTML = '<div class="detail-body">Member 105</div>'
    const facts = createPageFacts()
    // Before primary get succeeds, auxiliary 403 still blocks
    facts.begin('/admin-api/pay/wallet/get?userId=105')!(200, {code: 403, msg: '没有该操作权限'})
    document.body.insertAdjacentHTML('beforeend', '<div class="el-message--error">没有该操作权限</div>')
    for (const el of document.querySelectorAll('*')) {
      el.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    }
    expect(facts.readPageBlocker()).toBe(true)
    // Primary detail get fails with 403 -> strictly blocked
    facts.begin('/admin-api/member/user/get?id=105')!(200, {code: 403, msg: '没有该操作权限'})
    expect(facts.readPageBlocker()).toBe(true)
    // Primary detail get succeeds -> auxiliary subcard 403 + permission toast are isolated
    facts.begin('/admin-api/member/user/get?id=105')!(200, {code: 0, data: {id: 105, nickname: 'Member 105'}})
    facts.begin('/admin-api/member/level/list-all-simple')!(403, null)
    expect(facts.readPageBlocker()).toBe(false)
    // Non-permission error toast still blocks immediately
    document.body.insertAdjacentHTML('beforeend', '<div class="el-message--error">系统内部异常</div>')
    for (const el of document.querySelectorAll('.el-message--error')) {
      el.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    }
    expect(facts.readPageBlocker()).toBe(true)
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
  job?: {flags: number}
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
  _container?: {_vnode: NativeVNode}
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
    isMounted: true, isUnmounted: false, isDeactivated: false, job: {flags:0}, appContext: {app}, subTree: {}})
  const appView = internal('AppView')
  appView.subTree = {type: 'section', el: section, children: []}
  const routerView = internal('RouterView', appView)
  const keepAlive = internal('KeepAlive', routerView)
  const instance: NativeInstance & {vnode: NativeVNode} = {...internal(componentName, keepAlive), type: component,
    subTree: {type: 'div', el: element, children: []}, vnode: {type: component, key: path, el: element}}
  instance.vnode.component = instance
  keepAlive.subTree = instance.vnode
  routerView.subTree = {type: keepAlive.type, component: keepAlive}
  appView.subTree.children = [{type: routerView.type, component: routerView}]
  app._container = {_vnode: {type: appView.type, component: appView}}
  const matched: NativeMatch = {path, components: {default: component}, instances: {default: {$: instance}}}
  const active: NativeRoute = {path, fullPath: path, matched: [matched], meta: {title: 'Source menu title'}}
  app.config.globalProperties.$router = {currentRoute: {value: active}}
  Object.assign(document.querySelector('#app')!, {__vue_app__: app})
  return {app, active, matched, instance, routerView, appView, keepAlive, component, element, section}
}

describe('component location bindings', () => {
  function table() {
    const state = mountedPage('/system/role')
    const props = pageTable(state, [{id: 7, name: 'Alpha'}, {id: 8, name: 'Beta'}])
    const root = state.element.querySelector('.el-table')!
    root.innerHTML = '<div class="el-table__header-wrapper"><table><thead><tr><th>Name</th><th>Action</th></tr></thead></table></div>'
      + '<div class="el-table__body-wrapper"><table><tbody>'
      + '<tr><td><a>Alpha</a></td><td><button>详情</button></td></tr>'
      + '<tr><td>Beta</td><td><button>详情</button></td></tr></tbody></table></div>'
    for (const node of root.querySelectorAll('*')) node.getBoundingClientRect = () => ({width:50,height:20} as DOMRect)
    const owner = state.instance.subTree.children![0]!.component!
    const store = {states:{data:{value:[...props.data]}}}
    const body = {...state.instance,type:{name:'ElTableBody'},props:{store},
      subTree:{type:'tbody',el:root.querySelector('tbody')!,children:[] as NativeVNode[]}}
    Object.assign(owner, {uid: 23,store})
    owner.subTree.children = [{component:body}] as unknown as NativeVNode[]
    return {state, props, store, body, root, owner, button: root.querySelector('button')!}
  }
  it('binds a visible business value to its own table record and invalidates reused DOM buttons', () => {
    const s = table()
    expect(boundRecord(s.button)).toEqual({id:'7',table:'23',values:[['Name','Alpha']]})
    const api = {selectorMap: {}, getBrowserState: vi.fn(), clickElement: vi.fn(), inputText: vi.fn(),
      selectOption: vi.fn(), scroll: vi.fn(), dispose: vi.fn()} as PageControllerApi
    const page = new Page(api, () => s.button, createRedactor(), undefined, undefined, undefined,
      undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined,
      element => ({kind:'record',...boundRecord(element)}))
    const before = page.fingerprint('e1')
    Object.assign(s.owner, {job:{flags:1}})
    expect(boundRecord(s.button)).toBeUndefined()
    Object.assign(s.owner, {job:{flags:0}})
    s.store.states.data.value.reverse() // Internal sorting keeps props.data unchanged.
    s.root.querySelector('tbody td')!.textContent = 'Beta'
    expect(page.fingerprint('e1')).not.toBe(before)
    expect(boundRecord(s.button)?.id).toBe('8')
    Object.assign(s.body, {job:{flags:1}})
    expect(boundRecord(s.button)).toBeUndefined()
    Object.assign(s.body, {job:{flags:0}})
    s.store.states.data.value = [{id:7},{id:7}]
    expect(boundRecord(s.button)).toBeUndefined()
    s.store.states.data.value = [{id:7}]
    expect(boundRecord(s.button)).toBeUndefined() // Expanded/virtual/misaligned rows.
  })
  it('calls the mounted row callback once without dispatching pointer or click events', async () => {
    const s = table(), action = vi.fn(), dom = vi.fn()
    s.button.addEventListener('click', dom)
    s.body.subTree.children = [{type:'button',el:s.button,props:{onClick:action}}] as unknown as NativeVNode[]
    expect(await boundClick(s.button)).toBe(true)
    expect(action).toHaveBeenCalledTimes(1)
    expect(dom).not.toHaveBeenCalled()
    s.state.instance.isDeactivated = true
    expect(await boundClick(s.button)).toBe(false)
    expect(action).toHaveBeenCalledTimes(1)
  })
  it('uses update/change callbacks of the active query, never a matching business form', async () => {
    const state = mountedPage()
    state.element.innerHTML = '<form><div class="el-form-item"><input></div></form>'
    const form = state.element.querySelector('form')!, item = form.firstElementChild!, input = item.firstElementChild as HTMLInputElement
    for (const node of [form,item,input]) node.getBoundingClientRect = () => ({width:50,height:20} as DOMRect)
    const model = {pageNo:1,pageSize:10,name:''}, calls: string[] = []
    const onInput = async (event: Event) => {model.name = (event.target as HTMLInputElement).value; calls.push('input')}
    const onChange = () => {calls.push('change')}
    const field = {...state.instance,type:{name:'ElFormItem'},props:{prop:'name'},
      subTree:{type:'div',el:item,children:[{type:'input',el:input,props:{onInput,onChange}}]}}
    const owner = {...state.instance,type:{name:'ElForm'},props:{model},vnode:{ctx:state.instance},
      subTree:{type:'form',el:form,children:[{component:field}]}}
    state.instance.subTree.children = [{component:owner}] as unknown as NativeVNode[]
    expect(await boundInput(input,'Alpha')).toBe(true)
    expect(model.name).toBe('Alpha')
    expect(calls).toEqual(['input','change'])
    delete (model as Partial<typeof model>).pageNo
    expect(await boundInput(input,'Unrequested')).toBe(false)
    expect(model.name).toBe('Alpha')
    model.pageNo = 1; input.readOnly = true
    expect(await boundInput(input,'Unrequested')).toBe(false)
  })
})

/** A visible Element Plus table of the mounted page, bound to the records it renders.
 * Replace the returned `data` as the page re-renders after a read. */
function pageTable(state: ReturnType<typeof mountedPage>, data: object[], total?: number) {
  const root = document.createElement('div')
  root.className = 'el-table'
  root.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
  state.element.append(root)
  if (total !== undefined) {
    const shown = document.createElement('span')
    shown.className = 'el-pagination__total'
    shown.textContent = `共 ${total} 条`
    shown.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    state.element.append(shown)
  }
  const props = {data}
  const table = {...state.instance, type: {name: 'ElTable'}, props, subTree: {type: 'div', el: root, children: []}}
  state.instance.subTree.children = [...(state.instance.subTree.children || []),
    {component: table} as unknown as NativeVNode]
  return props
}

describe('native Yudao mounted navigation identity', () => {
  it('keeps a list pending while a native spinner remains paintable after its mask collapses', () => {
    mountedPage('/system/user','SystemUser')
    const spinner=document.createElement('div');spinner.className='el-loading-spinner'
    spinner.getBoundingClientRect=()=>({width:30,height:30} as DOMRect)
    document.querySelector('#app')!.append(spinner)
    const facts=createPageFacts()
    expect(facts.readPageBlocker()).toBe('pending')
    spinner.remove()
    expect(facts.readPageBlocker()).toBe(false)
  })
  it('observes only a native page-owned status switch whose row and component values agree', () => {
    const state=mountedPage('/system/user','SystemUser')
    state.element.innerHTML='<div class="el-table"><div class="el-table__body-wrapper"><table><tbody>'
      + '<tr><td></td><td>7</td><td>account7</td><td>合成用户</td><td><div class="el-switch">'
      + '<input role="switch" aria-checked="true"></div></td></tr></tbody></table></div></div>'
    const root=state.element.firstElementChild!,area=root.querySelector('.el-switch')!
    area.getBoundingClientRect=()=>({left:10,top:10,bottom:30,width:50,height:20} as DOMRect)
    const record={id:7,username:'account7',nickname:'合成用户',status:0}
    const props={modelValue:0,activeValue:0,inactiveValue:1,disabled:false}
    const control={...state.instance,type:{name:'ElSwitch'},props,subTree:{type:'div',el:area}}
    const table={...state.instance,type:{name:'ElTable'},props:{data:[record]},
      subTree:{type:'div',el:root,children:[{component:control}]}}
    state.instance.subTree.children=[{component:table}] as unknown as NativeVNode[]
    const map=new Map()
    expect(withUserStatusControls(map,'existing')).toContain('role=switch id=woa-user-status-7 aria-checked=true>状态开关：合成用户（account7）')
    expect(map.get(1).ref).toBe(area)
    props.modelValue=1
    expect(withUserStatusControls(new Map(),'existing')).toBe('existing')
    props.modelValue=0;record.id=8
    expect(withUserStatusControls(new Map(),'existing')).toBe('existing')
    record.id=7;props.disabled=true
    expect(withUserStatusControls(new Map(),'existing')).toBe('existing')
  })
  it('indexes only the live page-owned department node hit area and follows selection', () => {
    const state=mountedPage('/system/user','SystemUser')
    state.element.innerHTML='<div class="el-tree" role="tree"><div class="el-tree-node" role="treeitem" data-key="103">'
      + '<div class="el-tree-node__content"><span class="el-tree-node__label">研发部门</span></div></div></div>'
    const root=state.element.firstElementChild!,node=root.firstElementChild!,area=node.firstElementChild!
    for(const e of [root,node,area])e.getBoundingClientRect=()=>({left:10,top:10,bottom:30,width:100,height:20} as DOMRect)
    const wrapper={...state.instance,type:{name:'DeptTreeSelect'},vnode:{ctx:state.instance}}
    const nativeNode={data:{id:103,name:'研发部门'},isCurrent:false}
    const item={...state.instance,type:{name:'ElTreeNode'},props:{node:nativeNode},subTree:{type:'div',el:node}}
    const tree={...state.instance,type:{name:'ElTree'},vnode:{ctx:wrapper},
      props:{nodeKey:'id',showCheckbox:false,draggable:false},subTree:{type:'div',el:root,children:[{component:item}]}}
    state.instance.subTree.children=[{component:tree}] as unknown as NativeVNode[]
    expect(readElementPageControl(area)).toEqual({kind:'query',field:'deptId',queryValue:'103',selected:false})
    const map=new Map()
    expect(withTreeQueryControls(map,'existing')).toContain('role=treeitem aria-selected=false>研发部门')
    expect(map.get(1).ref).toBe(area)
    nativeNode.isCurrent=true
    expect(withTreeQueryControls(new Map(),'existing')).toContain('aria-selected=true')
    wrapper.vnode.ctx={...state.instance} // A child/editor/cache is not the active source owner.
    expect(readElementPageControl(area)).toBeUndefined()
    wrapper.vnode.ctx=state.instance
    node.setAttribute('data-key','104') // DOM labels and IDs alone cannot forge the native node.
    expect(readElementPageControl(area)).toBeUndefined()
  })
  it('proves the active keyed page after query replacement clears the route instance table', () => {
    const state = mountedPage('/pms/pm/project/list', 'ProjectList')
    delete state.matched.instances
    window.history.replaceState({}, '', '/pms/pm/project/list?tabs=participate')
    state.active.fullPath = '/pms/pm/project/list?tabs=participate'
    state.instance.vnode.key = state.active.fullPath
    expect(createPageFacts().readPageRoute()).toBe('/pms/pm/project/list')
    // The same recovery is used to bind generic query controls to their live page.
    state.element.innerHTML = '<form><input></form>'
    const form = state.element.querySelector('form')!
    const owner = {...state.instance, type: {name: 'ElForm'}, props: {model: {pageNo: 1, pageSize: 10}},
      subTree: {type: 'form', el: form}, vnode: {ctx: state.instance}, parent: state.instance}
    state.instance.subTree.children = [{component: owner}] as unknown as NativeVNode[]
    expect(readElementPageControl(form.querySelector('input'))).toEqual({kind: 'query'})
  })
  it('does not recover a cached, ambiguous, unkeyed or incomplete active page', () => {
    for (const change of [
      (state: ReturnType<typeof mountedPage>) => {state.instance.isDeactivated = true},
      (state: ReturnType<typeof mountedPage>) => {state.instance.vnode.key = '/another'},
      (state: ReturnType<typeof mountedPage>) => {state.keepAlive.subTree = {type: 'div', el: state.element}},
      (state: ReturnType<typeof mountedPage>) => {state.app._container!._vnode.suspense = {pendingBranch: {}}},
      (state: ReturnType<typeof mountedPage>) => {state.appView.subTree.children!.push(...Array.from({length: 64}, () => ({})))},
      (state: ReturnType<typeof mountedPage>) => {
        const other = {...state.instance, vnode: {...state.instance.vnode}}
        other.vnode.component = other
        state.keepAlive.subTree = {children: [state.instance.vnode, other.vnode]}
      },
    ]) {
      const state = mountedPage(); delete state.matched.instances; change(state)
      expect(createPageFacts().readPageRoute()).toBeUndefined()
    }
  })
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
  it('resolves a query label only to its unique same-item accessible select', () => {
    const state=mountedPage('/erp/sale/order', 'SaleOrder')
    state.element.innerHTML='<form><div class="el-form-item"><label for="state-choice">State</label>' +
      '<div class="el-select__wrapper"><input id="state-choice" role="combobox" aria-haspopup="listbox" readonly></div></div></form>'
    const form=state.element.querySelector('form')!, itemRoot=form.firstElementChild!
    const label=form.querySelector('label')!, input=form.querySelector('input')!
    const item={...state.instance,type:{name:'ElFormItem'},props:{prop:'status'},subTree:{type:'div',el:itemRoot}}
    const component={...state.instance,type:{name:'ElForm'},props:{model:{pageNo:1,pageSize:10}},
      subTree:{type:'form',el:form,children:[{component:item}]},vnode:{ctx:state.instance},parent:state.instance}
    state.instance.subTree.children=[{component} as unknown as NativeVNode]
    expect(nativeControlElement(label)).toBe(input)
    Object.assign(component.props.model, {status: 7})
    const select=form.querySelector('.el-select__wrapper')!
    select.classList.add('el-select')
    input.setAttribute('aria-controls','synthetic-options')
    expect(readElementPageControl(input)).toEqual({kind:'query',field:'status',queryValue:'7'})
    Object.assign(component.props.model, {status: 'private-text'})
    expect(readElementPageControl(input)).toEqual({kind:'query',field:'status'})
    input.disabled=true
    expect(nativeControlElement(label)).toBe(label)
    input.disabled=false
    form.append(input.cloneNode())
    expect(nativeControlElement(label)).toBe(label)
    form.lastChild!.remove()
    component.vnode.ctx={} as typeof state.instance
    expect(nativeControlElement(label)).toBe(label)
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
  it('names the rows-per-page select of the active pagination and nothing else as page size', () => {
    const state=mountedPage('/erp/sale/order', 'SaleOrder')
    state.element.innerHTML='<div class="el-pagination"><span class="el-pagination__sizes">'
      +'<div class="el-select"><div class="el-select__wrapper"><span id="inside">10</span>'
      +'<input role="combobox"></div></div></span><ul><li class="number">2</li></ul></div>'
      +'<div class="el-select"><input role="combobox" id="elsewhere"></div>'
    const root=state.element.firstElementChild!
    const component={...state.instance,type:{name:'ElPagination'},props:{currentPage:1,pageSize:10,total:25},
      subTree:{type:'div',el:root},parent:state.instance}
    state.instance.subTree.children=[{component}] as unknown as NativeVNode[]
    const input=root.querySelector('input')!
    expect(readElementPageControl(input)).toEqual({kind:'page_size',pageSize:10})
    expect(readElementPageControl(root.querySelector('.el-select__wrapper'))).toEqual({kind:'page_size',pageSize:10})
    expect(readElementPageControl(root.querySelector('#inside'))).toBeUndefined()
    expect(readElementPageControl(root.querySelector('li'))).toEqual({kind:'pagination',page:2,pageSize:10})
    expect(readElementPageControl(state.element.querySelector('#elsewhere'))).toBeUndefined()
    input.setAttribute('aria-disabled','true')
    expect(readElementPageControl(input)).toBeUndefined()
    input.removeAttribute('aria-disabled')
    component.isUnmounted=true
    expect(readElementPageControl(input)).toBeUndefined()
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
  function tableRows(state: ReturnType<typeof mountedPage>, count: number): NativeVNode[] {
    return Array.from({length: count}, () => ({type: 'tr', children: Array.from({length: 8}, () => ({
      type: 'td', children: [{type: 'span'}, {component: {
        type: {name: 'FieldCell'}, parent: state.instance,
        isMounted: true, isUnmounted: false, isDeactivated: false,
        appContext: state.instance.appContext, subTree: {type: 'span'},
      }}],
    }))}))
  }
  it('proves a rendered table with component wrappers beyond four thousand VNodes', () => {
    const state = mountedPage()
    // 128 rows with eight wrapped cells have 4225 live VNodes including the root.
    state.instance.subTree.children = tableRows(state, 128)
    expect(createPageFacts().readPageRoute()).toBe('/system/role')
  })
  it('proves arrival on a list of a few hundred rows and still refuses a tree beyond its bound', () => {
    const state = mountedPage()
    // Fifty or a hundred rows per page is an ordinary list; it used to exceed the bound.
    state.instance.subTree.children = tableRows(state, 256)
    expect(createPageFacts().readPageRoute()).toBe('/system/role')
    state.instance.subTree.children = tableRows(state, 4096)
    expect(createPageFacts().readPageRoute()).toBeUndefined()
  })
  it('does not truncate a blocking component after four thousand otherwise live VNodes', () => {
    const state = mountedPage()
    const blocker: NativeVNode = {component: {...state.instance, type: {name: 'Error'}}}
    // The stack visits the final children first, reaching this blocker after all rows.
    state.instance.subTree.children = [blocker, ...tableRows(state, 128)]
    expect(createPageFacts().readPageRoute()).toBeUndefined()
  })
  it('refuses custom keys, pending suspense, missing mount state and oversized render trees', () => {
    for (const change of [
      (state: ReturnType<typeof mountedPage>) => {state.instance.vnode.key = 'shared-view-key'},
      (state: ReturnType<typeof mountedPage>) => {state.routerView.subTree.suspense = {pendingBranch: {}}},
      (state: ReturnType<typeof mountedPage>) => {delete state.instance.isMounted},
      (state: ReturnType<typeof mountedPage>) => {state.instance.subTree.children = Array.from({length: 65536}, () => ({type: 'div'}))},
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

  it('moves only through the owned adjacent native month controls', () => {
    document.body.innerHTML = '<div class="el-date-editor--daterange"><input class="el-range-input" aria-controls="calendar" aria-haspopup="dialog"></div>'
      + '<div id="calendar" role="dialog">'
      + '<div class="el-date-range-picker__content is-left"><div class="el-date-range-picker__header">'
      + '<button class="arrow-left" aria-label="上个月"></button>'
      + '<span role="button">2026 年</span><span role="button">10 月</span></div></div>'
      + '<div class="el-date-range-picker__content is-right"><div class="el-date-range-picker__header">'
      + '<button class="arrow-right" aria-label="下个月"></button>'
      + '<span role="button">2026 年</span><span role="button">11 月</span></div></div></div>'
    const input = document.querySelector('input')!
    expect(nativeRangeDateStep(input, '2026-09-01')).toBe(document.querySelector('.arrow-left'))
    expect(nativeRangeDateStep(input, '2026-12-01')).toBe(document.querySelector('.arrow-right'))
    expect(nativeRangeDateStep(input, '2026-09-31')).toBeNull()
    expect(nativeRangeDateStep(input, '2026-10-01')).toBeNull()
    document.querySelector('.arrow-left')!.setAttribute('disabled', '')
    expect(nativeRangeDateStep(input, '2026-09-01')).toBeNull()
    input.setAttribute('aria-controls', 'other')
    expect(nativeRangeDateStep(input, '2026-12-01')).toBeNull()
  })
})

describe('applied query facets for target settlement', () => {
  it('reports visible query-form labels only while an applied read exists', async () => {
    pageTable(mountedPage('/mall/trade/order', 'TradeOrder'), [{id: 1}])
    const facts = createPageFacts()
    // No applied read yet: nothing may be claimed as an applied condition.
    document.body.insertAdjacentHTML('beforeend',
      '<div class="el-form-item"><label class="el-form-item__label">订单状态</label>'
      + '<div class="el-select__wrapper"><div class="el-select__selected-item">待支付</div></div></div>'
      + '<div class="el-form-item"><label class="el-form-item__label">创建时间</label>'
      + '<input placeholder="请选择日期"></div>')
    for (const element of document.querySelectorAll('*')) {
      element.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    }
    expect(facts.readPageFacets()).toEqual([])
    const url = '/admin-api/mall/trade/order/page?pageNo=1&pageSize=10&status=20'
    await facts.begin(url)!(200, {code:0, data:{list:[{id:1}], total:1}})
    const facets = facts.readPageFacets()
    expect(facets).toContainEqual({kind:'filter', name:'订单状态'})
    // An empty default control is not an applied condition.
    expect(facets).not.toContainEqual({kind:'filter', name:'创建时间'})
  })
})

describe('the application opens its own page through its router, in this document', () => {
  /** A router and a tab store as the application has them, over an address that is only a value. */
  function application({redirect = true, tabs = true, refuses = false, arrives = true, over = [] as readonly object[]} = {}) {
    const calls: string[] = []
    const location = {href: 'https://synthetic.test/crm/customer', origin: 'https://synthetic.test',
      pathname: '/crm/customer', search: '', hash: ''}
    const current = {value: {fullPath: '/crm/customer'}}
    const show = (fullPath: string) => {
      const url = new URL(fullPath, location.origin)
      Object.assign(location, {href: url.href, pathname: url.pathname, search: url.search})
      current.value = {fullPath}
    }
    const router = {
      currentRoute: current,
      resolve(to: string | {path: string; query?: Record<string, string>}) {
        if (typeof to === 'string') {
          const url = new URL(to, location.origin)
          return {fullPath: url.pathname + url.search, path: url.pathname,
            query: Object.fromEntries(url.searchParams), name: 'Page', matched: [{}]}
        }
        const search = new URLSearchParams(to.query || {}).toString()
        return {fullPath: to.path + (search ? '?' + search : ''), path: to.path, query: to.query || {},
          name: redirect && to.path.startsWith('/redirect/') ? 'Redirect' : 'NotFound', matched: [{}]}
      },
      async push(to: {fullPath: string; path: string}) {
        calls.push('push ' + to.fullPath)
        if (refuses) return {type: 4}
        show(to.fullPath)
        // The application's redirect view replaces itself with the page it was asked for.
        if (arrives && to.fullPath.startsWith('/redirect/')) {
          setTimeout(() => show(to.fullPath.slice('/redirect'.length)), 10)
        }
        return undefined
      },
    }
    const store = tabs ? {delAllViews: () => { calls.push('drop kept pages') }} : undefined
    const doc = {querySelectorAll: (selector: string) => selector === '.el-overlay, .el-image-viewer__wrapper' ? over : [],
      getElementById: (id: string) => id === 'app' ? {__vue_app__: {config: {globalProperties: {
        $router: router, $pinia: {_s: new Map(store ? [['tagsView', store]] : [])}}}}} : null}
    return {calls, location, options: {view: {location, setTimeout}, doc, mounted: () => true}}
  }

  it('drops the kept pages, enters through the redirect route and says yes once the page is shown', async () => {
    const app = application()
    await expect(openPage('https://synthetic.test/system/post?status=0', app.options)).resolves.toBe(true)
    expect(app.calls).toEqual(['drop kept pages', 'push /redirect/system/post?status=0'])
    expect(app.location.pathname + app.location.search).toBe('/system/post?status=0')
  })
  it('opens an answer target through the SPA and uses the reserved tab if the router refuses', async () => {
    const target = 'https://synthetic.test/system/post'
    for (const refuses of [false, true]) {
      const app = application({refuses})
      const tab = {closed: false, opener: window as Window | null,
        close: vi.fn(), location: {replace: vi.fn()}}
      const view = {open: vi.fn(() => tab)} as unknown as Window
      expect(tryOpenAnswerInPlace(target, url => openPage(url, app.options),
        app.options.doc as unknown as Document, view)).toBe(true)
      if (refuses) {
        await vi.waitFor(() => expect(tab.location.replace).toHaveBeenCalledWith(target))
        expect(tab.close).not.toHaveBeenCalled()
      } else {
        await vi.waitFor(() => expect(tab.close).toHaveBeenCalledOnce())
        expect(tab.location.replace).not.toHaveBeenCalled()
        expect(app.location.pathname).toBe('/system/post')
      }
      expect(tab.opener).toBeNull()
    }
  })
  it('leaves the page to be loaded when there is nothing to ask, while idempotently succeeding on same route or direct push without redirect', async () => {
    for (const [url, options] of [
      ['https://synthetic.test/system/post', {tabs: false}],       // no tab store: a fresh page cannot be promised
      ['https://synthetic.test/system/post#part', {}],             // the redirect route carries no fragment
      ['https://elsewhere.invalid/system/post', {}],
    ] as const) {
      const app = application(options)
      await expect(openPage(url, app.options)).resolves.toBe(false)
      expect(app.calls).toEqual([])
    }
    // Same fullPath already shown: succeed idempotently without reloading or destroying main-frame state
    const sameApp = application({})
    await expect(openPage('https://synthetic.test/crm/customer', sameApp.options)).resolves.toBe(true)
    expect(sameApp.calls).toEqual([])
    // No /redirect route: push wanted route directly via Vue Router
    const noRedirectApp = application({redirect: false})
    await expect(openPage('https://synthetic.test/system/post', noRedirectApp.options)).resolves.toBe(true)
    expect(noRedirectApp.calls).toEqual(['drop kept pages', 'push /system/post'])
    expect(noRedirectApp.location.pathname).toBe('/system/post')
    await expect(openPage('/system/post', {view: window, doc: document})).resolves.toBe(false)   // no application mounted
  })
  it('a closed dialog still in the document does not stop it', async () => {
    const app = application({over: [{getBoundingClientRect: () => ({width: 0, height: 0})}]})
    await expect(openPage('https://synthetic.test/system/post', app.options)).resolves.toBe(true)
  })
  it('uses the native router with an open dialog, leaving overlay readiness to fresh observations', async () => {
    const overlay = document.createElement('div')
    overlay.getBoundingClientRect = () => ({width: 1440, height: 900} as DOMRect)
    document.body.append(overlay)
    try {
      const app = application({over: [overlay]})
      await expect(openPage('https://synthetic.test/system/post', app.options)).resolves.toBe(true)
      expect(app.calls).toEqual(['drop kept pages', 'push /redirect/system/post'])
      // Routing does not click or save in the surviving overlay, or prove readiness.
      const facts = createPageFacts(app.options)
      expect(facts.readPageBlocker()).toBe(true)
      overlay.setAttribute('hidden', '')
      expect(facts.readPageBlocker()).toBe(false)
      // Once dismissed, a new display of that component is not a carried layer.
      overlay.removeAttribute('hidden')
      expect(facts.readPageBlocker()).toBe(false)
    } finally {overlay.remove()}
  })
  it('waits, within a bound, for the page itself and not only for its address', async () => {
    const app = application()
    let asked = 0
    const opened = openPage('https://synthetic.test/system/post', {...app.options, mounted: () => ++asked > 3})
    await expect(opened).resolves.toBe(true)
    expect(asked).toBe(4)
    // A page that never reports itself built is still open: the wait ends, the answer stands.
    const slow = application()
    const realNow = Date.now
    let now = realNow()
    Date.now = () => (now += 400)
    try {
      await expect(openPage('https://synthetic.test/system/post', {...slow.options, mounted: () => false}))
        .resolves.toBe(true)
    } finally { Date.now = realNow }
  })
  it('says no when the router refuses, or the page never arrives', async () => {
    const refused = application({refuses: true})
    await expect(openPage('https://synthetic.test/system/post', refused.options)).resolves.toBe(false)
    const lost = application({arrives: false})
    const realNow = Date.now
    let now = realNow()
    Date.now = () => (now += 500)
    try {
      await expect(openPage('https://synthetic.test/system/post', lost.options)).resolves.toBe(false)
    } finally { Date.now = realNow }
    expect(lost.calls).toEqual(['drop kept pages', 'push /redirect/system/post'])
  })
  it('succeeds when a destination page canonicalizes default query parameters on mount without reloading', async () => {
    const calls: string[] = []
    const location = {href: 'https://synthetic.test/index', origin: 'https://synthetic.test',
      pathname: '/index', search: '', hash: ''}
    const current = {value: {fullPath: '/index'}}
    const show = (fullPath: string) => {
      const url = new URL(fullPath, location.origin)
      Object.assign(location, {href: url.href, pathname: url.pathname, search: url.search})
      current.value = {fullPath}
    }
    const router = {
      currentRoute: current,
      resolve(to: string | {path: string; query?: Record<string, string>}) {
        if (typeof to === 'string') {
          const url = new URL(to, location.origin)
          return {fullPath: url.pathname + url.search, path: url.pathname,
            query: Object.fromEntries(url.searchParams), name: 'Page', matched: [{}]}
        }
        const search = new URLSearchParams(to.query || {}).toString()
        return {fullPath: to.path + (search ? '?' + search : ''), path: to.path, query: to.query || {},
          name: to.path.startsWith('/redirect/') ? 'Redirect' : 'Page', matched: [{}]}
      },
      async push(to: {fullPath: string; path: string}) {
        calls.push('push ' + to.fullPath)
        show(to.fullPath)
        if (to.fullPath === '/redirect/pms/pm/project/list') {
          setTimeout(() => show('/pms/pm/project/list?tabs=participate'), 10)
        } else if (to.fullPath.startsWith('/redirect/')) {
          setTimeout(() => show(to.fullPath.slice('/redirect'.length)), 10)
        }
        return undefined
      },
    }
    const store = {delAllViews: () => { calls.push('drop kept pages') }}
    const doc = {querySelectorAll: () => [],
      getElementById: (id: string) => id === 'app' ? {__vue_app__: {config: {globalProperties: {
        $router: router, $pinia: {_s: new Map([['tagsView', store]])}}}}} : null}
    const opts = {view: {location, setTimeout}, doc, mounted: () => true}
    // Initial navigation where onMounted immediately adds ?tabs=participate
    await expect(openPage('https://synthetic.test/pms/pm/project/list', opts)).resolves.toBe(true)
    expect(location.pathname + location.search).toBe('/pms/pm/project/list?tabs=participate')
    // Re-navigating to bare /pms/pm/project/list when already on ?tabs=participate also stays in SPA
    await expect(openPage('https://synthetic.test/pms/pm/project/list', opts)).resolves.toBe(true)
    // Explicit mismatched query parameter must fail when overridden to a conflicting value
    const realNow = Date.now
    let now = realNow()
    Date.now = () => (now += 1500)
    try {
      router.push = async (to: {fullPath: string; path: string}) => {
        show(to.fullPath)
        setTimeout(() => show('/pms/pm/project/list?tabs=participate'), 5)
        return undefined
      }
      await expect(openPage('https://synthetic.test/pms/pm/project/list?tabs=owner', opts)).resolves.toBe(false)
    } finally { Date.now = realNow }
  })
})

describe('native editor mode is current application state', () => {
  it('reads and restores a mounted full-page form, revalidating and checking its precondition', async () => {
    const state = mountedPage('/synthetic/editor', 'Editor')
    const form = document.createElement('form'); form.className = 'el-form'
    const input = document.createElement('input'); form.append(input); state.element.append(form)
    form.getBoundingClientRect = () => ({width: 500, height: 300} as DOMRect)
    const model = {id: 42, accountId: 8, hidden: 'default'}
    const validate = vi.fn(async () => true)
    const native = {...state.instance, type: {name: 'ElForm'}, props: {model},
      subTree: {type: 'form', el: form}, vnode: {ctx: state.instance}, exposed: {validate}}
    state.instance.subTree.children = [{component: native}] as unknown as NativeVNode[]
    expect(readElementFormModel(input)).toMatchObject({mode: 'update', record_id: '42', model})
    const request = {expected: {...model}, values: {accountId: 3}}
    expect(await restoreElementFormModel(input, request)).toBe(true)
    expect(model.accountId).toBe(3)
    expect(validate).toHaveBeenCalledOnce()
    expect(await restoreElementFormModel(input, request)).toBe(false)
    expect(await restoreElementFormModel(input, {expected: {...model}, values: {id: 9}})).toBe(false)
    validate.mockRejectedValueOnce(new Error('application validation'))
    expect(await restoreElementFormModel(input, {expected: {...model}, values: {accountId: 2}})).toBe(false)
    expect(model.accountId).toBe(3)
  })
  it('restores sorted ChannelServer commands and rolls back the complete before-image on failure', async () => {
    const state = mountedPage('/synthetic/fees/editor', 'SyntheticFees')
    const form = document.createElement('form'); form.className = 'el-form'
    const input = document.createElement('input'); form.append(input); state.element.append(form)
    form.getBoundingClientRect = () => ({width: 500, height: 300} as DOMRect)
    const model = {z: {two: 2, one: 1}, id: 42, accountId: 8, lines: [2, 1]}
    const validate = vi.fn(async () => true)
    const native = {...state.instance, type: {name: 'ElForm'}, props: {model},
      subTree: {type: 'form', el: form}, vnode: {ctx: state.instance}, exposed: {validate}}
    state.instance.subTree.children = [{component: native}] as unknown as NativeVNode[]
    const wire = (expected: unknown, values = {accountId: 3}) => JSON.parse(execFileSync(
      resolve(process.cwd(), '../.venv/bin/python'), ['-m', 'web_operation_agent.tests.transport.submit_wire'],
      {cwd: resolve(process.cwd(), '..'), input: JSON.stringify({kind: 'restore', ref: 'e1', expected, values}),
        env: {...process.env, PYTHONPATH: 'src:examples/yudao/src'}, encoding: 'utf8'}))
    const request = wire(model)
    expect(Object.keys(request.expected)).not.toEqual(Object.keys(model))
    expect(await restoreElementFormModel(input, request)).toBe(true)
    expect(model.accountId).toBe(3)
    for (const expected of [{...model, accountId: '3'}, {...model, lines: [1, 2]},
      {id: 42, accountId: 3, lines: [2, 1]}, {...model, extra: null}]) {
      expect(await restoreElementFormModel(input, wire(expected))).toBe(false)
    }
    const before = structuredClone(model)
    validate.mockImplementationOnce(async () => { model.z.one = 99; throw new Error('invalid') })
    expect(await restoreElementFormModel(input, wire(model, {accountId: 7}))).toBe(false)
    expect(model).toEqual(before)
    validate.mockImplementationOnce(async () => { model.accountId = 12; return true })
    expect(await restoreElementFormModel(input, wire(model, {accountId: 7}))).toBe(false)
    expect(model).toEqual(before)
    expect(sameFormStructure({a: null}, {})).toBe(false)
  // Seven interpreter spawns per run: under a loaded machine each cold start alone
  // eats the default timeout, so this case gets its own.
  }, 30_000)

  it('supports allowInvalid + unset + clearValidate for empty create-form undo even with a password sibling', async () => {
    const state = mountedPage('/synthetic/create-user', 'CreateUser')
    const form = document.createElement('form'); form.className = 'el-form'
    const input = document.createElement('input'); form.append(input); state.element.append(form)
    form.getBoundingClientRect = () => ({width: 500, height: 300} as DOMRect)
    const model: Record<string, unknown> = {customerId: 7, nickname: '顾溪', password: 'secret-value'}
    const validate = vi.fn(async () => false)
    const clearValidate = vi.fn()
    const native = {...state.instance, type: {name: 'ElForm'}, props: {model},
      subTree: {type: 'form', el: form}, vnode: {ctx: state.instance}, exposed: {validate, clearValidate}}
    state.instance.subTree.children = [{component: native}] as unknown as NativeVNode[]

    const readBefore = readElementFormModel(input)
    expect(readBefore).toMatchObject({withheld: ['password'], mode: 'create'})

    // Default (non-undo) path still refuses when withheld or invalid
    expect(await restoreElementFormModel(input, {expected: readBefore!.model, values: {nickname: ''}})).toBe(false)
    expect(model.password).toBe('secret-value')

    // Undo path with allowInvalid + unset clears validation and preserves password
    expect(await restoreElementFormModel(input, {
      expected: readBefore!.model,
      values: {nickname: ''},
      unset: ['customerId'],
      allowInvalid: true,
    })).toBe(true)
    expect(model.nickname).toBe('')
    expect(model.customerId).toBeUndefined()
    expect(model.password).toBe('secret-value')
    expect(clearValidate).toHaveBeenCalledWith(['nickname', 'customerId'])
    expect(validate).not.toHaveBeenCalled()

    // Trying to unset or overwrite a secret key is still rejected
    expect(await restoreElementFormModel(input, {
      expected: {nickname: ''},
      values: {},
      unset: ['password'],
      allowInvalid: true,
    })).toBe(false)
  })
  it('recognizes the real query form despite more than 8192 unrelated option VNodes', () => {
    const state = mountedPage('/synthetic/inventory', 'SyntheticInventory')
    state.element.innerHTML = '<form class="el-form"><div class="el-form-item"><input></div></form><aside></aside>'
    const form = state.element.querySelector('form')!, itemRoot = form.firstElementChild!
    const input = form.querySelector('input')!, aside = state.element.querySelector('aside')!
    const item = {...state.instance, type: {name: 'ElFormItem'}, props: {prop: 'reference'},
      subTree: {type: 'div', el: itemRoot}}
    const options = Array.from({length: 24000}, () => ({type: 'option', el: aside}))
    const component = {...state.instance, type: {name: 'ElForm'},
      props: {model: {pageNo: 1, pageSize: 10, reference: 'SYN-17'}},
      subTree: {type: 'form', el: form, children: [{component: item}, ...options]},
      vnode: {ctx: state.instance}, parent: state.instance}
    state.instance.subTree.children = [{component}, {type: 'aside', el: aside, children: options}] as unknown as NativeVNode[]
    expect(readElementPageControl(input)).toEqual({kind: 'query', field: 'reference'})
    const second = document.createElement('form'); second.className = 'el-form'; state.element.append(second)
    state.instance.subTree.children.push({component: {...component, subTree: {type: 'form', el: second}}} as unknown as NativeVNode)
    expect(readElementPageControl(input)).toBeUndefined()
  })
  it('requires the mounted native form model and rendered action to agree, including teleported dialogs', () => {
    const state=mountedPage('/system/user','SystemUser')
    const dialog=document.createElement('div');dialog.setAttribute('role','dialog')
    dialog.innerHTML='<div class="el-dialog__header">新增</div><form class="el-form"><input></form>'
    document.body.append(dialog)
    const form=dialog.querySelector('form')!,input=form.querySelector('input')!
    form.getBoundingClientRect=()=>({width:500,height:300} as DOMRect)
    const model:{id?:number}={}
    const native={...state.instance,type:{name:'ElForm'},props:{model},
      subTree:{type:'form',el:form},vnode:{ctx:state.instance}}
    state.instance.subTree.children=[{component:native}] as unknown as NativeVNode[]
    expect(readElementPageControl(input)).toEqual({kind:'form',mode:'create',recordId:null})
    const dateRoot=document.createElement('div');dateRoot.className='el-date-editor'
    const dateInput=document.createElement('input');dateRoot.append(dateInput);form.append(dateRoot)
    const picker={...state.instance,type:{name:'ElDatePicker'},props:{type:'datetime',format:undefined as string|undefined},
      parent:native,subTree:{type:Symbol('fragment'),el:document.createComment('fragment')}}
    const dateControl={...state.instance,type:{name:'ElInput'},props:{type:'text'},
      parent:picker,subTree:{type:'div',el:dateRoot}}
    native.subTree={type:'form',el:form,children:[{component:dateControl}]} as typeof native.subTree
    expect(readElementPageControl(dateInput)).toEqual({kind:'form',mode:'create',recordId:null,inputKind:'datetime'})
    picker.props.type='date'
    expect(readElementPageControl(dateInput)).toMatchObject({inputKind:'date'})
    picker.props.format='DD/MM/YYYY'
    expect(readElementPageControl(dateInput)).not.toHaveProperty('inputKind')
    picker.props.format=undefined;picker.props.type='datetimerange'
    expect(readElementPageControl(dateInput)).not.toHaveProperty('inputKind')
    picker.props.type='datetime';picker.isDeactivated=true
    expect(readElementPageControl(dateInput)).not.toHaveProperty('inputKind')
    picker.isDeactivated=false;dateControl.parent=native as unknown as typeof picker
    expect(readElementPageControl(dateInput)).not.toHaveProperty('inputKind') // CSS alone supplies no precision.
    dateRoot.remove()
    const lines=document.createElement('form');lines.className='el-form'
    const quantity=document.createElement('input');lines.append(quantity);form.append(lines)
    lines.getBoundingClientRect=()=>({width:400,height:100} as DOMRect)
    const lineForm={...native,props:{model:[]},subTree:{type:'form',el:lines}}
    native.subTree={type:'form',el:form,children:[{component:lineForm}]} as typeof native.subTree
    expect(readElementPageControl(quantity)).toEqual({kind:'form',mode:'create',recordId:null})
    model.id=42
    expect(readElementPageControl(input)).toBeUndefined()
    expect(readElementPageControl(quantity)).toBeUndefined()
    dialog.querySelector('.el-dialog__header')!.textContent='编辑'
    expect(readElementPageControl(input)).toEqual({kind:'form',mode:'update',recordId:'42'})
    expect(readElementPageControl(quantity)).toEqual({kind:'form',mode:'update',recordId:'42'})
    const sibling=document.createElement('form');sibling.className='el-form';dialog.append(sibling)
    sibling.getBoundingClientRect=()=>({width:400,height:100} as DOMRect)
    expect(readElementPageControl(quantity)).toBeUndefined() // Two independent editors remain ambiguous.
    sibling.remove();lines.remove()
    delete model.id
    expect(readElementPageControl(input)).toBeUndefined()
    model.id=42;native.isDeactivated=true
    expect(readElementPageControl(input)).toBeUndefined()
    native.isDeactivated=false
    const duplicate={...native}
    state.instance.subTree.children=[{component:native},{component:duplicate}] as unknown as NativeVNode[]
    expect(readElementPageControl(input)).toBeUndefined()
    state.instance.subTree.children=[]
    expect(readElementPageControl(input)).toBeUndefined() // DOM title/id alone do not attest an editor.
    dialog.remove()
  })
  it('classifies modelless inline filter toolbars as query controls and strips float tail noise on form models', () => {
    const state = mountedPage('/mall/statistics/product', 'ProductStatistics')
    state.element.innerHTML = '<form class="el-form el-form--inline"><input></form>'
    const inlineForm = state.element.querySelector('form')!, filterInput = inlineForm.querySelector('input')!
    const inlineOwner = {...state.instance, type: {name: 'ElForm'}, props: {},
      subTree: {type: 'form', el: inlineForm}, vnode: {ctx: state.instance}, parent: state.instance}
    state.instance.subTree.children = [{component: inlineOwner}] as unknown as NativeVNode[]
    expect(readElementPageControl(filterInput)).toEqual({kind: 'query'})

    const dialog = document.createElement('div'); dialog.setAttribute('role', 'dialog')
    dialog.innerHTML = '<header class="el-dialog__header">编辑</header><form class="el-form"><input></form>'
    const form = dialog.querySelector('form')!, input = form.querySelector('input')!
    for (const node of [dialog, form, input]) node.getBoundingClientRect = () => ({width: 400, height: 200} as DOMRect)
    state.element.append(dialog)
    const model = {id: 99, totalPrice: 1370.13 + 1381.43, inTime: 1795485600000}
    const formOwner = {...state.instance, type: {name: 'ElForm'}, props: {model},
      subTree: {type: 'form', el: form}, vnode: {ctx: state.instance}, parent: state.instance}
    state.instance.subTree.children = [{component: formOwner}] as unknown as NativeVNode[]
    const snapshot = readElementFormModel(input) as {model?: Record<string, unknown>}
    expect(snapshot.model).toEqual({id: 99, totalPrice: 2751.56, inTime: 1795485600000})
    dialog.remove()
  })
  it('retains bounded non-sensitive list and page query parameters across generic XHR routes', async () => {
    pageTable(mountedPage('/system/dept', 'SystemDept'), [{id: 101}])
    class Request extends EventTarget {
      responseType = 'json'; response = {code: 0, data: [{id: 101}]}; status = 200
      open() {}
      send() { this.dispatchEvent(new Event('loadend')) }
    }
    const facts = installPageFacts({location: window.location, fetch: window.fetch,
      XMLHttpRequest: Request, crypto: window.crypto}, document)
    const path = '/admin-api/system/dept/list'
    try {
      const req = new Request()
      // @ts-expect-error Installed XHR wrapper accepts URL strings.
      req.open('GET', `${path}?status=0&createTime%5B0%5D=2026-10-01%2000%3A00%3A00`)
      req.send()
      await vi.waitFor(() => {
        expect(facts.readPageQuery()).toEqual({
          path,
          parameters: {status: '0', 'createTime[0]': '2026-10-01 00:00:00'},
        })
      })
      const secretReq = new Request()
      // @ts-expect-error Installed XHR wrapper accepts URL strings.
      secretReq.open('GET', `${path}?status=0&accessToken=secret`)
      secretReq.send()
      expect(facts.readPageQuery()).toBeUndefined()
    } finally { facts.dispose() }
  })
  it('supports hyphenated list paths, benign keyword filters, empty-param pruning, sensitive key coverage, tree rows, and unpaged id-free lists', async () => {
    const state = mountedPage('/synthetic/items', 'SyntheticItems')
    const tableProps = pageTable(state, [{id: 1, children: [{id: 2}]}])
    class Request extends EventTarget {
      responseType = 'json'
      response: unknown = {code: 0, data: {list: [{id: 1}, {id: 2}], total: 2}}
      status = 200
      open() {}
      send() { this.dispatchEvent(new Event('loadend')) }
    }
    const facts = installPageFacts({location: window.location, fetch: window.fetch,
      XMLHttpRequest: Request, crypto: window.crypto}, document)
    try {
      const sendReq = (url: string, response: unknown) => {
        const req = new Request()
        req.response = response
        // @ts-expect-error Installed XHR wrapper accepts URL strings.
        req.open('GET', url)
        req.send()
      }
      // Empty params are pruned consistently even when total parameters <= 16.
      sendReq(
        '/admin-api/synthetic/item-page?pageNo=1&pageSize=10&name=&employeeKeyword=abc&status=',
        {code: 0, data: {list: [{id: 1}, {id: 2}], total: 2}},
      )
      await vi.waitFor(() => {
        expect(facts.readPageQuery()).toEqual({
          path: '/admin-api/synthetic/item-page',
          parameters: {pageNo: '1', pageSize: '10', employeeKeyword: 'abc'},
        })
      })
      const empties = Array.from({length: 14}, (_, i) => `f${i}=`).join('&')
      sendReq(
        `/admin-api/synthetic/item-page?pageNo=1&pageSize=10&alphaKeyword=abc&${empties}`,
        {code: 0, data: {list: [{id: 1}, {id: 2}], total: 2}},
      )
      await vi.waitFor(() => {
        expect(facts.readPageQuery()).toEqual({
          path: '/admin-api/synthetic/item-page',
          parameters: {pageNo: '1', pageSize: '10', alphaKeyword: 'abc'},
        })
      })
      for (const badParam of [
        'apiKey=secret-key',
        'clientKey=ck',
        'pubKey=pk',
        'keyId=1',
        'access_key_id=ak',
        'sshKey=ssh',
        'secretKeyword=abc',
        'keyKeyword=abc',
        'keywordKey=abc',
        'keywordId=1',
        'clientKey=',
      ]) {
        sendReq(`/admin-api/synthetic/item-page?pageNo=1&pageSize=10&${badParam}`, {code: 0, data: {list: [{id: 1}, {id: 2}], total: 2}})
        expect(facts.readPageQuery()).toBeUndefined()
      }
      sendReq('/admin-api/synthetic/item-simple-list?pageNo=1', {code: 0, data: [{id: 1}, {id: 2}]})
      expect(facts.readPageQuery()).toBeUndefined()

      tableProps.data = [{code: 'R1', amount: 10}]
      sendReq('/admin-api/synthetic/item-list?period=2026-01', {code: 0, data: [{code: 'R1', amount: 10}]})
      await vi.waitFor(() => {
        expect(facts.readPageQuery()).toEqual({
          path: '/admin-api/synthetic/item-list',
          parameters: {period: '2026-01'},
        })
      })
    } finally { facts.dispose() }
  })
  it('classifies body-teleported popover query controls owned by the active page query ElForm', () => {
    const state = mountedPage('/erp/purchase/order', 'ErpPurchaseOrder')
    state.element.innerHTML = '<form class="el-form"></form>'
    const queryForm = state.element.querySelector('form')!
    queryForm.getBoundingClientRect = () => ({width: 500, height: 80} as DOMRect)

    const popover = document.createElement('div')
    popover.className = 'el-popover'
    popover.innerHTML = '<div class="el-form-item"><input></div><button class="el-button">确定</button>'
    for (const node of [popover, ...popover.querySelectorAll('*')]) {
      node.getBoundingClientRect = () => ({width: 120, height: 32} as DOMRect)
    }
    document.body.append(popover)

    const itemRoot = popover.querySelector('.el-form-item')!
    const input = popover.querySelector('input')!
    const button = popover.querySelector('button')!
    const model = {pageNo: 1, pageSize: 10, name: ''}

    const formOwner = {
      ...state.instance,
      type: {name: 'ElForm'},
      props: {model},
      vnode: {ctx: state.instance},
      parent: state.instance,
      subTree: {type: 'form', el: queryForm, children: [] as NativeVNode[]},
    }
    const itemInst = {
      ...state.instance,
      type: {name: 'ElFormItem'},
      props: {prop: 'name'},
      parent: formOwner,
      subTree: {type: 'div', el: itemRoot},
    }
    const buttonInst = {
      ...state.instance,
      type: {name: 'ElButton'},
      props: {},
      parent: formOwner,
      subTree: {type: 'button', el: button},
    }
    const popoverInst = {
      ...state.instance,
      type: {name: 'ElPopover'},
      props: {},
      parent: formOwner,
      subTree: {
        type: 'div',
        el: popover,
        children: [{component: itemInst}, {component: buttonInst}] as unknown as NativeVNode[],
      },
    }
    formOwner.subTree.children = [{component: popoverInst}] as unknown as NativeVNode[]
    state.instance.subTree.children = [{component: formOwner}] as unknown as NativeVNode[]

    expect(readElementPageControl(input)).toEqual({kind: 'query', field: 'name'})
    expect(readElementPageControl(button)).toEqual({kind: 'query'})

    const strayPopover = document.createElement('div')
    strayPopover.className = 'el-popover'
    strayPopover.innerHTML = '<div class="el-form-item"><input></div><button class="el-button">确定</button>'
    for (const node of [strayPopover, ...strayPopover.querySelectorAll('*')]) {
      node.getBoundingClientRect = () => ({width: 120, height: 32} as DOMRect)
    }
    document.body.append(strayPopover)
    expect(readElementPageControl(strayPopover.querySelector('input')!)).toBeUndefined()
    expect(readElementPageControl(strayPopover.querySelector('button')!)).toBeUndefined()

    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    const dialogPopover = document.createElement('div')
    dialogPopover.className = 'el-popover'
    dialogPopover.innerHTML = '<div class="el-form-item"><input></div>'
    for (const node of [dialog, dialogPopover, ...dialogPopover.querySelectorAll('*')]) {
      node.getBoundingClientRect = () => ({width: 120, height: 32} as DOMRect)
    }
    dialog.append(dialogPopover)
    document.body.append(dialog)
    expect(readElementPageControl(dialogPopover.querySelector('input')!)).toBeUndefined()

    popover.remove()
    strayPopover.remove()
    dialog.remove()
  })
})
