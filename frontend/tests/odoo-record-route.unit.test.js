import {afterEach, expect, it, vi} from 'vitest'
import {readPageRecordRoute}
  from '../../examples/odoo/deploy/addons/woa_runtime/static/src/page-record-route.js'

afterEach(() => vi.unstubAllGlobals())

it('reads only the Odoo form bridge and never infers identity from the URL', () => {
  expect(readPageRecordRoute()).toBeUndefined()
  vi.stubGlobal('__woaRecordBridge', {current: () => '/odoo/res.partner/4'})
  expect(readPageRecordRoute()).toBe('/odoo/res.partner/4')
})
