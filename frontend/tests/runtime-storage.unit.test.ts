import {beforeEach, describe, expect, it, vi} from 'vitest'
import {createRedactor} from '../src/runtime/redaction'
import {clearStored, loadSession, readStored, writeStored} from '../src/runtime/storage'

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
  // Unique session ids: once a document has written a session, reads answer from the
  // in-memory mirror, and the suite shares that map across its cases.
  it('returns an empty object rather than throwing on invalid JSON', () => {
    sessionStorage.setItem('woa.remote.v1.s-corrupt', '{not json')
    expect(readStored('s-corrupt')).toEqual({})
  })

  it('returns an empty object when nothing was ever stored', () => {
    expect(readStored('never-seen')).toEqual({})
  })
})

describe('storage that refuses -- Safari private mode, a full quota', () => {
  it('writeStored survives a throwing setItem, and the mirror keeps the page coherent', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new DOMException('QuotaExceededError')
      })
    try {
      expect(() => writeStored('s-throw', {epoch: 3})).not.toThrow()
    } finally {
      spy.mockRestore()
    }
    // The real storage never got the value; the mirror is what the rest of the document
    // reads back until a navigation -- the one thing a mirror cannot survive.
    expect(readStored('s-throw')).toEqual({epoch: 3})
    expect(sessionStorage.getItem('woa.remote.v1.s-throw')).toBe(null)
  })

  it('readStored survives a throwing getItem -- a session never mirrored, storage unreadable', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem')
      .mockImplementation(() => {
        throw new DOMException('SecurityError')
      })
    try {
      expect(readStored('s-blocked')).toEqual({})
    } finally {
      spy.mockRestore()
    }
  })

  it('loadSession does not throw on boot when storage is hostile', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new DOMException('QuotaExceededError')
      })
    try {
      expect(() => loadSession('s-boot', 'gen-9', redactor)).not.toThrow()
    } finally {
      spy.mockRestore()
    }
  })
})

describe('clearStored says goodbye to a finished session', () => {
  it('removes both the mirror and the real entry', () => {
    writeStored('s-done', {epoch: 5})
    expect(readStored('s-done')).toEqual({epoch: 5})
    clearStored('s-done')
    expect(readStored('s-done')).toEqual({})
    expect(sessionStorage.getItem('woa.remote.v1.s-done')).toBe(null)
  })

  it('survives a throwing removeItem', () => {
    const spy = vi.spyOn(Storage.prototype, 'removeItem')
      .mockImplementation(() => {
        throw new DOMException('SecurityError')
      })
    try {
      expect(() => clearStored('s-any')).not.toThrow()
    } finally {
      spy.mockRestore()
    }
  })
})
