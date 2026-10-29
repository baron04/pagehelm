import {describe, expect, it, vi} from 'vitest'
import {Page, pageControllerOptions, type BrowserState, type PageControllerApi}
  from '../src/runtime/page'
import {createRedactor} from '../src/runtime/redaction'
import {formInstance} from '../src/runtime/formInstance'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'

function controller(): PageControllerApi {
  return {
    selectorMap: {},
    getBrowserState: vi.fn(),
    clickElement: vi.fn(),
    inputText: vi.fn(),
    selectOption: vi.fn(),
    scroll: vi.fn(),
    dispose: vi.fn(),
  }
}

describe('adapter-issued single-choice combobox drafts', () => {
  it('verifies an unnamed line only by its exact current ref, live form instance and value', () => {
    const form = document.createElement('form')
    form.id = 'unnamed-line'
    form.innerHTML = '<input type="number" value="2">'
    document.body.append(form)
    try {
      let input = form.querySelector('input')!
      const page = new Page(controller(), () => input, createRedactor())
      const instance = formInstance(input)!
      const observed = [{ref:'e1', role:'spinbutton', name:'', tag:'input', epoch:1,
        attrs:{title:'Line 1 · Quantity', form_scope:'form:unnamed-line', form_instance:instance}}]
      const binding = {ref:'e1', field:'Line 1 · Quantity', control:'spinbutton', value:'2',
        scope:'form:unnamed-line', binding:'ref', instance}
      expect(page.verifyFormFields([binding],observed)).toBe(true)
      for (const bad of [{...binding,ref:'e2'}, {...binding,instance:'expired'}, {...binding,ref:undefined},
        {...binding,value:'3'}, {...binding,scope:'form:other'}])
        expect(page.verifyFormFields([bad],observed)).toBe(false)
      input.replaceWith(input.cloneNode() as HTMLInputElement)
      input = form.querySelector('input')!
      expect(page.verifyFormFields([binding],observed)).toBe(false)
    } finally {form.remove()}
  })
  function setup(extra = '', interrupt: () => boolean = () => false) {
    const form = document.createElement('form')
    form.id = 'choice-form'
    form.innerHTML = `<label>Owner<input role="combobox" readonly aria-haspopup="listbox"
      aria-controls="owner-options" aria-expanded="false"></label>
      <ul id="owner-options" role="listbox" hidden><li role="option" aria-selected="false">Alex</li>${extra}</ul>`
    document.body.append(form)
    const input = form.querySelector('input')!, list = form.querySelector('ul')!, option = list.querySelector('[role="option"]')!
    for (const node of [input, list, ...list.querySelectorAll('li')])
      node.getBoundingClientRect = () => ({width: 100, height: 20} as DOMRect)
    const api = controller()
    api.getBrowserState = vi.fn(async () => ({url:location.href, title:'', content:'[1]<input />\n[2]<li role=option>Alex />'}))
    api.clickElement = vi.fn(async index => {
      if (index === 1) {list.hidden = !list.hidden; input.setAttribute('aria-expanded',String(!list.hidden))}
      else {option.setAttribute('aria-selected','true'); list.hidden = true; input.setAttribute('aria-expanded','false')}
      return {success:true}
    })
    const page = new Page(api, (_, index) => index === 1 ? input : option, createRedactor(),
      undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined, interrupt)
    const observed = [{ref:'e1', role:'combobox', name:'Owner', tag:'input', epoch:1,
      attrs:{field_label:'Owner',form_scope:'form:choice-form'}}]
    const fields = [{ref:'e1', field:'owner', label:'Owner', binding:'label', scope:'form:choice-form',
      control:'combobox', value:'17', option_label:'Alex'}]
    return {form,input,list,option,api,page,observed,fields}
  }
  it('selects only an option in its own list and verifies selection rather than the filter input', async () => {
    const s = setup()
    try {
      await s.page.select('e1', '17', 'Alex')
      expect(s.api.clickElement).toHaveBeenNthCalledWith(1,1)
      expect(s.api.clickElement).toHaveBeenNthCalledWith(2,2)
      expect(s.input.value).toBe('')
      expect(s.page.verifyFormFields(s.fields,s.observed)).toBe(true)
      s.option.setAttribute('aria-selected','false')
      expect(s.page.verifyFormFields(s.fields,s.observed)).toBe(false)
      expect(() => s.page.input('e1','Alex')).toThrow(/read-only/)
    } finally {s.form.remove()}
  })
  it('closes a list that does not offer the option and says so by its own code', async () => {
    const s = setup()
    try {
      await expect(s.page.select('e1', '99', 'Nobody')).rejects.toThrow(/^option_unavailable:/)
      // Opened once to look, pressed once more to close; no option was pressed.
      expect(s.api.clickElement).toHaveBeenCalledTimes(2)
      expect(s.api.clickElement).toHaveBeenNthCalledWith(2, 1)
      expect(s.option.getAttribute('aria-selected')).toBe('false')
    } finally {s.form.remove()}
  })
  it('proves no selection even when the open popup leaves no current ref for dismissal', async () => {
    const s = setup()
    try {
      s.api.getBrowserState = vi.fn(async () => ({url:location.href, title:'', content:'[2]<li role=option>Alex />'}))
      await expect(s.page.select('e1','Missing','Missing')).rejects.toMatchObject({
        code:'option_unavailable', no_effect:true, candidate_labels:['Alex'],
      })
      expect(s.api.clickElement).toHaveBeenCalledTimes(1)
      expect(s.input.getAttribute('aria-expanded')).toBe('true')
    } finally {s.form.remove()}
  })
  it('ignores the assistant panel during selection indexing and restores its hit targets', async () => {
    const s = setup()
    const panel = document.body.appendChild(document.createElement('div'))
    panel.style.pointerEvents = 'auto'
    const events = new EventTarget()
    s.api.addEventListener = events.addEventListener.bind(events)
    s.api.removeEventListener = events.removeEventListener.bind(events)
    s.api.getBrowserState = vi.fn(async () => {
      events.dispatchEvent(new Event('beforeUpdate'))
      const content = panel.style.pointerEvents === 'none' ? '[2]<li role=option>Alex />' : ''
      events.dispatchEvent(new Event('afterUpdate'))
      return {url:location.href,title:'',content}
    })
    const page = new Page(s.api, (_, index) => index === 1 ? s.input : s.option, createRedactor(),
      undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined,
      undefined, undefined, undefined, undefined, panel)
    try {
      await page.select('e1','17','Alex')
      expect(s.api.clickElement).toHaveBeenLastCalledWith(2)
      expect(s.api.scroll).not.toHaveBeenCalled()
      expect(panel.style.pointerEvents).toBe('auto')
    } finally {s.form.remove(); panel.remove()}
  })
  it('refuses duplicate, disabled or multi-select options without choosing anything', async () => {
    for (const mode of ['duplicate','disabled','multiple']) {
      const s = setup(mode === 'duplicate' ? '<li role="option">Alex</li>' : '')
      try {
        if (mode === 'disabled') s.option.setAttribute('aria-disabled','true')
        if (mode === 'multiple') s.list.setAttribute('aria-multiselectable','true')
        await expect(s.page.select('e1','17','Alex')).rejects.toThrow()
        expect(s.api.clickElement).not.toHaveBeenCalledWith(2)
      } finally {s.form.remove()}
    }
  })
  it('closes an unavailable choice by its fresh control reference after reindexing', async () => {
    const s = setup()
    let fresh = false
    const page = new Page(s.api, (_, index) => fresh ? index === 3 ? s.input : s.option : s.input,
      createRedactor())
    try {
      s.api.getBrowserState = vi.fn(async () => {
        fresh = true
        return {url:location.href,title:'',content:'[1]<li role=option>Alex />\n[3]<input />'}
      })
      s.api.clickElement = vi.fn(async index => {
        if (!fresh && index === 1) {s.list.hidden = false; s.input.setAttribute('aria-expanded','true')}
        else if (fresh && index === 3) {s.list.hidden = true; s.input.setAttribute('aria-expanded','false')}
        else s.option.setAttribute('aria-selected','true')
        return {success:true}
      })
      await expect(page.select('e1','99','Nobody')).rejects.toMatchObject({
        code: 'option_unavailable', candidate_labels: ['Alex'], no_effect: true})
      expect(vi.mocked(s.api.clickElement).mock.calls).toEqual([[1],[3]])
      expect(s.option.getAttribute('aria-selected')).toBe('false')
    } finally {s.form.remove()}
  })
  it('proves unchanged values even when the widget does not toggle its popup closed', async () => {
    const s = setup()
    s.api.clickElement = vi.fn(async () => {
      s.list.hidden = false
      s.input.setAttribute('aria-expanded', 'true')
      return {success: true}
    })
    try {
      await expect(s.page.select('e1', '99', 'Nobody')).rejects.toMatchObject({
        code: 'option_unavailable', candidate_labels: ['Alex'], no_effect: true})
      expect(s.option.getAttribute('aria-selected')).toBe('false')
    } finally {s.form.remove()}
  })
  it('does not claim no effect when opening the list changed another local field', async () => {
    const s = setup()
    const other = document.createElement('input')
    s.form.append(other)
    s.api.clickElement = vi.fn(async () => {
      other.value = 'changed'
      s.list.hidden = !s.list.hidden
      s.input.setAttribute('aria-expanded', String(!s.list.hidden))
      return {success: true}
    })
    try {
      await expect(s.page.select('e1', '99', 'Nobody')).rejects.toMatchObject({
        code: 'option_unavailable', no_effect: false})
    } finally {s.form.remove()}
  })
  it('stops when the user intervenes between opening and choosing', async () => {
    let interrupted = false
    const s = setup('', () => interrupted)
    try {
      s.api.getBrowserState = vi.fn(async () => {interrupted = true; return {url:location.href,title:'',content:'[2]<li>Alex />'}})
      await expect(s.page.select('e1','17','Alex')).rejects.toThrow(/changed before selection/)
      expect(s.api.clickElement).toHaveBeenCalledTimes(1)
    } finally {s.form.remove()}
  })
  it('scrolls only from an indexed option in the owned menu and refreshes before choosing', async () => {
    const s = setup('<li role="option">Bob</li>')
    let scrolled = false
    const other = s.list.lastElementChild!
    const page = new Page(s.api, (_, index) => index === 1 ? s.input : index === 2 ? s.option : other, createRedactor())
    try {
      s.api.getBrowserState = vi.fn(async () => ({url:location.href,title:'',
        content:scrolled ? '[2]<li role=option>Alex />' : '[3]<li role=option>Bob />'}))
      s.api.scroll = vi.fn(async () => {scrolled = true; return {success:true}})
      await page.select('e1','17','Alex')
      expect(s.api.scroll).toHaveBeenCalledOnce()
      expect(s.api.scroll).toHaveBeenCalledWith(expect.objectContaining({index:3}))
      expect(s.api.clickElement).toHaveBeenNthCalledWith(2,2)
    } finally {s.form.remove()}
  })
  it('reaches a far option in a long menu by the distance left, whatever the scroll scales by', async () => {
    const s = setup('<li role="option">Bob</li>')
    const other = s.list.lastElementChild!
    const page = new Page(s.api, (_, index) => index === 1 ? s.input : index === 2 ? s.option : other, createRedactor())
    // The menu shows 200px; the wanted option starts 9000px below its top edge.
    s.list.style.overflowY = 'auto'
    Object.defineProperty(s.list, 'scrollHeight', {value: 12_000})
    Object.defineProperty(s.list, 'clientHeight', {value: 200})
    let top = 9000
    s.list.getBoundingClientRect = () => ({top: 0, bottom: 200, width: 100, height: 200} as DOMRect)
    s.option.getBoundingClientRect = () => ({top, bottom: top + 20, width: 100, height: 20} as DOMRect)
    const shown = () => top >= 0 && top + 20 <= 200
    try {
      s.api.getBrowserState = vi.fn(async () => ({url:location.href,title:'',
        content:shown() ? '[2]<li role=option>Alex />' : '[3]<li role=option>Bob />'}))
      s.api.scroll = vi.fn(async ({down, pixels}) => {top -= (down ? 1 : -1) * pixels / 3; return {success:true}})
      await page.select('e1','17','Alex')
      expect(vi.mocked(s.api.scroll).mock.calls.length).toBeLessThanOrEqual(3)
      expect(s.api.scroll).toHaveBeenCalledWith(expect.objectContaining({index:3, down:true}))
      expect(s.api.clickElement).toHaveBeenNthCalledWith(2,2)
    } finally {s.form.remove()}
  })
  it('observes the result of the last permitted scroll before declaring the option unavailable', async () => {
    const s = setup('<li role="option">Bob</li>')
    const other = s.list.lastElementChild!
    let moves = 0
    const page = new Page(s.api, (_, index) => index === 1 ? s.input : index === 2 ? s.option : other,
      createRedactor())
    try {
      s.api.getBrowserState = vi.fn(async () => ({url:location.href,title:'',
        content:moves === 8 ? '[2]<li role=option>Alex />' : '[3]<li role=option>Bob />'}))
      s.api.scroll = vi.fn(async () => {moves++; return {success:true}})
      await page.select('e1','17','Alex')
      expect(s.api.scroll).toHaveBeenCalledTimes(8)
      expect(s.api.clickElement).toHaveBeenLastCalledWith(2)
    } finally {s.form.remove()}
  })
})

describe('an anchored scroll moves the container that holds the anchor', () => {
  it('scrolls the nearest ancestor with room, however deep, and falls back to the page', async () => {
    const outer = document.createElement('div')
    outer.style.overflowY = 'auto'
    let inner: HTMLElement = outer
    for (let depth = 0; depth < 14; depth++) inner = inner.appendChild(document.createElement('div'))
    const anchor = inner.appendChild(document.createElement('button'))
    document.body.append(outer)
    Object.defineProperty(outer, 'scrollHeight', {value: 2000})
    Object.defineProperty(outer, 'clientHeight', {value: 500})
    const api = controller()
    api.scroll = vi.fn(async () => ({success: true}))
    const page = new Page(api, () => anchor, createRedactor())
    try {
      await page.scroll(true, 2, 'e1')
      expect(outer.scrollTop).toBe(1000)
      expect(api.scroll).not.toHaveBeenCalled()
      await page.scroll(false, 1, 'e1')
      expect(outer.scrollTop).toBe(500)
      outer.style.overflowY = 'visible'
      await page.scroll(true, 1, 'e1')
      expect(api.scroll).toHaveBeenCalledWith({down: true, numPages: 1})
    } finally {outer.remove()}
  })
})

describe('the product PageController extraction policy', () => {
  it('accepts explicit application loading while unknown blocker values fail closed', () => {
    const read = (value: unknown) => new Page(controller(), () => null, createRedactor(),
      undefined, undefined, undefined, undefined, () => value).readiness()
    expect(read('pending')).toEqual({blocked: false, pending: true})
    expect(read(false)).toEqual({blocked: false, pending: false})
    for (const value of [true, null, undefined, 'ready', 0, {}]) {
      expect(read(value)).toEqual({blocked: true, pending: false})
    }
    const failed = new Page(controller(), () => null, createRedactor(),
      undefined, undefined, undefined, undefined, () => { throw new Error('unavailable') })
    expect(failed.readiness()).toEqual({blocked: true, pending: false})
  })
  it('observes only the current viewport', () => {
    expect(pageControllerOptions([]).viewportExpansion).toBe(0)
  })

  it('includes generic accessible control state in the bounded observation', () => {
    expect(pageControllerOptions([]).includeAttributes).toEqual(
      expect.arrayContaining(['aria-selected', 'aria-checked', 'aria-expanded']))
  })
})

describe('Page.describe bounds an observation before it crosses the browser boundary', () => {
  it('projects only a bounded successful list receipt, keeping filter values as digests', () => {
    const proof = {path: '/api/contacts', query_digest: 'a'.repeat(64),
      query_fields: {domain: 'b'.repeat(64)}, revision: 2, page: 2, page_size: 10, total: 25, rows: 10}
    const read = (value: unknown, route: string | undefined = '/contacts') =>
      new Page(controller(), () => null, createRedactor(), undefined, undefined, undefined,
        undefined, undefined, undefined, () => route, undefined, undefined, undefined,
        () => value).describe({url: 'https://app.test/contacts', title: '', content: ''}, 1).observation
    expect(read(proof).page_read).toEqual(proof)
    expect(read(proof, '').page_read).toBeUndefined()
    for (const value of [null, {...proof, path: '//other/api'}, {...proof, revision: 0},
      {...proof, page: Number.MAX_SAFE_INTEGER + 1}, {...proof, rows: 11},
      {...proof, query_fields: {password: 'a'.repeat(64)}},
      {...proof, query_fields: {domain: 'raw-value'}}, {...proof, raw_query: 'private'}]) {
      expect(read(value).page_read).toBeUndefined()
    }
  })
  it('attaches application pagination facts only to observed elements with valid numeric targets', () => {
    const element = document.createElement('button')
    element.textContent = 'Next'
    document.body.append(element)
    try {
      const read = (value: unknown) => new Page(controller(), () => element, createRedactor(),
        undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined,
        undefined, undefined, undefined, () => value).describe({url: 'https://app.test/contacts',
        title: '', content: '[1]<button>Next />'}, 1).observation.elements[0]?.attrs
      expect(read({kind: 'pagination', page: 2, pageSize: 10})).toMatchObject({
        page_control: 'pagination', page_number: '2', page_size: '10'})
      for (const invalid of [{kind: 'pagination', page: 0, pageSize: 10},
        {kind: 'pagination', page: 2, pageSize: NaN}, {kind: 'submit', page: 2, pageSize: 10}]) {
        expect(read(invalid)?.page_control).toBeUndefined()
      }
    } finally { element.remove() }
  })
  it('projects the application query binding only for a currently observed control', () => {
    const element = document.createElement('input')
    document.body.append(element)
    try {
      const read = (value: unknown, present = true) => new Page(controller(),
        () => present ? element : null, createRedactor(), undefined, undefined, undefined,
        undefined, undefined, undefined, undefined, undefined, undefined, undefined,
        undefined, () => value).describe({url: 'https://app.test/contacts', title: '',
        content: '[1]<input />'}, 1).observation.elements[0]?.attrs
      expect(read({kind: 'query'})).toMatchObject({page_control: 'query'})
      expect(read({kind: 'query', field: 'searchTerm'})).toMatchObject({query_field: 'searchTerm'})
      expect(read({kind: 'query', field: 'productId', queryValue: '7'})).toMatchObject({query_value: '7'})
      for (const queryValue of ['private-text', '-1', '1'.repeat(100), {}, 7]) {
        expect(read({kind: 'query', field: 'productId', queryValue})?.query_value).toBeUndefined()
      }
      expect(read({kind: 'query', field: 'password', queryValue: '7'})?.query_value).toBeUndefined()
      for (const field of ['password', 'accessToken', '../outside', 'x'.repeat(500), 1]) {
        expect(read({kind: 'query', field})?.query_field).toBeUndefined()
      }
      expect(read({kind: 'query'}, false)?.page_control).toBeUndefined()
      for (const invalid of [null, {kind: 'submit'}, {}, 'query']) {
        expect(read(invalid)?.page_control).toBeUndefined()
      }
    } finally { element.remove() }
  })
  it('preserves a live cleared query value instead of retaining a stale value or omitting its proof', () => {
    const input = document.createElement('input')
    document.body.append(input)
    try {
      const page = new Page(controller(), () => input, createRedactor(), undefined, undefined,
        undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined,
        undefined, () => ({kind: 'query', field: 'username'}))
      const read = () => page.describe({url: 'https://app.test/users', title: '',
        content: '[1]<input value=previous />'}, 1).observation.elements[0]?.attrs
      input.value = 'account7'
      expect(read()).toMatchObject({page_control: 'query', query_field: 'username', value: 'account7'})
      input.value = ''
      expect(read()).toMatchObject({page_control: 'query', query_field: 'username', value: ''})
    } finally { input.remove() }
  })
  it('projects an application query only with page identity and intact bounded nonsecret values', () => {
    const query = {path: '/api/contacts', parameters: {domain: 'example.test'}}
    const read = (value: unknown, path: string | undefined = '/contacts') => new Page(controller(), () => null,
      createRedactor(), undefined, undefined, undefined, undefined, undefined, undefined,
      () => path, () => value).describe({url: 'https://app.test/contacts', title: '', content: ''}, 1).observation
    expect(read(query).page_query).toEqual(query)
    expect(read(query, '').page_query).toBeUndefined()
    for (const value of [null, {path: '//other/api', parameters: {}},
      {path: '/api/contacts?token=x', parameters: {}},
      {path: '/api/contacts', parameters: {token: 'private'}},
      {path: '/api/contacts', parameters: {domain: 'x'.repeat(257)}},
      {path: '/api/contacts', parameters: {domain: ['one', 'two']}}]) {
      expect(read(value).page_query).toBeUndefined()
    }
  })
  it('removes wrapper-identified credentials from both text and element projections', () => {
    const wrapper = document.createElement('div')
    wrapper.setAttribute('name', 'api_key')
    wrapper.innerHTML = '<input>'
    document.body.append(wrapper)
    try {
      const input = wrapper.querySelector('input')!
      input.value = 'private-credential'
      const page = new Page(controller(), () => input, createRedactor())
      const reading = page.describe({url: 'https://app.test/form', title: 'Form',
        content: '[2]<input type=text value=private-credential>private-credential />'}, 1)
      expect(JSON.stringify(reading.observation)).not.toContain('private-credential')
      expect(reading.observation.elements[0]?.role).toBe('password')
      expect(input.value).toBe('private-credential')
    } finally { wrapper.remove() }
  })

  it('accepts a bounded application-reported rendered view without a switch control', () => {
    const read = (value: unknown) => new Page(controller(), () => null, createRedactor(),
      undefined, undefined, undefined, undefined, undefined, () => value).describe({
      url: 'https://app.test/items', title: 'Items', content: '',
    }, 1).observation
    expect(read('List').active_view).toBe('List')
    expect(read('List').selected_controls).toEqual([])
    for (const invalid of [null, 1, 'x'.repeat(121), 'password']) {
      expect(read(invalid).active_view).toBeUndefined()
    }
  })
  it('accepts only a bounded application record route as an observation', () => {
    const make = (route: unknown) => new Page(controller(), () => null,
      createRedactor(), undefined, undefined, undefined, () => route).describe({
        url: 'https://app.test/items/4', title: 'Item', content: '',
      }, 1).observation.record_route
    expect(make('/items/4')).toBe('/items/4')
    for (const bad of ['//other.test/x', '/items/4?token=x', '/a/../b',
      '/items/4#tab', '/items/4 password']) expect(make(bad)).toBeUndefined()
    expect(make(null)).toBeUndefined()
  })
  it('publishes only an explicit bounded application main-page identity', () => {
    const read = (hook?: () => unknown) => new Page(controller(), () => null,
      createRedactor(), undefined, undefined, undefined, undefined, undefined, undefined,
      hook).describe({url: 'https://app.test/help', title: 'Help',
        content: '[1]<a href=/contacts>Header menu />'}, 1).observation
    expect(read(() => '/help').page_route).toBe('/help')
    expect(read(() => '/old-page').page_route).toBe('/old-page')
    expect(read().page_route).toBeUndefined()
    expect(read(() => { throw new Error('unavailable') }).page_route).toBeUndefined()
    for (const route of [null, 1, '', '//other.test/help', '/help?token=x', '/a/../help',
      '/help#tab', '/a\\b', '/a b', '/' + 'x'.repeat(1000)]) {
      expect(read(() => route).page_route).toBeUndefined()
    }
    expect(read(() => '/help').record_route).toBeUndefined()
  })
  it('publishes only bounded, valid, non-sensitive application facet choices', () => {
    const choices = [
      {ref: 'fc_1', kind: 'group', name: 'Salesperson', active: false},
      {ref: 'fc_2', kind: 'filter', name: '  Open  ', active: true},
      {ref: 'bad ref', kind: 'group', name: 'Discard', active: false},
      {ref: 'fc_3', kind: 'group', name: 'password', active: false},
      ...Array.from({length: 40}, (_, index) => ({
        ref: `fc_${index + 10}`, kind: 'filter', name: `Choice ${index}`, active: false,
      })),
    ]
    const page = new Page(controller(), () => null, createRedactor(), undefined,
      () => choices, vi.fn())
    const reading = page.describe({url: 'https://app.test/orders', title: 'Orders',
      content: ''}, 1)
    expect(reading.observation.facet_candidates?.slice(0, 2)).toEqual([
      {ref: 'fc_1', kind: 'group', name: 'Salesperson', active: false},
      {ref: 'fc_2', kind: 'filter', name: 'Open', active: true},
    ])
    expect(reading.observation.facet_candidates?.length).toBeLessThanOrEqual(32)
    expect(reading.observation.facet_candidates?.some(item => item.name === 'password'))
      .toBe(false)
  })
  it('accepts bounded typed facets and ignores invalid application facts', () => {
    const page = new Page(controller(), () => null, createRedactor(), () => [
      {kind: 'filter', name: '  Open  '}, {kind: 'group', name: 'Customer'},
      {kind: 'candidate', name: 'Not applied'}, {kind: 'group', name: 'Customer'},
    ])
    const reading = page.describe({url: 'https://app.test/orders', title: 'Orders',
      content: ''}, 1)
    expect(reading.observation.page_facets).toEqual([
      {kind: 'filter', name: 'Open'}, {kind: 'group', name: 'Customer'},
    ])
    expect(new Page(controller(), () => null, createRedactor()).describe({
      url: 'https://app.test/orders', title: 'Orders', content: '',
    }, 1).observation.page_facets).toBeUndefined()
  })
  it('keeps 200 complete records, preserves the scroll hint and reports truncation', () => {
    const api = controller()
    const page = new Page(api, (_map, index) => ({
      tagName: 'BUTTON', textContent: `Action ${index}`, isConnected: true,
    }), createRedactor())
    const state: BrowserState = {
      url: 'https://app.test/large', title: 'Large', header: '[Start of page]',
      content: Array.from({length: 205}, (_, index) =>
        `[${index}]<button>Action ${index} />`).join('\n'),
      footer: '... 9000 pixels below - scroll to see more ...',
    }

    const reading = page.describe(state, 7)

    expect(reading.observation.elements).toHaveLength(200)
    expect(reading.observation.elements.at(-1)?.ref).toBe('e199')
    expect(reading.observation.content).not.toContain('[200]<button>')
    expect(reading.observation.content).toContain('scroll to see more')
    expect(reading.observation.truncated).toBe(true)
    expect(reading.fingerprints.size).toBe(200)
  })

  it('keeps the footer inside the character limit when non-interactive text is huge', () => {
    const page = new Page(controller(), () => null, createRedactor())
    const reading = page.describe({
      url: 'https://app.test/large', title: 'Large', header: 'header',
      content: 'x'.repeat(140_000), footer: 'footer-scroll-hint',
    }, 1)

    expect(reading.observation.content.length).toBeLessThanOrEqual(120_000)
    expect(reading.observation.content.endsWith('footer-scroll-hint')).toBe(true)
    expect(reading.observation.truncated).toBe(true)
  })
})

describe('Page refuses controls that are not writable at execution time', () => {
  it('observes disabled form buttons through the controller whitelist without allowing a click', () => {
    const form = document.createElement('form')
    form.id = 'draft'
    form.innerHTML = '<button disabled>Save</button><button hidden disabled>Hidden</button>'
    document.body.append(form)
    try {
      const button = form.querySelector('button')!
      for (const element of form.querySelectorAll('button'))
        element.getBoundingClientRect = () => ({width: 80, height: 20} as DOMRect)
      const options = pageControllerOptions([])
      const whitelist = options.interactiveWhitelist as (() => HTMLElement | undefined)[]
      expect(whitelist[0]!()).toBe(button)
      expect(whitelist[1]!()).toBeUndefined()
      const api = controller()
      const page = new Page(api, () => button, createRedactor())
      expect(() => page.click('e1')).toThrow('disabled buttons')
      expect(api.clickElement).not.toHaveBeenCalled()
      const replaced = button.cloneNode(true) as HTMLButtonElement
      replaced.getBoundingClientRect = () => ({width: 80, height: 20} as DOMRect)
      button.replaceWith(replaced)
      expect(whitelist[0]!()).toBe(replaced)
      const excluded = pageControllerOptions([form]).interactiveWhitelist as (() => HTMLElement | undefined)[]
      expect(excluded[0]!()).toBeUndefined()
    } finally { form.remove() }
  })
  it('commits a local numeric or date draft while ordinary typing keeps its current focus', async () => {
    const form = document.createElement('form')
    form.id = 'draft-types'
    form.innerHTML = '<label>Amount<input type="number"></label><label>Day<input type="date"></label>'
    document.body.append(form)
    try {
      for (const [index, input] of Array.from(form.querySelectorAll('input')).entries()) {
        const changed = vi.fn(), blurred = vi.fn()
        input.addEventListener('change', changed)
        input.addEventListener('blur', blurred)
        const api = controller()
        api.inputText = vi.fn(async (_i, value) => {input.focus(); input.value = value; return {success: true}})
        const page = new Page(api, () => input, createRedactor())
        const value = index === 0 ? '12.50' : '2026-10-05'
        await page.input('e1', value)
        expect(changed).not.toHaveBeenCalled()
        expect(document.activeElement).toBe(input)
        await page.input('e1', value, true)
        expect(changed).toHaveBeenCalledTimes(1)
        expect(blurred).toHaveBeenCalledTimes(1)
        const observed = [{ref: 'e1', role: index === 0 ? 'spinbutton' : 'textbox',
          name: '', tag: 'input', epoch: 1, attrs: {field_label: index === 0 ? 'Amount' : 'Day', form_scope: 'form:draft-types'}}]
        expect(page.verifyFormFields([{ref: 'e1', field: 'value', binding: 'label',
          label: index === 0 ? 'Amount' : 'Day', control: observed[0]!.role,
          scope: 'form:draft-types', value}], observed)).toBe(true)
        if (index === 0) {
          const binding = [{ref: 'e1', field: 'value', binding: 'label', label: 'Amount',
            control: 'spinbutton', scope: 'form:draft-types', value: '12.5'}]
          expect(page.verifyFormFields(binding, observed)).toBe(true)
          input.value = '12.5001'
          expect(page.verifyFormFields(binding, observed)).toBe(false)
          input.value = ''; binding[0]!.value = '0'
          expect(page.verifyFormFields(binding, observed)).toBe(false)
          input.value = '9007199254740992'; binding[0]!.value = '9007199254740993'
          expect(page.verifyFormFields(binding, observed)).toBe(false)
          input.value = '1.25e1'; binding[0]!.value = '12.50'
          expect(page.verifyFormFields(binding, observed)).toBe(true)
        }
      }
    } finally {form.remove()}
  })
  it.each([
    ['2026-10-06T09:05:00', '2026-10-06T09:05'],
    // jsdom serializes nonzero seconds with .000; Chromium uses :07. The
    // application owns the exact spelling, and this test exercises that contract
    // against this DOM implementation rather than inferring time equivalence.
    ['2026-10-06 09:05:07', '2026-10-06T09:05:07.000'],
  ])('commits a native datetime-local draft and verifies the application-normalized value (%s)', async (sourceValue, draftValue) => {
    // @ts-expect-error The independent test application serves its own JS module.
    const {PageController, getElementByIndex} = await import('../../testapps/contacts/assets/page_controller.js')
    const form = document.createElement('form')
    form.id = 'native-datetime'
    form.innerHTML = '<label>Follow-up time<input type="datetime-local" step="1"></label>' +
      '<button type="submit" aria-label="Save contact">Save contact</button>'
    document.body.append(form)
    const input = form.querySelector('input')!, button = form.querySelector('button')!
    // Only geometry is supplied for jsdom; the native value setter, focus and events
    // and the application's PageController input path remain real.
    for (const node of [form, input, button])
      node.getBoundingClientRect = () => ({width: 100, height: 20} as DOMRect)
    const api = new PageController()
    const filled = vi.spyOn(api, 'inputText'), clicked = vi.spyOn(api, 'clickElement')
    const nativeSubmit = vi.spyOn(form, 'submit'), requestSubmit = vi.spyOn(form, 'requestSubmit')
    const submitted = vi.fn((event: Event) => event.preventDefault()), blurred = vi.fn()
    const changed: string[] = []
    form.addEventListener('submit', submitted)
    input.addEventListener('blur', blurred)
    input.addEventListener('change', () => changed.push(input.value))
    const runtime = Object.create(EmbeddedWebRuntime.prototype) as EmbeddedWebRuntime
    const state = runtime as unknown as Record<string, unknown>
    state.controller = api
    state.getElementByIndex = getElementByIndex
    state.redactorInstance = createRedactor()
    const observe = async (epoch: number) => {
      const reading = runtime.page.describe(await runtime.page.read(), epoch)
      state.currentObservation = {observation: reading.observation}
      state.observationFingerprints = reading.fingerprints
      state.observedUrl = reading.canonicalUrl
      return reading.observation
    }
    try {
      // A native input normalizes these strings itself. The adapter must issue that
      // exact spelling in its draft; the Runtime does not infer datetime equivalence.
      input.value = sourceValue
      expect(input.value).toBe(draftValue)
      input.value = ''
      const before = await observe(1)
      const field = before.elements.find(element => element.attrs.field_label === 'Follow-up time')!
      expect(field).toMatchObject({role: 'textbox', attrs: {type: 'datetime-local',
        field_label: 'Follow-up time', form_scope: 'form:native-datetime'}})
      const fill = {command_id: 'datetime-fill', action: 'fill',
        args: {ref: field.ref, value: draftValue, commit: true}, expect_navigation: false}
      expect(runtime._actionFreshness(fill).fresh).toBe(true)
      await expect(runtime._execute(fill)).resolves.toMatchObject({success: true})
      expect(filled).toHaveBeenCalledWith(Number(field.ref.slice(1)), draftValue)
      expect(input.value).toBe(draftValue)
      expect(input.validity.valid).toBe(true)
      expect(changed.at(-1)).toBe(draftValue)
      expect(blurred).toHaveBeenCalledOnce()
      expect(document.activeElement).not.toBe(input)

      const after = await observe(2)
      const entered = after.elements.find(element => element.attrs.field_label === 'Follow-up time')!
      const submit = after.elements.find(element => element.role === 'button' && element.name === 'Save contact')!
      expect(entered.attrs.value).toBe(draftValue)
      const binding = {ref: entered.ref, field: 'follow_up_time', label: 'Follow-up time',
        binding: 'label', scope: 'form:native-datetime', control: 'textbox', value: draftValue}
      const handoff = {command_id: 'datetime-handoff', action: 'handoff_form', args: {
        fields: [binding], submit: {ref: submit.ref, label: 'Save contact', scope: 'form:native-datetime'},
      }, expect_navigation: false}
      expect(runtime._actionFreshness(handoff).fresh).toBe(true)
      const receipt = await runtime._execute(handoff)
      expect(receipt).toEqual({success: true, message: 'form fields verified for handoff'})
      expect(JSON.stringify(receipt)).not.toContain(draftValue)
      expect(button.style.outline).toContain('Highlight')
      expect(clicked).not.toHaveBeenCalled()
      expect(submitted).not.toHaveBeenCalled()

      const unnormalized = {...handoff, args: {...handoff.args, fields: [{...binding, value: sourceValue}]}}
      expect(() => runtime._execute(unnormalized)).toThrow(/fields do not match/)
      input.value = '2026-10-06T09:06:07'
      expect(runtime._actionFreshness(handoff).fresh).toBe(false)
      expect(() => runtime._execute(handoff)).toThrow(/fields do not match/)
      for (const invalid of ['2026-02-29T09:05:00', '2026-10-06T09:05:00Z']) {
        input.value = invalid
        expect(input.value).toBe('')
        expect(() => runtime._execute(handoff)).toThrow(/fields do not match/)
      }
      expect(clicked).not.toHaveBeenCalled()
      expect(submitted).not.toHaveBeenCalled()
      expect(nativeSubmit).not.toHaveBeenCalled()
      expect(requestSubmit).not.toHaveBeenCalled()
    } finally {
      (state.highlightHandle as {clear(): void} | undefined)?.clear()
      api.dispose()
      form.remove()
    }
  })
  it('checks a scoped unnamed input and its native submit button without clicking', () => {
    const form = document.createElement('form')
    form.id = 'native-editor'
    form.innerHTML = '<label for="n">Name</label><input id="n"><button type="submit">Confirm</button>'
    document.body.append(form)
    try {
      const input = form.querySelector('input')!, button = form.querySelector('button')!
      button.getBoundingClientRect = () => ({width: 80, height: 20} as DOMRect)
      input.value = 'Draft'
      const clicked = vi.fn(); button.addEventListener('click', clicked)
      const page = new Page(controller(), (_map, index) => index === 1 ? input : button, createRedactor())
      const observed = [
        {ref: 'e1', role: 'textbox', name: '', tag: 'input', epoch: 1,
          attrs: {field_label: 'Name', form_scope: 'form:native-editor'}},
        {ref: 'e2', role: 'button', name: 'Confirm', tag: 'button', epoch: 1,
          attrs: {form_scope: 'form:native-editor'}},
      ]
      const fields = [{ref: 'e1', field: 'name', label: 'Name', binding: 'label',
        scope: 'form:native-editor', control: 'textbox', value: 'Draft'}]
      const submit = {ref: 'e2', label: 'Confirm', scope: 'form:native-editor'}
      expect(page.verifyFormFields(fields, observed)).toBe(true)
      expect(page.verifyFormSubmit(submit, fields, observed)).toBe(true)
      expect(clicked).not.toHaveBeenCalled()
      input.name = 'different'
      expect(page.verifyFormFields(fields, observed)).toBe(false)
      input.removeAttribute('name')
      button.disabled = true
      expect(page.verifyFormSubmit(submit, fields, observed)).toBe(true)
      button.hidden = true
      expect(page.verifyFormSubmit(submit, fields, observed)).toBe(false)
      button.hidden = false
      button.disabled = false
      expect(page.verifyFormSubmit(submit, fields, [...observed, {...observed[1]!, ref: 'e3'}])).toBe(false)
      form.id = 'replaced'
      expect(page.verifyFormFields(fields, observed)).toBe(false)
    } finally { form.remove() }
  })

  it.each([false, true])('verifies requested prefill while submit enabled is %s, without activating it', async fillEmail => {
    const form = document.createElement('form')
    form.id = 'partial-editor'
    form.innerHTML = '<label>Name<input name="name" required></label>' +
      '<label>Email<input name="email" type="email" required></label><button type="submit" disabled>Save</button>'
    document.body.append(form)
    try {
      const inputs = [...form.querySelectorAll('input')], button = form.querySelector('button')!
      button.getBoundingClientRect = () => ({width: 80, height: 20} as DOMRect)
      const clicked = vi.fn(), submitted = vi.fn()
      button.addEventListener('click', clicked); form.addEventListener('submit', submitted)
      for (const input of inputs) input.addEventListener('change', () => {
        button.disabled = !inputs.every(field => field.checkValidity())
      })
      const api = controller()
      api.inputText = vi.fn(async (index, value) => {inputs[index - 1]!.value = value; return {success: true}})
      const page = new Page(api, (_map, index) => index === 3 ? button : inputs[index - 1], createRedactor())
      expect(button.disabled).toBe(true)
      await page.input('e1', 'Ada Byron', true)
      if (fillEmail) await page.input('e2', 'ada@example.test', true)
      const observed = [
        ...inputs.map((input, index) => ({ref: `e${index + 1}`, role: 'textbox', name: input.name,
          tag: 'input', epoch: 2, attrs: {field: input.name, form_scope: 'form:partial-editor'}})),
        {ref: 'e3', role: 'button', name: 'Save', tag: 'button', epoch: 2,
          attrs: {form_scope: 'form:partial-editor', ...(button.disabled ? {disabled: 'true'} : {})}},
      ]
      const fields = inputs.slice(0, fillEmail ? 2 : 1).map((input, index) => ({ref: `e${index + 1}`,
        field: input.name, control: 'textbox', scope: 'form:partial-editor', value: input.value}))
      expect(page.verifyFormFields(fields, observed)).toBe(true)
      expect(page.verifyFormSubmit({ref: 'e3', label: 'Save', scope: 'form:partial-editor'}, fields, observed)).toBe(true)
      const highlight = page.highlight('e3')
      expect(highlight.active()).toBe(true)
      expect(button.disabled).toBe(!fillEmail)
      expect(clicked).not.toHaveBeenCalled(); expect(submitted).not.toHaveBeenCalled()
      expect(api.clickElement).not.toHaveBeenCalled()
      highlight.clear()
    } finally { form.remove() }
  })
  it('rechecks only actually entered controls outside the viewport in the same live form', async () => {
    const form = document.createElement('form')
    form.id = 'entered-editor'
    form.innerHTML = '<label for="off">Name</label><input id="off"><button type="submit">Save</button>'
    document.body.append(form)
    try {
      const input = form.querySelector('input')!
      vi.spyOn(input, 'getBoundingClientRect').mockReturnValue({width: 20, height: 20} as DOMRect)
      const api = controller()
      api.inputText = vi.fn(async (_i, value) => {input.value = value; return {success: true}})
      const page = new Page(api, () => input, createRedactor())
      const binding = [{field: 'name', label: 'Name', binding: 'label',
        scope: 'form:entered-editor', control: 'textbox', value: 'Draft'}]
      const observed = () => [{ref: 'e3', role: 'button', name: 'Save', tag: 'button', epoch: 4,
        attrs: {form_scope: 'form:entered-editor', form_instance: formInstance(input)!}}]
      expect(page.verifyFormFields(binding, observed())).toBe(false)
      await page.input('e1', 'Draft')
      expect(page.verifyFormFields(binding, observed())).toBe(true)
      expect(page.verifyFormFields(binding, [])).toBe(false)
      input.value = 'Changed'
      expect(page.verifyFormFields(binding, observed())).toBe(false)
      input.value = 'Draft'; input.disabled = true
      expect(page.verifyFormFields(binding, observed())).toBe(false)
      input.disabled = false; input.hidden = true
      expect(page.verifyFormFields(binding, observed())).toBe(false)
      input.hidden = false
      form.setAttribute('hidden', ''); form.removeAttribute('hidden')
      expect(page.verifyFormFields(binding, observed())).toBe(false)
      await page.input('e1', 'Draft')
      expect(page.verifyFormFields(binding, observed())).toBe(true)
      input.remove()
      expect(page.verifyFormFields(binding, observed())).toBe(false)
    } finally {form.remove()}
  })

  it('refuses a forged field identity inherited from the HTML form name', () => {
    const form = document.createElement('form')
    form.name = 'customer'
    form.innerHTML = '<input>'
    document.body.append(form)
    try {
      const input = form.querySelector('input')!
      input.value = 'draft'
      const page = new Page(controller(), () => input, createRedactor())
      expect(page.verifyFormFields(
        [{ref: 'e2', field: 'customer', control: 'textbox', value: 'draft'}],
        [{ref: 'e2', role: 'textbox', name: '', tag: 'input', epoch: 4,
          attrs: {field: 'customer'}}],
      )).toBe(false)
    } finally { form.remove() }
  })

  it('verifies an exact live form value without returning that value in a receipt', () => {
    const input = document.createElement('input')
    input.name = 'phone'
    input.value = '+1 512 555 0199'
    document.body.append(input)
    try {
      const page = new Page(controller(), () => input, createRedactor())
      const observed = [{ref: 'e2', role: 'textbox', name: 'Phone', tag: 'input', epoch: 4,
        attrs: {field: 'phone'}}]
      const binding = [{ref: 'e2', field: 'phone', control: 'textbox', value: input.value}]
      expect(page.verifyFormFields(binding, observed)).toBe(true)
      input.value = 'someone changed it'
      expect(page.verifyFormFields(binding, observed)).toBe(false)
      input.value = binding[0]!.value
      input.name = 'different_field'
      expect(page.verifyFormFields(binding, observed)).toBe(false)
    } finally { input.remove() }
  })

  it('highlights a live control without clicking it, then restores its style', () => {
    const api = controller()
    const button = document.createElement('button')
    button.textContent = 'Review'
    button.style.outline = '1px dotted red'
    button.getBoundingClientRect = () => ({width: 80, height: 20} as DOMRect)
    button.scrollIntoView = vi.fn()
    document.body.append(button)
    try {
      const page = new Page(api, () => button, createRedactor())
      const marker = page.highlight('e1')
      expect(marker.active()).toBe(true)
      expect(button.style.outline).toContain('Highlight')
      expect(button.style.outlineOffset).toBe('-3px')
      expect(button.scrollIntoView).toHaveBeenCalledOnce()
      expect(api.clickElement).not.toHaveBeenCalled()
      marker.clear()
      expect(marker.active()).toBe(false)
      expect(button.style.outline).toBe('1px dotted red')
      const replaced = page.highlight('e1')
      button.style.setProperty('outline', '2px solid green', 'important')
      expect(replaced.active()).toBe(false)
      replaced.clear()
      expect(button.style.outline).toContain('green')
    } finally {
      button.remove()
    }
  })

  it('keeps a highlight through controller annotations but clears it when application context changes', async () => {
    const owner = document.createElement('main')
    owner.innerHTML = '<span>Order Q-1042</span><button>Review</button>'
    const button = owner.querySelector('button')!
    button.getBoundingClientRect = () => ({width: 80, height: 20} as DOMRect)
    document.body.append(owner)
    const marker = new Page(controller(), () => button, createRedactor()).highlight('e1')
    try {
      const overlay = document.createElement('div')
      overlay.id = 'playwright-highlight-container'
      overlay.innerHTML = '<span>1</span><span>2</span>'
      owner.append(overlay)
      await Promise.resolve()
      expect(marker.active()).toBe(true)
      overlay.remove()
      await Promise.resolve()
      expect(marker.active()).toBe(true)
      owner.querySelector('span')!.textContent = 'Order Q-1043'
      await Promise.resolve()
      expect(marker.active()).toBe(false)
      expect(button.style.outline).toBe('')
    } finally { marker.clear(); owner.remove() }
  })

  it('refuses a hidden or detached handoff control', () => {
    const button = document.createElement('button')
    button.getBoundingClientRect = () => ({width: 0, height: 0} as DOMRect)
    document.body.append(button)
    try {
      expect(() => new Page(controller(), () => button, createRedactor()).highlight('e1'))
        .toThrow(/not visible/)
    } finally {
      button.remove()
    }
    expect(() => new Page(controller(), () => button, createRedactor()).highlight('e1'))
      .toThrow(/no longer on the page/)
  })

  it('applies only a currently offered facet with a changed desired state', async () => {
    let active = false
    const apply = vi.fn(() => { active = true })
    const page = new Page(controller(), () => null, createRedactor(), undefined,
      () => [{ref: 'fc_1', kind: 'group', name: 'Salesperson', active}], apply)
    expect(() => page.applyFacet('fc_2', true)).toThrow(/candidate absent/)
    expect(() => page.applyFacet('fc_1', false)).toThrow(/already/)
    await page.applyFacet('fc_1', true)
    expect(apply).toHaveBeenCalledOnce()
    expect(apply).toHaveBeenCalledWith('fc_1', true)
    expect(() => page.applyFacet('fc_1', true)).toThrow(/already/)
  })

  it('rejects a reused facet ref if the page changed after observation', () => {
    let name = 'Salesperson'
    const runtime = Object.create(EmbeddedWebRuntime.prototype) as EmbeddedWebRuntime
    const state = runtime as unknown as Record<string, unknown>
    state.controller = controller()
    state.getElementByIndex = () => null
    state.redactorInstance = createRedactor()
    state.readFacetCandidates = () => [
      {ref: 'fc_1', kind: 'group', name, active: false},
    ]
    state.applyPageFacet = vi.fn()
    state.observedUrl = window.location.href
    state.currentObservation = {observation: {
      url: window.location.href,
      facet_candidates: [{ref: 'fc_1', kind: 'group', name, active: false}],
    }}
    const command = {command_id: 'c1', action: 'apply_facet',
      args: {ref: 'fc_1', active: true}, expect_navigation: false}
    expect(runtime._actionFreshness(command).fresh).toBe(true)
    name = 'Customer'
    expect(runtime._actionFreshness(command).fresh).toBe(false)
    expect(() => runtime._execute(command)).toThrow(/changed before dispatch/)
    expect(state.applyPageFacet).not.toHaveBeenCalled()
  })
  it('does not pass a disabled input to PageController', async () => {
    const api = controller()
    const page = new Page(api, () => ({tagName: 'INPUT', disabled: true}), createRedactor())

    expect(() => page.input('e1', 'NovaForge')).toThrow(/disabled or read-only/)
    expect(api.inputText).not.toHaveBeenCalled()
  })

  it('does not pass a read-only select or a missing control to PageController', async () => {
    const api = controller()
    const readOnly = new Page(api, () => ({tagName: 'SELECT', readOnly: true}), createRedactor())
    const missing = new Page(api, () => null, createRedactor())

    expect(() => readOnly.select('e2', 'active')).toThrow(/disabled or read-only/)
    expect(() => missing.select('e3', 'active')).toThrow(/no longer on the page/)
    expect(api.selectOption).not.toHaveBeenCalled()
  })

  it('translates an application option value to PageController visible text', async () => {
    const api = controller()
    const page = new Page(api, () => ({
      tagName: 'SELECT', isConnected: true,
      options: [
        {value: 'order', textContent: 'Ordered quantities'},
        {value: 'delivery', textContent: 'Delivered quantities'},
      ],
    }), createRedactor())

    await page.select('e4', 'delivery')

    expect(api.selectOption).toHaveBeenCalledWith(4, 'Delivered quantities')
  })

  it('refuses absent, disabled, and duplicate-label option values before dispatch', () => {
    const api = controller()
    const absent = new Page(api, () => ({
      tagName: 'SELECT', options: [{value: 'active', textContent: 'Active'}],
    }), createRedactor())
    const disabled = new Page(api, () => ({
      tagName: 'SELECT', options: [{value: 'archived', textContent: 'Archived', disabled: true}],
    }), createRedactor())
    const duplicate = new Page(api, () => ({
      tagName: 'SELECT', options: [
        {value: 'one', textContent: 'Same'}, {value: 'two', textContent: 'Same'},
      ],
    }), createRedactor())

    expect(() => absent.select('e5', 'missing')).toThrow(/absent, ambiguous, or disabled/)
    expect(() => disabled.select('e5', 'archived')).toThrow(/absent, ambiguous, or disabled/)
    expect(() => duplicate.select('e5', 'two')).toThrow(/label is absent or ambiguous/)
    expect(api.selectOption).not.toHaveBeenCalled()
  })
})

describe('application navigation facts', () => {
  it('projects a plain HTML adapter target and invalidates a ref when it changes', async () => {
    // @ts-expect-error The independent test application serves its own JS module.
    const {readElementNavigation} = await import('../../testapps/contacts/assets/page_controller.js')
    const nav = document.createElement('nav')
    nav.innerHTML = '<a href="/contacts">Contacts</a>'
    document.body.append(nav)
    const link = nav.querySelector('a')!
    const api = controller()
    const page = new Page(api, () => link, createRedactor(),
      undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined,
      () => false, readElementNavigation)
    const state = {url:location.href,title:'',content:'[1]<a href="/contacts">Contacts />'}
    const reading = page.describe(state, 1)
    expect(reading.observation.elements[0]!.attrs.navigation_path).toBe('/contacts')
    const fingerprint = reading.fingerprints.get('e1')
    link.href = '/companies'
    expect(page.fingerprint('e1')).not.toBe(fingerprint)
    link.href = '//other.test/contacts'
    expect(page.describe(state, 2).observation.elements[0]!.attrs.navigation_path).toBeUndefined()
    nav.remove()
  })
  it('refuses malformed and secret-bearing target readers', () => {
    const element = document.createElement('div')
    const state = {url:location.href,title:'',content:'[1]<div role=menuitem>Menu />'}
    for (const value of [{kind:'page',path:'//foreign.test/a'}, {kind:'page',path:'/a?token=secret'},
      {kind:'page',path:'/a/../b'}, {kind:'execute',path:'/a'}, {kind:'page',path:'https://foreign.test/a'}]) {
      const page = new Page(controller(), () => element, createRedactor(),
        undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined,
        () => false, () => value)
      expect(page.describe(state,1).observation.elements[0]!.attrs.navigation_path).toBeUndefined()
    }
  })
})

it('invalidates a menu ref when the app reports a changed expansion state', () => {
    const element = document.createElement('div')
    const facts = {kind:'branch',path:'/crm',expanded:false}
    const page = new Page(controller(), () => element, createRedactor(),
      undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined,
      () => false, () => facts)
    const reading = page.describe({url:location.href,title:'',content:'[1]<div role=menuitem>CRM />'},1)
    expect(reading.observation.elements[0]!.attrs['aria-expanded']).toBe('false')
    facts.expanded = true
    expect(page.fingerprint('e1')).not.toBe(reading.fingerprints.get('e1'))
  })

describe('a collapsed menu is opened the way a person opens it', () => {
  function setup(opensOn: 'hover' | 'press') {
    const button = document.createElement('button')
    button.type = 'button'
    button.setAttribute('aria-haspopup', 'menu')
    button.setAttribute('aria-expanded', 'false')
    document.body.append(button)
    // A menu that opens under the pointer closes again when its control is pressed.
    button.addEventListener('mouseenter', () => {
      if (opensOn === 'hover') button.setAttribute('aria-expanded', 'true')
    })
    const api = controller()
    api.clickElement = vi.fn(async () => {
      button.setAttribute('aria-expanded', opensOn === 'hover' ? 'false' : 'true')
      return {success: true}
    })
    const page = new Page(api, () => button, createRedactor())
    return {button, api, page}
  }
  it('lets the pointer open it and does not then press it shut', async () => {
    const s = setup('hover')
    try {
      await s.page.reveal('e1')
      expect(s.api.clickElement).not.toHaveBeenCalled()
      expect(s.button.getAttribute('aria-expanded')).toBe('true')
    } finally {s.button.remove()}
  })
  it('presses a control the pointer alone did not open', async () => {
    const s = setup('press')
    try {
      await s.page.reveal('e1')
      expect(s.api.clickElement).toHaveBeenCalledTimes(1)
      expect(s.button.getAttribute('aria-expanded')).toBe('true')
    } finally {s.button.remove()}
  })
  it('still refuses a control that is not a collapsed menu', async () => {
    const s = setup('press')
    try {
      s.button.setAttribute('aria-expanded', 'true')
      await expect(s.page.reveal('e1')).rejects.toThrow(/not a collapsed menu/)
      expect(s.api.clickElement).not.toHaveBeenCalled()
    } finally {s.button.remove()}
  })
})

describe('application-owned form context on ordinary HTML', () => {
  it('binds live record identity into freshness and never accepts controller-shaped mode text', () => {
    const form = document.createElement('form')
    form.id = 'html-contact-editor'
    form.dataset.mode = 'update'; form.dataset.recordId = 'contact-b'
    form.innerHTML = '<label>Name<input name="name" value="Same"></label>'
    document.body.append(form)
    const input = form.querySelector('input')!
    const api = controller()
    // A structurally different app declares its real HTML form state, no Vue/Yudao rule.
    const context = (element: unknown) => {
      const owner = (element as Element).closest('form')!
      return {kind: 'form', mode: owner.dataset.mode,
        recordId: owner.dataset.mode === 'create' ? null : owner.dataset.recordId,
        inputKind: owner.dataset.inputKind}
    }
    const make = (reader?: typeof context) => new Page(api, () => input, createRedactor(),
      undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined,
      undefined, undefined, undefined, reader)
    const state = {url:location.href, title:'', content:'[1]<input form_mode="create" form_record_id="" form_input_kind="datetime" />'}
    try {
      const page = make(context), before = page.fingerprint('e1')
      expect(page.describe(state,1).observation.elements[0]!.attrs).toMatchObject({form_mode:'update',form_record_id:'contact-b'})
      expect(page.describe(state,1).observation.elements[0]!.attrs.form_input_kind).toBeUndefined()
      form.dataset.inputKind = 'datetime'
      const dated = page.fingerprint('e1')
      expect(dated).not.toBe(before)
      expect(page.describe(state,1).observation.elements[0]!.attrs.form_input_kind).toBe('datetime')
      form.dataset.inputKind = 'date'
      expect(page.fingerprint('e1')).not.toBe(dated)
      form.dataset.inputKind = 'untrusted'
      expect(page.describe(state,1).observation.elements[0]!.attrs.form_input_kind).toBeUndefined()
      form.dataset.recordId = 'contact-c'
      expect(page.fingerprint('e1')).not.toBe(before)
      form.dataset.mode = 'unknown'
      expect(page.describe(state,2).observation.elements[0]!.attrs.form_mode).toBeUndefined()
      expect(make().describe(state,3).observation.elements[0]!.attrs.form_mode).toBeUndefined()
      form.dataset.mode = 'create'; delete form.dataset.recordId
      expect(page.describe(state,4).observation.elements[0]!.attrs).toMatchObject({form_mode:'create',form_record_id:''})
      input.type = 'password'
      expect(page.describe(state,5).observation.elements[0]!.attrs.form_mode).toBeUndefined()
    } finally {form.remove()}
  })
})
