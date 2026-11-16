/**
 * The tab's own identity: stable while the tab lives, surrendered when it is duplicated.
 *
 * `sessionStorage` keeps the value across the tab's own reloads and in-tab navigation,
 * which is what makes a same-tab reload restore the same session. But the browser's
 * "duplicate tab" copies `sessionStorage` wholesale, so a duplicated tab would open
 * holding the original's identity -- and its session -- as a second owner.
 *
 * The Web Locks API settles it: the tab that owns the identity holds a lock named after
 * it for the document's lifetime. A duplicated tab finds the lock still held, generates
 * a fresh identity instead, and asks the caller to drop whatever session state it cloned.
 * Where Web Locks is unavailable there is no way to tell a duplicate from a first load,
 * so the stored identity stands -- a duplicated tab then collides with the original over
 * the session rather than silently taking it.
 */

const TAB_KEY = 'woa.assistant.tab.v1'
const LOCK_PREFIX = 'woa.assistant.tab.'

let acquired = ''
let ready: Promise<string> | null = null
/** A rotation that already happened: late listeners are told on registration. */
let rotatedFlag = false
/** The callback a rotation notifies once. */
let onRotated: (() => void) | null = null

const freshId = (): string =>
  (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replaceAll('-', '')

const read = (): string => {
  try { return sessionStorage.getItem(TAB_KEY) || '' } catch { return '' }
}

const write = (value: string): void => {
  // Storage may be denied; the id still lives in memory for this document's life.
  try { sessionStorage.setItem(TAB_KEY, value) } catch { /* keep the in-memory value */ }
}

/** A promise that never resolves, which is how a lock callback holds its lock. */
const held = (): Promise<never> => new Promise<never>(() => {})

/** Take an identity and keep its lock for the document's life. */
const hold = (id: string): Promise<string> =>
  new Promise(resolve => {
    navigator.locks.request(LOCK_PREFIX + id, () => {
      acquired = id
      write(id)
      resolve(id)
      return held()
    }).catch(() => {
      // The lock could not even be requested; the identity stands on its own.
      acquired ||= id
      write(acquired)
      resolve(acquired)
    })
  })

/**
 * Reclaim the stored identity, or rotate it. The tab's own reload finds the lock free --
 * the old document released it on unload -- while a duplicated tab finds it still held,
 * fires the rotation callback, and adopts a fresh identity it then holds itself.
 */
const reclaim = (id: string): Promise<string> =>
  new Promise(resolve => {
    navigator.locks.request(LOCK_PREFIX + id, {ifAvailable: true}, lock => {
      if (!lock) return undefined
      acquired = id
      resolve(id)
      return held()
    }).then(rotated => {
      if (rotated !== undefined) return
      // The lock was already held: this document is a duplicate of the tab owning it.
      // Rotation is committed first -- a listener that throws must not leave this tab
      // answering for the identity it was told to surrender.
      rotatedFlag = true
      try { onRotated?.() } catch { /* a listener's failure is its own */ }
      hold(freshId()).then(resolve)
    }).catch(() => {
      acquired ||= freshId()
      write(acquired)
      resolve(acquired)
    })
  })

/**
 * Resolve the tab's identity once per document. The first call does the work; later
 * calls return the same promise.
 */
export function tabId(): Promise<string> {
  if (ready) return ready
  const stored = read()
  if (!navigator.locks?.request) {
    acquired = stored || freshId()
    if (!stored) write(acquired)
    ready = Promise.resolve(acquired)
  } else if (!stored) {
    ready = hold(freshId())
  } else {
    ready = reclaim(stored)
  }
  return ready
}

/** What this document settled on, synchronously; '' before `tabId()` resolved. */
export function tabIdNow(): string { return acquired }

/** Register the one callback a rotation notifies; returns an unregister. */
export function whenTabIdRotated(callback: () => void): () => void {
  onRotated = callback
  if (rotatedFlag) {
    onRotated = null
    try { callback() } catch { /* a listener's failure is its own */ }
  }
  return () => { if (onRotated === callback) onRotated = null }
}
