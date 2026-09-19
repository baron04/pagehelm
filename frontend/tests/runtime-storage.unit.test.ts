import {beforeEach, describe, expect, it} from 'vitest'
import {createRedactor} from '../src/runtime/redaction'
import {loadSession, readStored, writeStored} from '../src/runtime/storage'

const redactor = createRedactor()

beforeEach(() => { sessionStorage.clear() })

describe('loadSession normalizes and persists the stored state for this incarnation', () => {
  it('requires a sessionGeneration', () => {
    expect(() => loadSession('s1', '', redactor)).toThrow(/sessionGeneration/)
  })

  it('starts fresh, with every field present and defaulted', () => {
    const state = loadSession('s1', 'gen-1', redactor)
    expect(state).toMatchObject({
      sessionGeneration: 'gen-1', epoch: 0, lastEventId: '', inFlight: null,
      cachedResult: null, approvals: {}, taskInFlight: false, activeMessageId: '',
      cancellationPending: false,
    })
    expect(state.clientId).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('reloading the same generation keeps clientId and epoch', () => {
    const first = loadSession('s1', 'gen-1', redactor)
    writeStored('s1', {...first, epoch: 7})
    const second = loadSession('s1', 'gen-1', redactor)
    expect(second.clientId).toBe(first.clientId)
    expect(second.epoch).toBe(7)
  })

  it('a new generation discards everything the old one stored', () => {
    const first = loadSession('s1', 'gen-1', redactor)
    writeStored('s1', {...first, epoch: 7, lastEventId: 'e7'})
    const renewed = loadSession('s1', 'gen-2', redactor)
    expect(renewed.epoch).toBe(0)
    expect(renewed.lastEventId).toBe('')
    expect(renewed.clientId).not.toBe(first.clientId)
  })
})

describe('readStored tolerates a missing or corrupt entry', () => {
  it('returns an empty object rather than throwing on invalid JSON', () => {
    sessionStorage.setItem('woa.remote.v1.s1', '{not json')
    expect(readStored('s1')).toEqual({})
  })

  it('returns an empty object when nothing was ever stored', () => {
    expect(readStored('never-seen')).toEqual({})
  })
})
