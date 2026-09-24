/* global document */
import {afterEach, expect, it} from 'vitest'
import {readActiveView} from '../../examples/odoo/deploy/addons/woa_runtime/static/src/page-active-view.js'

afterEach(() => { document.body.innerHTML = '' })

it('reads an actual rendered list view without a switch button', () => {
  document.body.innerHTML = '<div class="o_action_manager"><div class="o_view_controller o_list_view"></div></div>'
  const controller = document.querySelector('.o_view_controller')
  controller.getClientRects = () => [{width: 300, height: 100}]
  expect(readActiveView()).toBe('List')
  controller.className = 'o_view_controller o_kanban_view'
  expect(readActiveView()).toBe('Kanban')
  controller.getClientRects = () => []
  expect(readActiveView()).toBeUndefined()
})

it('does not guess a view during a transition with two visible controllers', () => {
  document.body.innerHTML = '<div class="o_action_manager"><div class="o_view_controller o_list_view"></div><div class="o_view_controller o_form_view"></div></div>'
  for (const controller of document.querySelectorAll('.o_view_controller')) {
    controller.getClientRects = () => [{width: 300, height: 100}]
  }
  expect(readActiveView()).toBeUndefined()
})
