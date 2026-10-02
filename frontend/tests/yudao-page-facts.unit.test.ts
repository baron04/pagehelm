import {afterAll, beforeEach, describe, expect, it} from 'vitest'
// @ts-expect-error Framework-free application module is served separately from the Runtime.
import {createPageFacts, disposeInstalledPageFacts} from '../src/integrations/yudao/page-facts.js'

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
  it('requires a successful exact-ID read and matching visible title', () => {
    const facts = createPageFacts()
    expect(facts.readPageBlocker()).toBe(true)
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
  it('retains a lazy read failure after the visible toast disappears, until a fresh successful read', () => {
    const facts = createPageFacts(); ready(facts)
    const settle = facts.begin('/admin-api/crm/contact/page-by-customer?customerId=6')!
    expect(facts.readPageBlocker()).toBe(true)
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
  it('rejects loading overlays and duplicate visible record titles', () => {
    const facts = createPageFacts(); ready(facts)
    const overlay = document.createElement('div'); overlay.className = 'el-loading-mask'
    overlay.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    document.body.append(overlay)
    expect(facts.readPageBlocker()).toBe(true)
    overlay.remove()
    const second = document.querySelector('span')!.cloneNode(true) as HTMLElement
    second.getBoundingClientRect = () => ({width: 50, height: 20} as DOMRect)
    document.body.append(second)
    expect(facts.readPageRecordRoute()).toBeUndefined()
  })
  it('ignores an old route completion and other origins or non-CRM requests', () => {
    const facts = createPageFacts()
    expect(facts.begin('https://other.test/admin-api/crm/customer/get?id=6')).toBeNull()
    expect(facts.begin('/admin-api/crm/customer/update', 'PUT')).toBeNull()
    expect(facts.begin('/admin-api/system/notify-message/get-unread-count')).toBeNull()
    const old = facts.begin('/admin-api/crm/customer/get?id=6')!
    window.history.replaceState({}, '', '/crm/customer/detail/12')
    old(200, {code: 0, data: {id: 6, name: 'Synthetic Customer'}})
    expect(facts.readPageRecordRoute()).toBeUndefined()
    expect(facts.readPageBlocker()).toBe(true)
  })
})
