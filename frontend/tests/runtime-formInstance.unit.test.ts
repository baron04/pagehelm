import {describe, it, expect} from 'vitest'
import {formInstance} from '../src/runtime/formInstance'
import {actionElementFingerprint} from '../src/runtime/pageModel'

describe('current form instance is a DOM lifetime fact', () => {
  it('shares one token for fields and footer while values change', () => {
    const form = document.createElement('form')
    form.innerHTML = '<input name="name"><button>Save</button>'
    document.body.append(form)
    try {
      const input = form.querySelector('input')!, button = form.querySelector('button')!
      const token = formInstance(input)
      expect(token).toMatch(/^[0-9a-f-]{36}$/)
      input.value = 'Edited'
      expect(formInstance(input)).toBe(token)
      expect(formInstance(button)).toBe(token)
    } finally { form.remove() }
  })

  it('invalidates close/reopen and same-ID replacement before the next action', () => {
    const overlay = document.createElement('section')
    overlay.innerHTML = '<div role="dialog" id="editor"><input name="name"></div>'
    document.body.append(overlay)
    try {
      const input = overlay.querySelector('input')!
      const token = formInstance(input), fingerprint = actionElementFingerprint(input)
      overlay.hidden = true
      overlay.hidden = false
      expect(formInstance(input)).not.toBe(token)
      expect(actionElementFingerprint(input)).not.toBe(fingerprint)
      const second = formInstance(input)
      overlay.innerHTML = '<div role="dialog" id="editor"><input name="name"></div>'
      expect(formInstance(overlay.querySelector('input'))).not.toBe(second)
      expect(formInstance(input)).toBeNull()
    } finally { overlay.remove() }
  })

  it('rejects controls outside a live form and refreshes replaced form controls', () => {
    const form = document.createElement('form')
    form.innerHTML = '<input>'
    document.body.append(form)
    try {
      expect(formInstance(document.body)).toBeNull()
      const token = formInstance(form.querySelector('input'))
      form.replaceChildren(document.createElement('input'))
      expect(formInstance(form.querySelector('input'))).not.toBe(token)
    } finally { form.remove() }
  })
})
