import {describe, expect, it} from 'vitest'
import {createRedactor} from '../src/runtime/redaction'
import {
  actionElementFingerprint, boundIndexedContent, elementsFrom, explicitAttribute, hydrateButtonFacts, hydrateFormFacts,
  hydrateLinkFacts, hydrateRevealFacts, hydrateSelectionFacts, formControlLabel, formControlScope,
  refIndex, revealable, type ElementLike,
} from '../src/runtime/pageModel'

const redactor = createRedactor()

describe('observable form labels and scopes', () => {
  it('projects only a unique live single-choice option and removes stale or sensitive hints', () => {
    const form = document.createElement('form')
    form.innerHTML = '<input role="combobox" aria-haspopup="listbox" aria-controls="choice-options">' +
      '<ul id="choice-options" role="listbox"><li role="option" aria-selected="true">Waiting</li>' +
      '<li role="option" aria-selected="false">Ready</li></ul>'
    document.body.append(form)
    try {
      const input = form.querySelector('input')!
      const elements = elementsFrom('[0]<input role=combobox />', 1, redactor)
      elements[0]!.attrs.selected_option = 'invented'
      hydrateFormFacts(elements, {}, () => input, redactor)
      expect(elements[0]!.attrs.selected_option).toBe('Waiting')
      expect(elements[0]!.attrs['aria-controls']).toBe('choice-options')
      form.querySelectorAll('li')[1]!.setAttribute('aria-selected', 'true')
      hydrateFormFacts(elements, {}, () => input, redactor)
      expect(elements[0]!.attrs.selected_option).toBeUndefined()
      form.querySelectorAll('li')[1]!.setAttribute('aria-selected', 'false')
      input.name = 'password'
      hydrateFormFacts(elements, {}, () => input, redactor)
      expect(elements[0]!.attrs.selected_option).toBeUndefined()
    } finally { form.remove() }
  })
  it('projects only the unique ARIA listbox owning an option', () => {
    const list = document.createElement('ul')
    list.id = 'paging-options'; list.setAttribute('role', 'listbox')
    list.innerHTML = '<li role="option">20</li>'
    document.body.append(list)
    try {
      const elements = elementsFrom('[0]<li role=option>20 />', 1, redactor)
      hydrateFormFacts(elements, {}, () => list.firstElementChild!, redactor)
      expect(elements[0]!.attrs.listbox_owner).toBe('paging-options')
      const duplicate = list.cloneNode() as HTMLElement; document.body.append(duplicate)
      hydrateFormFacts(elements, {}, () => list.firstElementChild!, redactor)
      expect(elements[0]!.attrs.listbox_owner).toBeUndefined()
      duplicate.remove()
    } finally { list.remove() }
  })
  it('binds an idless native search form to one DOM lifetime without inventing an id', () => {
    const form = document.createElement('form')
    form.innerHTML = '<label for="search-name">Name</label><input id="search-name"><button>Search</button>'
    document.body.append(form)
    try {
      const input = form.querySelector('input')!, button = form.querySelector('button')!
      const scope = formControlScope(input)
      expect(scope).toMatch(/^form_instance:[0-9a-f-]{36}$/)
      expect(formControlScope(button)).toBe(scope)
      expect(form.id).toBe('')
      const replacement = form.cloneNode(true) as HTMLFormElement
      form.replaceWith(replacement)
      expect(formControlScope(replacement.querySelector('input')!)).not.toBe(scope)
      replacement.remove()
    } finally {form.remove()}
  })
  it('uses the unique ARIA body association when a custom header omits its title ID', () => {
    const dialog = document.createElement('section')
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('aria-labelledby', 'missing-title')
    dialog.setAttribute('aria-describedby', 'editor-body')
    dialog.innerHTML = '<header>Create</header><section id="editor-body"><input aria-label="Name"></section>'
    document.body.append(dialog)
    try {
      const input = dialog.querySelector('input')!
      expect(formControlScope(input)).toBe('dialog_describedby:editor-body')
      const before = actionElementFingerprint(input)
      dialog.querySelector('header')!.textContent = 'Update'
      expect(actionElementFingerprint(input)).not.toBe(before)
      const duplicate = document.createElement('section')
      duplicate.id = 'editor-body'; dialog.append(duplicate)
      expect(formControlScope(input)).toBeNull()
    } finally { dialog.remove() }
  })
  it('associates label/for inputs and the dialog footer through the same ARIA dialog', () => {
    const dialog = document.createElement('section')
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('aria-labelledby', 'editor-title')
    dialog.innerHTML = '<h2 id="editor-title">Create</h2><form><label for="native-name">Name</label>' +
      '<input id="native-name"></form><button type="button">Confirm</button>'
    document.body.append(dialog)
    try {
      const input = dialog.querySelector('input')!, button = dialog.querySelector('button')!
      expect(formControlLabel(input)).toBe('Name')
      expect(formControlScope(input)).toBe('dialog_labelledby:editor-title')
      expect(formControlScope(button)).toBe(formControlScope(input))
      const elements = elementsFrom('[0]<input type=text />\n[1]<button>Confirm />', 1, redactor)
      hydrateFormFacts(elements, {}, (_map, index) => [input, button][index], redactor)
      expect(elements[0]?.attrs).toMatchObject({field_label: 'Name', form_scope: 'dialog_labelledby:editor-title'})
      expect(elements[1]?.attrs.form_scope).toBe('dialog_labelledby:editor-title')
      const before = actionElementFingerprint(input)
      dialog.querySelector('h2')!.textContent = 'Update'
      expect(actionElementFingerprint(input)).not.toBe(before)
    } finally { dialog.remove() }
  })

  it('refuses duplicate identities and conflicting accessible labels', () => {
    const form = document.createElement('form')
    form.id = 'editor'
    form.innerHTML = '<label for="n">Name</label><input id="n" aria-label="Other">'
    document.body.append(form)
    const duplicate = document.createElement('form')
    duplicate.id = 'editor'
    document.body.append(duplicate)
    try {
      const input = form.querySelector('input')!
      expect(formControlLabel(input)).toBeNull()
      expect(formControlScope(input)).toBeNull()
      duplicate.remove()
      input.removeAttribute('aria-label')
      expect(formControlLabel(input)).toBe('Name')
      expect(formControlScope(input)).toBe('form:editor')
      input.setAttribute('aria-labelledby', 'missing')
      expect(formControlLabel(input)).toBeNull()
    } finally { form.remove(); duplicate.remove() }
  })

  it('reads ARIA labels without treating placeholders as field bindings', () => {
    const form = document.createElement('form')
    form.id = 'editor'
    form.innerHTML = '<span id="label">Full name</span><input aria-labelledby="label">' +
      '<input placeholder="Name">'
    document.body.append(form)
    try {
      const inputs = form.querySelectorAll('input')
      expect(formControlLabel(inputs[0])).toBe('Full name')
      expect(formControlLabel(inputs[1])).toBeNull()
    } finally { form.remove() }
  })
})

describe('boundIndexedContent limits the live control surface without cutting a record', () => {
  it('retains the first bounded records and reports omitted controls', () => {
    const content = Array.from({length: 205}, (_, index) =>
      `[${index}]<button>Action ${index} />`).join('\n')

    const bounded = boundIndexedContent(content, 200)

    expect(bounded.truncated).toBe(true)
    expect(elementsFrom(bounded.content, 1, redactor)).toHaveLength(200)
    expect(bounded.content).toContain('[199]<button>Action 199 />')
    expect(bounded.content).not.toContain('[200]<button>Action 200 />')
  })

  it('leaves a short observation unchanged and validates its bound', () => {
    expect(boundIndexedContent('[0]<button>Only />', 2)).toEqual({
      content: '[0]<button>Only />', truncated: false,
    })
    expect(() => boundIndexedContent('', 0)).toThrow(/positive integer/)
  })
})

describe('refIndex parses the server ref format and nothing else', () => {
  it('extracts the numeric index from e<number>', () => {
    expect(refIndex('e12')).toBe(12)
    expect(refIndex('e0')).toBe(0)
  })

  it('rejects anything that is not exactly that shape', () => {
    expect(() => refIndex('12')).toThrow(/bad_arguments/)
    expect(() => refIndex('e')).toThrow(/bad_arguments/)
    expect(() => refIndex(undefined)).toThrow(/bad_arguments/)
  })
})

describe('explicitAttribute reads only what the element itself declares', () => {
  it('reads a real element through hasAttribute/getAttribute', () => {
    const element = {
      hasAttribute: (name: string) => name === 'href',
      getAttribute: (name: string) => (name === 'href' ? '/next' : null),
    }
    expect(explicitAttribute(element, 'href')).toBe('/next')
    expect(explicitAttribute(element, 'target')).toBeNull()
  })

  it('falls back to a plain object property for test doubles', () => {
    const control = {formaction: '/save'} as unknown as Parameters<typeof explicitAttribute>[0]
    expect(explicitAttribute(control, 'formaction')).toBe('/save')
    expect(explicitAttribute({}, 'formaction')).toBeNull()
  })

  it('returns null for a missing element', () => {
    expect(explicitAttribute(null, 'href')).toBeNull()
  })
})

describe('elementsFrom parses PageController records into the server schema', () => {
  it('reads role, tag and a bounded accessible name from one record', () => {
    const content = '[4]<a href=/next>Open next />'
    const elements = elementsFrom(content, 3, redactor)
    expect(elements).toEqual([
      {ref: 'e4', role: 'link', name: 'Open next', tag: 'a', epoch: 3,
        attrs: {href: '/next'}},
    ])
  })

  it('names an icon-only control by title, then by a tooltip attribute', () => {
    const elements = elementsFrom(
      '[1]<button title=Remove />\n[2]<button data-tooltip=Kanban />\n' +
      '[3]<button aria-label=Next data-tooltip=Other />', 1, redactor)
    expect(elements.map(element => element.name)).toEqual(['Remove', 'Kanban', 'Next'])
  })

  it('uses the button value as the accessible name only for submit-typed inputs', () => {
    const elements = elementsFrom('[9]<input type=submit name=find value=Find />', 1, redactor)
    expect(elements[0]?.role).toBe('button')
    expect(elements[0]?.name).toBe('Find')
  })

  it('never lets a textbox value become its accessible name', () => {
    const elements = elementsFrom('[5]<input type=text name=note value=field-secret />', 1, redactor)
    expect(elements[0]?.role).toBe('textbox')
    expect(elements[0]?.name).not.toContain('field-secret')
  })

  it('blanks the accessible name for a field whose own name is sensitive', () => {
    // Only the derived accessible name is blanked here; attrs.name is the raw parsed
    // attribute value, which redactString leaves alone because it is not itself a
    // credential assignment (redaction of the value attribute is elementsFrom's job,
    // exercised by the textbox-value case above).
    const withKey = createRedactor(['externalLoginKey'])
    const elements = elementsFrom(
      '[5]<input type=text name=externalLoginKey value=field-secret />', 1, withKey)
    expect(elements[0]?.name).toBe('')
  })

  it('bounds the accessible name to 120 characters', () => {
    const long = 'x'.repeat(500)
    const elements = elementsFrom(`[0]<button>${long} />`, 1, redactor)
    expect(elements[0]?.name.length).toBe(120)
  })

  it('parses multiple records in one pass', () => {
    const content = [
      '[0]<button>First />',
      '[1]<input type=text name=note value=public />',
    ].join('\n')
    expect(elementsFrom(content, 1, redactor)).toHaveLength(2)
  })
})

describe('actionElementFingerprint captures everything an action depends on', () => {
  it('reports missing for a null or disconnected element', () => {
    expect(actionElementFingerprint(null)).toBe('missing')
    expect(actionElementFingerprint({isConnected: false})).toBe('missing')
  })

  it('changes when the element text or disabled state changes', () => {
    const base = {tagName: 'BUTTON', textContent: 'Save', disabled: false, isConnected: true}
    const changedText = {...base, textContent: 'Cancel'}
    const changedDisabled = {...base, disabled: true}
    expect(actionElementFingerprint(base)).not.toBe(actionElementFingerprint(changedText))
    expect(actionElementFingerprint(base)).not.toBe(actionElementFingerprint(changedDisabled))
  })

  it('changes when a tab selection changes without changing its label', () => {
    const selected = {tagName: 'BUTTON', textContent: 'Terms', isConnected: true,
      hasAttribute: (name: string) => name === 'aria-selected',
      getAttribute: () => 'true'}
    const unselected = {...selected, getAttribute: () => 'false'}
    expect(actionElementFingerprint(selected)).not.toBe(
      actionElementFingerprint(unselected))
  })

  it('is stable for the same element read twice', () => {
    const element = {tagName: 'A', href: 'https://app.test/x', isConnected: true}
    expect(actionElementFingerprint(element)).toBe(actionElementFingerprint({...element}))
  })
})

describe('hydrateLinkFacts replaces the lossy display href/target with the live DOM truth', () => {
  it('overwrites a truncated href from PageController with the exact live one', () => {
    const elements = elementsFrom('[0]<a href=/trunc...>Open />', 1, redactor)
    const element = {hasAttribute: (n: string) => n === 'href',
      getAttribute: (n: string) => (n === 'href' ? '/exact/long/path' : null)}
    hydrateLinkFacts(elements, {}, () => element, redactor)
    expect(elements[0]?.attrs.href).toBe('/exact/long/path')
  })

  it('leaves the summary alone when the live element cannot be resolved', () => {
    const elements = elementsFrom('[0]<a href=/x>Open />', 1, redactor)
    hydrateLinkFacts(elements, {}, () => { throw new Error('stale index') }, redactor)
    expect(elements[0]?.attrs.href).toBe('/x')
  })

  it('does not touch a non-link element', () => {
    const elements = elementsFrom('[0]<button>Save />', 1, redactor)
    hydrateLinkFacts(elements, {}, () => ({}), redactor)
    expect(elements[0]?.attrs.href).toBeUndefined()
  })
})

describe('hydrateFormFacts adds a bounded application field identifier', () => {
  it('observes live searchbox text without inventing a form and redacts sensitive search controls', () => {
    const fixture = document.createElement('div')
    fixture.innerHTML = '<input role="searchbox" aria-label="Search"><input role="searchbox" name="password">'
    document.body.append(fixture)
    try {
      const inputs = fixture.querySelectorAll('input')
      inputs[0]!.value = 'Blue'
      inputs[1]!.value = 'private'
      const elements = elementsFrom('[0]<input role=searchbox />\n[1]<input role=searchbox name=password value=stale />', 1, redactor)
      hydrateFormFacts(elements, {}, (_map, index) => inputs[index], redactor)
      expect(elements[0]?.attrs.value).toBe('Blue')
      expect(elements[0]?.attrs.form_scope).toBeUndefined()
      expect(elements[1]?.attrs.value).toBeUndefined()
      expect(elements[1]?.name).toBe('')
    } finally {fixture.remove()}
  })
  it('gives one live control its form facts once when two indexed hit areas resolve to it', () => {
    const dialog = document.createElement('section')
    dialog.setAttribute('role', 'dialog')
    dialog.id = 'order-editor'
    dialog.innerHTML = '<label for="customer">Customer</label><div class="wrapper" tabindex="-1">'
      + '<input id="customer" role="combobox" aria-controls="customer-list"></div>'
    document.body.append(dialog)
    try {
      const input = dialog.querySelector('input')!
      // The application controller maps both the wrapper and the input to the input.
      const elements = elementsFrom('[0]<div tabindex=-1 />\n[1]<input role=combobox />', 1, redactor)
      for (const item of elements) item.role = 'combobox'
      hydrateFormFacts(elements, {}, () => input, redactor)
      expect(elements.filter(item => item.attrs.field_label === 'Customer')).toHaveLength(1)
      expect(elements[0]?.attrs.form_scope).toBe('dialog:order-editor')
      expect(elements[1]?.attrs.form_scope).toBeUndefined()
      expect(elements[1]?.attrs.field_label).toBeUndefined()
    } finally { dialog.remove() }
  })
  it('does not turn a named HTML form into the identity of an unnamed field', () => {
    const fixture = document.createElement('form')
    fixture.name = 'customer'
    fixture.innerHTML = '<input><input name="email">'
    document.body.append(fixture)
    try {
      const inputs = fixture.querySelectorAll('input')
      const elements = elementsFrom('[0]<input type=text />\n[1]<input type=text name=email />', 1, redactor)
      hydrateFormFacts(elements, {}, (_map, index) => inputs[index], redactor)
      expect(elements[0]?.attrs.field).toBeUndefined()
      expect(elements[1]?.attrs.field).toBe('email')
    } finally { fixture.remove() }
  })

  it('invalidates an action when its named field wrapper changes', () => {
    const wrapper = document.createElement('div')
    wrapper.setAttribute('name', 'email')
    wrapper.innerHTML = '<input>'
    document.body.append(wrapper)
    try {
      const input = wrapper.querySelector('input')!
      const before = actionElementFingerprint(input)
      wrapper.setAttribute('name', 'telephone')
      expect(actionElementFingerprint(input)).not.toBe(before)
    } finally { wrapper.remove() }
  })

  it.each(['wrapper', 'type', 'autocomplete', 'id'])('removes even a parsed value for sensitive %s facts', source => {
    const wrapper = document.createElement('div')
    wrapper.innerHTML = '<input>'
    document.body.append(wrapper)
    try {
      const input = wrapper.querySelector('input')!
      input.value = 'private-credential'
      if (source === 'wrapper') wrapper.setAttribute('name', 'api_key')
      if (source === 'type') input.type = 'password'
      if (source === 'autocomplete') input.autocomplete = 'current-password'
      if (source === 'id') input.id = 'secret'
      const elements = elementsFrom('[0]<input type=text value=private-credential />\n' +
        '[1]<input type=text value=private-credential />', 1, redactor)
      hydrateFormFacts(elements, {}, () => input, redactor)
      for (const element of elements) {
        expect(element.attrs.value).toBeUndefined()
        expect(element.attrs.field).toBeUndefined()
        expect(element.name).toBe('')
      }
    } finally { wrapper.remove() }
  })

  it('reads name from the nearest [name] ancestor when the input itself has none', () => {
    const elements = elementsFrom('[0]<input type=text value=x />', 1, redactor)
    elements[0]!.role = 'textbox'
    const wrapper = {hasAttribute: (n: string) => n === 'name',
      getAttribute: (n: string) => (n === 'name' ? 'contact.email' : null)}
    const element = {closest: () => wrapper}
    hydrateFormFacts(elements, {}, () => element, redactor)
    expect(elements[0]?.attrs.field).toBe('contact.email')
  })

  it('scrubs sensitive radio aliases before grouping can skip a duplicate', () => {
    const group = document.createElement('div')
    group.setAttribute('role', 'radiogroup')
    group.innerHTML = '<label>A<input type="radio" name="api_key" value="private-credential"></label>' +
      '<label>B<input type="radio" name="api_key" value="another-credential"></label>'
    document.body.append(group)
    try {
      const elements = elementsFrom('[0]<input type=radio value=private-credential />\n' +
        '[1]<input type=radio value=private-credential />', 1, redactor)
      hydrateFormFacts(elements, {}, () => group.querySelector('input'), redactor)
      for (const element of elements) {
        expect(element.attrs.value).toBeUndefined()
        expect(element.attrs.field).toBeUndefined()
        expect(element.name).toBe('')
      }
    } finally { group.remove() }
  })

  it('omits a field identifier that looks like a credential name', () => {
    const elements = elementsFrom('[0]<input type=text />', 1, redactor)
    elements[0]!.role = 'textbox'
    const element = {closest: () => ({hasAttribute: () => true, getAttribute: () => 'password'})}
    hydrateFormFacts(elements, {}, () => element, redactor)
    expect(elements[0]?.attrs.field).toBeUndefined()
  })

  it('omits a field identifier with characters outside the safe pattern', () => {
    const elements = elementsFrom('[0]<input type=text />', 1, redactor)
    elements[0]!.role = 'textbox'
    const element = {closest: () => ({hasAttribute: () => true, getAttribute: () => 'has space'})}
    hydrateFormFacts(elements, {}, () => element, redactor)
    expect(elements[0]?.attrs.field).toBeUndefined()
  })

  it('reports what a text field currently shows, redacted and bounded', () => {
    const elements = elementsFrom('[0]<input type=text name=validity_date />', 1, redactor)
    elements[0]!.role = 'textbox'
    const element = {closest: () => null, value: '2026-10-31', name: 'validity_date',
      hasAttribute: () => false, getAttribute: () => null}
    hydrateFormFacts(elements, {}, () => element, redactor)
    expect(elements[0]?.attrs.value).toBe('2026-10-31')
  })

  it('never reports the value of a field whose name marks it sensitive', () => {
    const elements = elementsFrom('[0]<input type=text name=password />', 1, redactor)
    elements[0]!.role = 'textbox'
    const element = {closest: () => null, value: 'hunter2', name: 'password',
      hasAttribute: () => false, getAttribute: () => null}
    hydrateFormFacts(elements, {}, () => element, redactor)
    expect(elements[0]?.attrs.value).toBeUndefined()
  })

  it('reports disabled and read-only state from the live control', () => {
    const elements = elementsFrom('[0]<input type=text name=company />', 1, redactor)
    elements[0]!.role = 'textbox'
    const element = {name: 'company', disabled: true, readOnly: true, closest: () => null}

    hydrateFormFacts(elements, {}, () => element, redactor)

    expect(elements[0]?.attrs).toMatchObject({
      field: 'company', disabled: 'true', readonly: 'true',
    })
  })
})

describe('hydrateButtonFacts adds a bounded live method identifier', () => {
  it('marks native submit controls including implicit and externally associated buttons', () => {
    const fixture = document.createElement('div')
    fixture.innerHTML = '<form id="checkout"><button>Continue</button>' +
      '<button type="button">Details</button><input type="image" alt="Go"></form>' +
      '<button form="checkout" role="tab">Next</button><button>Outside</button>'
    document.body.append(fixture)
    try {
      const buttons = [...fixture.querySelectorAll('button,input')]
      const elements = elementsFrom('[0]<button>Continue />\n[1]<button>Details />\n[2]<input type=image />\n' +
        '[3]<button role=tab>Next />\n[4]<button native_submit=true>Outside />', 1, redactor)
      hydrateButtonFacts(elements, {}, (_, index) => buttons[index], redactor)
      expect(elements.map(item => item.attrs.native_submit)).toEqual(['true', undefined, 'true', 'true', undefined])
    } finally {
      fixture.remove()
    }
  })

  it('uses the indexed button rather than display text', () => {
    const elements = elementsFrom('[0]<button>Pay />', 1, redactor)
    hydrateButtonFacts(elements, {}, () => ({hasAttribute: (n: string) => n === 'name',
      getAttribute: () => 'action_register_payment'}), redactor)
    expect(elements[0]?.attrs.action_name).toBe('action_register_payment')
  })

  it('recovers an indexed blank native button name from visible text without widening observation', () => {
    const button = document.createElement('button')
    button.innerHTML = '<svg aria-hidden="true"></svg><span>新增</span>'
    Object.defineProperty(button, 'innerText', {value: '新增'})
    const elements = elementsFrom('[0]<button type=button />', 1, redactor)
    hydrateButtonFacts(elements, {}, () => button, redactor)
    expect(elements[0]?.name).toBe('新增')
    expect(elements).toHaveLength(1)
    const named = elementsFrom('[0]<button>Reviewed name />', 1, redactor)
    hydrateButtonFacts(named, {}, () => button, redactor)
    expect(named[0]?.name).toBe('Reviewed name')
    button.setAttribute('name', 'password')
    const sensitive = elementsFrom('[0]<button />', 1, redactor)
    hydrateButtonFacts(sensitive, {}, () => button, redactor)
    expect(sensitive[0]?.name).toBe('')
  })

  it('omits credential-shaped and malformed method names', () => {
    const elements = elementsFrom('[0]<button>Pay />', 1, redactor)
    hydrateButtonFacts(elements, {}, () => ({hasAttribute: () => true,
      getAttribute: () => 'password'}), redactor)
    expect(elements[0]?.attrs.action_name).toBeUndefined()
    hydrateButtonFacts(elements, {}, () => ({hasAttribute: () => true,
      getAttribute: () => 'bad method'}), redactor)
    expect(elements[0]?.attrs.action_name).toBeUndefined()
  })
})

describe('hydrateSelectionFacts says which tab is current when the page says it by class', () => {
  const tabs = (content: string) => elementsFrom(content, 1, redactor)

  it('checks the live linked panel even when aria-selected is already declared', () => {
    const fixture = document.createElement('div')
    fixture.innerHTML = '<button id="t" role="tab" aria-controls="p" aria-selected="true">Sales</button>' +
      '<section id="p" role="tabpanel" aria-labelledby="t"></section>'
    document.body.append(fixture)
    const tab = fixture.querySelector('button')!
    const panel = fixture.querySelector('section')!
    Object.defineProperty(panel, 'getClientRects', {value: () => [{width: 100, height: 50}]})
    const ready = () => {
      const elements = tabs('[0]<button role=tab aria-selected=true>Sales />')
      hydrateSelectionFacts(elements, {}, () => tab)
      return elements[0]?.attrs.panel_ready
    }
    expect(ready()).toBe('true') // An empty panel is still a valid panel.
    panel.setAttribute('aria-busy', 'true')
    expect(ready()).toBe('false')
    panel.removeAttribute('aria-busy')
    panel.hidden = true
    expect(ready()).toBe('false')
    panel.hidden = false
    panel.setAttribute('aria-labelledby', 'another-tab')
    expect(ready()).toBe('false')
    panel.setAttribute('aria-labelledby', 't')
    panel.insertAdjacentHTML('afterend', panel.outerHTML)
    expect(ready()).toBe('false') // Duplicate panel IDs cannot establish association.
    fixture.remove()
  })

  it('marks the tab whose live element carries the current-state class', () => {
    const elements = tabs('[0]<a role=tab>General />\n[1]<a role=tab>Sales />')
    const live = [{className: 'nav-link'}, {className: 'nav-link active'}]
    hydrateSelectionFacts(elements, {}, (_map, index) => live[index])
    expect(elements.map(item => item.attrs.selected)).toEqual([undefined, 'true'])
  })

  it('reads aria-current as current too', () => {
    const elements = tabs('[0]<a role=tab>Sales />')
    hydrateSelectionFacts(elements, {}, () => ({
      hasAttribute: (name: string) => name === 'aria-current',
      getAttribute: (name: string) => (name === 'aria-current' ? 'page' : null)}))
    expect(elements[0]?.attrs.selected).toBe('true')
  })

  it('leaves a tab that declares aria-selected, a non-tab, and a stale index alone', () => {
    const declared = tabs('[0]<a role=tab aria-selected=false>Sales />')
    hydrateSelectionFacts(declared, {}, () => ({className: 'nav-link active'}))
    expect(declared[0]?.attrs.selected).toBeUndefined()
    const button = tabs('[0]<button>Sales />')
    hydrateSelectionFacts(button, {}, () => ({className: 'btn active'}))
    expect(button[0]?.attrs.selected).toBeUndefined()
    const stale = tabs('[0]<a role=tab>Sales />')
    hydrateSelectionFacts(stale, {}, () => { throw new Error('stale index') })
    expect(stale[0]?.attrs.selected).toBeUndefined()
  })

  it('marks the sole active button in a grouped view switch, not a lone active button', () => {
    const elements = tabs('[0]<button data-tooltip=List />\n[1]<button data-tooltip=Kanban />')
    const live: ElementLike[] = [
      {className: 'btn o_list'}, {className: 'btn o_kanban active'},
    ]
    const group = {querySelectorAll: () => live}
    for (const button of live) button.closest = () => group
    hydrateSelectionFacts(elements, {}, (_map, index) => live[index])
    expect(elements.map(item => item.attrs.selected)).toEqual([undefined, 'true'])

    const lone = tabs('[0]<button data-tooltip=Kanban />')
    hydrateSelectionFacts(lone, {}, () => ({className: 'btn active'}))
    expect(lone[0]?.attrs.selected).toBeUndefined()
  })
})

describe('revealable: a collapsed menu control the Runtime may open, in any application', () => {
  const control = (attrs: Record<string, string>, extra: Partial<ElementLike> = {},
                   inNavigation = false): ElementLike => ({
    tagName: 'BUTTON', isConnected: true, form: null, ...extra,
    hasAttribute: (name: string) => Object.hasOwn(attrs, name),
    getAttribute: (name: string) => attrs[name] ?? null,
    closest: () => (inNavigation ? ({} as ElementLike) : null),
  })

  it('opens a button that declares itself collapsed or declares an unopened popup', () => {
    expect(revealable(control({'aria-expanded': 'false'}))).toBe(true)
    expect(revealable(control({'aria-haspopup': 'menu'}))).toBe(true)
    expect(revealable(control({role: 'button', 'aria-expanded': 'false'},
      {tagName: 'DIV'}))).toBe(true)
  })

  it('refuses an open menu, a plain button, inputs, submit buttons and navigation', () => {
    expect(revealable(control({'aria-expanded': 'true'}))).toBe(false)
    expect(revealable(control({'aria-expanded': 'true', 'aria-haspopup': 'menu'}))).toBe(false)
    expect(revealable(control({}))).toBe(false)
    expect(revealable(control({'aria-haspopup': 'false'}))).toBe(false)
    expect(revealable(control({'aria-expanded': 'false', role: 'combobox'},
      {tagName: 'INPUT'}))).toBe(false)
    expect(revealable(control({'aria-expanded': 'false', type: 'submit'}))).toBe(false)
    // An untyped button submits the form it is in; outside any form it submits nothing.
    expect(revealable(control({'aria-expanded': 'false'}, {form: {action: '/save'}}))).toBe(false)
    expect(revealable(control({'aria-expanded': 'false', type: 'button'},
      {form: {action: '/save'}}))).toBe(true)
    expect(revealable(control({'aria-expanded': 'false'}, {disabled: true}))).toBe(false)
    expect(revealable(control({'aria-expanded': 'false'}, {}, true))).toBe(false)
    expect(revealable(control({'aria-expanded': 'false'}, {tagName: 'A'}))).toBe(false)
    expect(revealable(null)).toBe(false)
  })

  it('marks only the observed buttons it may open', () => {
    const elements = elementsFrom('[0]<button>Actions />\n[1]<button>Save />', 1, redactor)
    const live = [control({'aria-expanded': 'false'}), control({})]
    hydrateRevealFacts(elements, {}, (_map, index) => live[index])
    expect(elements.map(item => item.attrs.revealable)).toEqual(['true', undefined])
  })
})
