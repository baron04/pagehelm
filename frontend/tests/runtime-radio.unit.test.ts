import {describe, expect, it, vi} from 'vitest'
import {Page, type PageControllerApi} from '../src/runtime/page'
import {hydrateFormFacts} from '../src/runtime/pageModel'
import {createRedactor} from '../src/runtime/redaction'
import {formInstance} from '../src/runtime/formInstance'

function setup(aria = false, interrupted: () => boolean = () => false) {
  const form = document.createElement('form')
  form.id = 'radio-form'
  form.innerHTML = `<div role="radiogroup" aria-label="Visibility">${aria
    ? '<button type="button" role="radio" aria-checked="true">Private</button><button type="button" role="radio" aria-checked="false">Public</button>'
    : '<label>Private<input type="radio" name="visibility" value="false" checked></label><label>Public<input type="radio" name="visibility" value="true"></label>'}</div>`
  document.body.append(form)
  const group = form.firstElementChild as HTMLElement
  group.getBoundingClientRect = () => ({width: 100, height: 30} as DOMRect)
  const options = [...group.querySelectorAll<HTMLElement>(aria ? '[role=radio]' : 'input')]
  const api = {
    selectorMap: {}, getBrowserState: vi.fn(async () => ({url: location.href, title: '', content: '[1]<input />\n[2]<input />'})),
    clickElement: vi.fn(async (index: number) => {
      options.forEach((e, i) => e instanceof HTMLInputElement ? e.checked = index === i + 1
        : e.setAttribute('aria-checked', String(index === i + 1)))
      return {success: true}
    }), inputText: vi.fn(), selectOption: vi.fn(), scroll: vi.fn(), dispose: vi.fn(),
  } as PageControllerApi
  const lookup = (_: unknown, index: number) => options[index - 1]
  const redactor = createRedactor()
  const page = new Page(api, lookup, redactor, undefined, undefined, undefined, undefined,
    undefined, undefined, undefined, undefined, interrupted)
  const observed = hydrateFormFacts(options.map((_, i) => ({ref: `e${i + 1}`, role: 'radio', name: '',
    tag: 'input', epoch: 1, attrs: {}})), {}, lookup, redactor)
  const fields = [{ref:'e1', field:'visibility', label:'Visibility', binding:'label',
    scope:'form:radio-form', control:'radiogroup', value:'true', option_label:'Public'}]
  return {form, group, options, api, page, observed, fields}
}

describe('adapter-issued radio choice', () => {
  it.each([false, true])('groups native/ARIA options into one field and checks actual selection (ARIA=%s)', async aria => {
    const s = setup(aria)
    try {
      expect(s.observed.filter(e => e.role === 'radiogroup')).toHaveLength(1)
      expect(s.observed[0]?.attrs.field_label).toBe('Visibility')
      expect(JSON.parse(s.observed[0]!.attrs.radio_options!)).toEqual(aria
        ? [{label:'Private',value:'Private'}, {label:'Public',value:'Public'}]
        : [{label:'Private',value:'false'}, {label:'Public',value:'true'}])
      await s.page.select('e1', 'true', 'Public')
      expect(s.api.clickElement).toHaveBeenCalledExactlyOnceWith(2)
      expect(s.page.verifyFormFields(s.fields, s.observed)).toBe(true)
      if (s.options[1] instanceof HTMLInputElement) s.options[1].checked = false
      else s.options[1]!.setAttribute('aria-checked', 'false')
      expect(s.page.verifyFormFields(s.fields, s.observed)).toBe(false)
    } finally { s.form.remove() }
  })
  it('omits label/value facts for ambiguous or sensitive options', () => {
    for (const mode of ['duplicate', 'sensitive']) {
      const s = setup()
      try {
        if (mode === 'duplicate') s.group.firstElementChild!.firstChild!.textContent = 'Public'
        else (s.options[0] as HTMLInputElement).value = 'token=synthetic-secret'
        const observed = hydrateFormFacts([{ref:'e1',role:'radio',name:'',tag:'input',epoch:1,attrs:{}}],
          {}, () => s.options[0], createRedactor())
        expect(observed[0]!.attrs.radio_options).toBeUndefined()
      } finally {s.form.remove()}
    }
  })
  it.each(['duplicate', 'disabled', 'wrong-group', 'sensitive'])('refuses %s without an option click', async mode => {
    const s = setup()
    try {
      if (mode === 'duplicate') s.group.firstElementChild!.firstChild!.textContent = 'Public'
      if (mode === 'disabled') (s.options[1] as HTMLInputElement).disabled = true
      if (mode === 'wrong-group') s.group.removeAttribute('role')
      if (mode === 'sensitive') s.group.setAttribute('aria-label', 'API token')
      await expect(Promise.resolve().then(() => s.page.select('e1', 'true', 'Public'))).rejects.toThrow()
      expect(s.api.clickElement).not.toHaveBeenCalled()
    } finally { s.form.remove() }
  })
  it('does not retry an acknowledged click that failed to establish exclusive selection', async () => {
    const s = setup()
    try {
      s.api.clickElement = vi.fn(async () => ({success: true}))
      await expect(s.page.select('e1', 'true', 'Public')).rejects.toThrow(/not verified/)
      expect(s.api.clickElement).toHaveBeenCalledTimes(1)
    } finally { s.form.remove() }
  })
  it('rejects a disabled native button masquerading as an ARIA radio', async () => {
    const s = setup(true)
    try {
      ;(s.options[1] as HTMLButtonElement).disabled = true
      await expect(s.page.select('e1', 'true', 'Public')).rejects.toThrow(/unavailable/)
      expect(s.api.clickElement).not.toHaveBeenCalled()
    } finally { s.form.remove() }
  })
  it('rejects a native option whose current value disagrees with the declared label binding', async () => {
    const s = setup()
    try {
      ;(s.options[1] as HTMLInputElement).value = 'other'
      await expect(s.page.select('e1', 'true', 'Public')).rejects.toThrow(/changed/)
      expect(s.api.clickElement).not.toHaveBeenCalled()
      ;(s.options[0] as HTMLInputElement).checked = false
      ;(s.options[1] as HTMLInputElement).checked = true
      expect(s.page.verifyFormFields(s.fields, s.observed)).toBe(false)
    } finally { s.form.remove() }
  })
  it.each(['intervention', 'replacement', 'value-change'])('stops after fresh indexing on %s', async mode => {
    let interrupted = false
    const s = setup(false, () => interrupted)
    try {
      s.api.getBrowserState = vi.fn(async () => {
        if (mode === 'intervention') interrupted = true
        else if (mode === 'replacement') s.options[1]!.replaceWith(s.options[1]!.cloneNode(true))
        else (s.options[1] as HTMLInputElement).value = 'other'
        return {url:location.href, title:'', content:'[2]<input />'}
      })
      await expect(s.page.select('e1', 'true', 'Public')).rejects.toThrow(/fresh action/)
      expect(s.api.clickElement).not.toHaveBeenCalled()
    } finally { s.form.remove() }
  })
  it('keeps an approved radio selection when it removes an unentered dependent field', async () => {
    const s = setup()
    try {
      const dependent = document.createElement('div')
      dependent.innerHTML = '<label>Members<input name="members"></label>'
      s.form.append(dependent)
      const later = document.createElement('input'); s.form.append(later)
      const before = formInstance(s.group)
      const click = s.api.clickElement
      s.api.clickElement = vi.fn(async index => {
        dependent.remove()
        return click(index)
      })
      await s.page.select('e1', 'true', 'Public')
      expect(formInstance(s.group)).toBe(before)
      expect(s.page.verifyFormFields(s.fields, s.observed)).toBe(true)
      expect(s.api.clickElement).toHaveBeenCalledTimes(1)
      later.remove()
      expect(formInstance(s.group)).not.toBe(before)
    } finally { s.form.remove() }
  })
  it.each(['replacement', 'reopen', 'heading', 'intervention', 'protected-removal', 'new-control'])(
    'does not turn %s during an acknowledged radio click into a successful handoff', async mode => {
      let interrupted = false
      const s = setup(false, () => interrupted)
      try {
        const title = document.createElement('h2'); title.setAttribute('role', 'heading'); title.textContent = 'Create'
        s.form.prepend(title)
        const entered = document.createElement('input'); entered.name = 'name'; s.form.append(entered)
        s.options.push(entered)
        s.api.inputText = vi.fn(async () => {entered.value = 'Synthetic'; return {success: true}})
        await s.page.input('e3', 'Synthetic')
        const before = formInstance(s.group), click = s.api.clickElement
        s.api.clickElement = vi.fn(async index => {
          const result = await click(index)
          if (mode === 'replacement') s.options[1]!.replaceWith(s.options[1]!.cloneNode(true))
          else if (mode === 'reopen') {s.form.hidden = true; s.form.hidden = false}
          else if (mode === 'heading') title.textContent = 'Edit another record'
          else if (mode === 'intervention') interrupted = true
          else if (mode === 'new-control') s.form.append(document.createElement('input'))
          else entered.remove()
          return result
        })
        await expect(s.page.select('e1', 'true', 'Public')).rejects.toThrow(/not verified/)
        expect(s.api.clickElement).toHaveBeenCalledTimes(1)
        if (mode !== 'intervention') expect(formInstance(s.group)).not.toBe(before)
      } finally { s.form.remove() }
    })
  it('closes the removal window when the controller throws', async () => {
    const s = setup()
    try {
      const dependent = document.createElement('input'); s.form.append(dependent)
      const before = formInstance(s.group)
      s.api.clickElement = vi.fn(async () => {throw new Error('controller unavailable')})
      await expect(s.page.select('e1', 'true', 'Public')).rejects.toThrow(/controller unavailable/)
      dependent.remove()
      expect(formInstance(s.group)).not.toBe(before)
      expect(s.api.clickElement).toHaveBeenCalledTimes(1)
    } finally {s.form.remove()}
  })
})
