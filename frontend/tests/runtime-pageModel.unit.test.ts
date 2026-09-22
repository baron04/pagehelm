import {describe, expect, it} from 'vitest'
import {createRedactor} from '../src/runtime/redaction'
import {
  actionElementFingerprint, boundIndexedContent, elementsFrom, explicitAttribute, hydrateFormFacts,
  hydrateLinkFacts, refIndex,
} from '../src/runtime/pageModel'

const redactor = createRedactor()

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
  it('reads name from the nearest [name] ancestor when the input itself has none', () => {
    const elements = elementsFrom('[0]<input type=text value=x />', 1, redactor)
    elements[0]!.role = 'textbox'
    const wrapper = {hasAttribute: (n: string) => n === 'name',
      getAttribute: (n: string) => (n === 'name' ? 'contact.email' : null)}
    const element = {closest: () => wrapper}
    hydrateFormFacts(elements, {}, () => element, redactor)
    expect(elements[0]?.attrs.field).toBe('contact.email')
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
