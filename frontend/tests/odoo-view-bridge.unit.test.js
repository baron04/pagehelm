import {afterEach, expect, it} from 'vitest'
import {webcrypto, createHash} from 'node:crypto'
import {TextEncoder} from 'node:util'
import source from '../src/integrations/odoo/view-bridge.js?raw'

// Exercise the application module with Owl's raw/proxy identity contract and lifecycle.
function fixture() {
  const rawOf = new WeakMap(), mounted = []
  class Model {async load() {}}
  class DynamicList {async load() {}}
  class Controller {setup() {}}
  class Form extends Controller {}
  class List extends Controller {}
  class Kanban extends Controller {}
  class SearchBar extends Controller {}
  function patch(target, extension) {
    const parent = Object.create(Object.getPrototypeOf(target))
    Object.defineProperties(parent, Object.getOwnPropertyDescriptors(target))
    Object.setPrototypeOf(extension, parent)
    Object.defineProperties(target, Object.getOwnPropertyDescriptors(extension))
  }
  const fakeCrypto = webcrypto
  new Function('FormController', 'ListController', 'KanbanController', 'RelationalModel',
    'DynamicList', 'SearchBar', 'patch', 'onMounted', 'onWillUnmount', 'toRaw', 'crypto', 'location', 'TextEncoder',
    source.replace(/^import .*;\n/gm, ''))(
    Form, List, Kanban, Model, DynamicList, SearchBar, patch, fn => mounted.push(fn), () => {},
    value => rawOf.get(value) || value, fakeCrypto, {pathname: '/odoo/products'},
    TextEncoder,
  )
  const element = document.createElement('div')
  element.getClientRects = () => [{}]
  document.body.append(element)
  const raw = new Model()
  raw.config = {resModel: 'product.template'}
  raw.root = {offset: 0, limit: 20, count: 40, records: Array(20).fill({}), domain: [], orderBy: []}
  const proxy = new Proxy(raw, {})
  rawOf.set(proxy, raw)
  const controller = new List()
  Object.assign(controller, {rootRef: {el: element}, model: proxy,
    props: {resModel: 'product.template'}, env: {config: {actionId: 8}},
    actionService: {currentController: {action: {type: 'ir.actions.act_window',
      id: 8, path: 'products', res_model: 'product.template'}}}})
  controller.setup()
  mounted.forEach(fn => fn())
  function mountSearch(model) {
    const el = document.createElement('div')
    el.getClientRects = () => [{}]
    element.append(el)
    const bar = new SearchBar()
    Object.assign(bar, {root: {el}, env: {searchModel: model}})
    bar.setup()
    mounted.at(-1)()
    return bar
  }
  return {raw, proxy, controller, mountSearch}
}

afterEach(() => {delete window.__woaViewBridge; document.body.replaceChildren()})

it('binds completed reads across the Owl reactive wrapper, but refuses dirty or mismatched views', async () => {
  const {raw, proxy, controller} = fixture()
  expect(window.__woaViewBridge.current()).toBeNull()
  await raw.load()
  expect(window.__woaViewBridge.current()).toMatchObject({path: '/odoo/products', page: 1, revision: 1})
  raw.root.offset = 20
  await proxy.load()
  expect(window.__woaViewBridge.current()).toMatchObject({page: 2, revision: 2})
  raw.root.dirty = true
  expect(window.__woaViewBridge.current()).toBeNull()
  raw.root.dirty = false
  controller.env.config.actionId = 9
  expect(window.__woaViewBridge.current()).toBeNull()
})

it('does not certify a pending read or missing model as rendered state', async () => {
  const {raw} = fixture()
  const pending = raw.load()
  expect(window.__woaViewBridge.current()).toBeNull()
  await pending
  expect(window.__woaViewBridge.current()).not.toBeNull()
  raw.root = null
  await raw.load()
  expect(window.__woaViewBridge.current()).toBeNull()
})

it('projects only scalar search terms read by the mounted view, never draft or duplicate OR terms', async () => {
  const {raw, mountSearch} = fixture()
  const hash = value => createHash('sha256').update(JSON.stringify([value])).digest('hex')
  const field = {id: 1, type: 'field', fieldName: 'name', fieldType: 'char'}
  const filter = {type: 'filter', name: 'active', isActive: false}
  const model = {resModel: 'product.template', domain: [], query: [], searchItems: {1: field},
    getSearchItems: predicate => [field, filter].filter(predicate)}
  mountSearch(model)
  await raw.load()
  expect(window.__woaViewBridge.current().query_fields).toEqual({filter_active: hash('false')})
  model.query = [{searchItemId: 1, autocompleteValue: {value: 'Blue', operator: 'ilike'}}]
  model.domain = [['name', 'ilike', 'Blue']]
  expect(window.__woaViewBridge.current().query_fields).toEqual({})
  raw.root.domain = model.domain
  await raw.load()
  expect(window.__woaViewBridge.current().query_fields).toEqual({filter_active: hash('false'), search_name: hash('Blue')})
  model.query.push({searchItemId: 1, autocompleteValue: {value: 'Red', operator: 'ilike'}})
  expect(window.__woaViewBridge.current()).toBeNull()
  await new Promise(resolve => setTimeout(resolve, 10))
  expect(window.__woaViewBridge.current().query_fields).toEqual({filter_active: hash('false')})
})

it('refuses an unrelated search model and several visible search bars', async () => {
  const {raw, mountSearch} = fixture()
  const model = {resModel: 'res.partner', domain: [], query: [], getSearchItems: () => []}
  mountSearch(model)
  await raw.load()
  expect(window.__woaViewBridge.search()).toBeNull()
  model.resModel = 'product.template'
  expect(window.__woaViewBridge.search()).not.toBeNull()
  mountSearch(model)
  expect(window.__woaViewBridge.search()).toBeNull()
})
