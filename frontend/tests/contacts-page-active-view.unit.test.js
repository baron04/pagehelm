/* global document */
import {afterEach, expect, it} from 'vitest'
import {readActiveView} from '../../testapps/contacts/assets/page_controller.js'

afterEach(() => { document.body.innerHTML = '' })

it('reads a REST app view marker, not an Odoo class or URL', () => {
  document.body.innerHTML = '<main id="app" data-active-view="List"></main>'
  expect(readActiveView()).toBe('List')
  document.querySelector('main').removeAttribute('data-active-view')
  expect(readActiveView()).toBeUndefined()
})
