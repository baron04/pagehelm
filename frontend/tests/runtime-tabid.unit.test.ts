import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'

/**
 * The tab identity in `runtime/tabid.ts`: same-tab navigation keeps it, a duplicated
 * tab adopts a fresh one. `navigator.locks` is faked so the test decides who holds what.
 */

const KEY = 'woa.assistant.tab.v1'

interface HeldLock {name: string}

function fakeLocks(held = new Set<string>()) {
  return {
    held,
    request: vi.fn((name: string, options: unknown, callback?: (lock: HeldLock | null) => unknown) => {
      if (typeof options === 'function') { callback = options as typeof callback; options = {} }
      const ifAvailable = Boolean((options as {ifAvailable?: boolean})?.ifAvailable)
      if (ifAvailable && held.has(name)) return Promise.resolve(callback?.(null))
      held.add(name)
      // A callback that keeps its promise keeps its lock for the document's life.
      return new Promise(resolve => {
        const outcome = callback?.({name})
        if (outcome && typeof (outcome as Promise<unknown>).then === 'function') {
          (outcome as Promise<unknown>).then(resolve)
        } else {
          resolve(outcome)
        }
      })
    }),
  }
}

function stubLocks(locks: {request: unknown}) {
  Object.defineProperty(navigator, 'locks', {value: locks, configurable: true})
}

async function freshTabId() {
  vi.resetModules()
  return await import('../src/runtime/tabid')
}

beforeEach(() => {
  sessionStorage.clear()
  delete (navigator as {locks?: unknown}).locks
})

afterEach(() => {
  sessionStorage.clear()
  delete (navigator as {locks?: unknown}).locks
})

describe('the tab identity', () => {
  it('mints one for a new tab and keeps it for the document', async () => {
    const locks = fakeLocks()
    stubLocks(locks)
    const {tabId, tabIdNow} = await freshTabId()
    const id = await tabId()
    expect(id).toMatch(/^[0-9a-f]{32}$/)
    expect(sessionStorage.getItem(KEY)).toBe(id)
    expect(tabIdNow()).toBe(id)
    expect(locks.request).toHaveBeenCalledOnce()
    await expect(tabId()).resolves.toBe(id)
    expect(locks.request).toHaveBeenCalledOnce()
  })

  it('is reclaimed by the tab it was stored for: a reload is the same tab', async () => {
    sessionStorage.setItem(KEY, 'a'.repeat(32))
    const locks = fakeLocks()
    stubLocks(locks)
    const {tabId, whenTabIdRotated} = await freshTabId()
    const rotated = vi.fn()
    whenTabIdRotated(rotated)
    await expect(tabId()).resolves.toBe('a'.repeat(32))
    expect(rotated).not.toHaveBeenCalled()
    expect(locks.held.has('woa.assistant.tab.' + 'a'.repeat(32))).toBe(true)
  })

  it('is surrendered by a duplicated tab, which keeps a fresh one and says so', async () => {
    const original = 'b'.repeat(32)
    sessionStorage.setItem(KEY, original)
    const locks = fakeLocks(new Set(['woa.assistant.tab.' + original]))
    stubLocks(locks)
    const {tabId, tabIdNow, whenTabIdRotated} = await freshTabId()
    const rotated = vi.fn()
    whenTabIdRotated(rotated)
    const id = await tabId()
    expect(id).not.toBe(original)
    expect(rotated).toHaveBeenCalledOnce()
    expect(sessionStorage.getItem(KEY)).toBe(id)
    // The fresh identity is held too: a duplicate of this duplicate rotates again.
    expect(locks.held.has('woa.assistant.tab.' + id)).toBe(true)
    expect(tabIdNow()).toBe(id)
  })

  it('stands on the stored value when Web Locks is not there', async () => {
    sessionStorage.setItem(KEY, 'c'.repeat(32))
    const {tabId} = await freshTabId()
    await expect(tabId()).resolves.toBe('c'.repeat(32))
  })
})
