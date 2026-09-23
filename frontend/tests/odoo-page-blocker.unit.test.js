/* global document */
import {afterEach, expect, it} from 'vitest'
import {readPageBlocker}
  from '../../examples/odoo/deploy/addons/woa_runtime/static/src/page-blocker.js'

afterEach(() => { document.body.innerHTML = '' })

it('reports a visible Odoo technical error, not an ordinary dialog', () => {
  expect(readPageBlocker()).toBe(false)
  const dialog = document.createElement('div')
  dialog.className = 'modal o_technical_modal'
  document.body.appendChild(dialog)
  dialog.getClientRects = () => [{width: 300, height: 100}]
  expect(readPageBlocker()).toBe(false)
  const error = document.createElement('div')
  error.className = 'modal-content o_error_dialog'
  dialog.appendChild(error)
  error.getClientRects = () => [{width: 300, height: 100}]
  expect(readPageBlocker()).toBe(true)
  error.getClientRects = () => []
  expect(readPageBlocker()).toBe(false)
  dialog.remove()
  expect(readPageBlocker()).toBe(false)
})
