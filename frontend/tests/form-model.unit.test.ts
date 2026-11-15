import {describe, expect, it} from 'vitest'
import {canonicalJson, formCheck, normalizeFormRead, normalizeRowRead, pressConfirmButton,
  readConfirmBox, sameControlText, waitForSubmitToSettle} from '../src/runtime/formModel'

const read = (model: Record<string, unknown>, extra: Record<string, unknown> = {}) => ({
  model, items: [{prop: 'name', label: '姓名', display: '张三'}], mode: 'update', record_id: '7', title: '编辑', ...extra})

describe('form model reading', () => {
  it('gives the same check for the same data whatever the key order', () => {
    expect(canonicalJson({b: 1, a: [{d: 1, c: 2}]})).toBe('{"a":[{"c":2,"d":1}],"b":1}')
    expect(normalizeFormRead(read({a: 1, b: 2}))?.check).toBe(normalizeFormRead(read({b: 2, a: 1}))?.check)
  })

  it('notices a hidden field, a line or a related id changing', () => {
    const base = normalizeFormRead(read({id: 7, customerId: 3, lines: [{id: 1, qty: 2}]}))!
    for (const changed of [{id: 7, customerId: 4, lines: [{id: 1, qty: 2}]},
                           {id: 7, customerId: 3, lines: [{id: 1, qty: 3}]},
                           {id: 7, customerId: 3, lines: []}, {id: 7, customerId: 3, lines: [{id: 1, qty: 2}], hidden: 1}]) {
      expect(normalizeFormRead(read(changed))!.check).not.toBe(base.check)
    }
  })

  it('carries what was withheld into the check, and refuses anything that is not a form', () => {
    expect(normalizeFormRead(read({a: 1}, {withheld: ['password']}))!.check)
      .not.toBe(normalizeFormRead(read({a: 1}))!.check)
    expect(normalizeFormRead(null)).toBeNull()
    expect(normalizeFormRead({model: [], mode: 'update'})).toBeNull()
    expect(normalizeFormRead(read({a: 1}, {mode: 'view'}))).toBeNull()
    expect(normalizeFormRead(read({blob: 'x'.repeat(250_000)}))).toBeNull()
  })

  it('keeps a record id only for an edit and bounds labels', () => {
    expect(normalizeFormRead(read({a: 1}, {mode: 'create', record_id: '9'}))!.record_id).toBe('')
    const long = normalizeFormRead(read({a: 1}, {items: [{prop: 'a', label: 'x'.repeat(500), display: 'y\u0001'}]}))!
    expect(long.items[0]!.label.length).toBe(120)
    expect(long.items[0]!.display).toBe('y ')
  })

  it('reads a table row only when it has a plain identity', () => {
    expect(normalizeRowRead({id: '12', name: '张三'})).toMatchObject({record_id: '12', name: '张三', title: '', mode: 'action'})
    expect(normalizeRowRead({id: '../x'})).toBeNull()
    expect(normalizeRowRead(undefined)).toBeNull()
    expect(formCheck('a')).not.toBe(formCheck('b'))
  })

  it('forwards a read source only when the integration vouches for it', () => {
    const sourced = read({a: 1}, {source: '/api/contacts'})
    expect(normalizeFormRead(sourced)!.source).toBeUndefined()
    const accept = {acceptReadSource: (source: string, kind: string) =>
      kind === 'form' && source === '/api/contacts'}
    expect(normalizeFormRead(sourced, accept)!.source).toBe('/api/contacts')
    const other = read({a: 1}, {source: '/api/other'})
    expect(normalizeFormRead(other, accept)!.source).toBeUndefined()
  })

  it('vouches a row source as a row, never as a form', () => {
    const row = {id: 'grace-hopper', name: 'Grace', source: '/api/contacts'}
    const seen: string[] = []
    const accept = {acceptReadSource: (source: string, kind: string) => {
      seen.push(kind)
      return kind === 'row' && source === '/api/contacts'
    }}
    expect(normalizeRowRead(row, accept)!.source).toBe('/api/contacts')
    expect(normalizeRowRead(row)!.source).toBeUndefined()
    expect(seen).toEqual(['row'])
  })

  it('fails closed on confirmation and settle facts without an integration', async () => {
    expect(readConfirmBox().open).toBe(false)
    expect(pressConfirmButton('确 定')).toBe(false)
    await expect(waitForSubmitToSettle()).rejects.toThrow('settle detection')
    expect(readConfirmBox(undefined,
      {readConfirmBox: () => ({open: true, title: 't', text: 'x', buttons: ['确定']})}).open).toBe(true)
  })

  it('compares a control with the card by its own text, and binds one with no text otherwise', () => {
    expect(sameControlText('确 定', '确定')).toBe(true)
    expect(sameControlText('确定', '取消')).toBe(false)
    expect(sameControlText('状态开关：张三', '')).toBe(true)
    expect(sameControlText('', '确定')).toBe(true)
  })
})
