/**
 * Bounded post-action page settle watcher.
 *
 * Waits for substantive DOM mutations and recent in-flight network requests to settle
 * after an action, while ignoring:
 * - no-op attribute mutations (`oldValue === currentValue`)
 * - assistant panel / controller annotation DOM mutations
 * - long-tail in-flight network requests older than 1500ms so settle never stalls 5s+.
 */
import {later, listen, type Cancel} from './contain'

export const DEFAULT_SETTLE_QUIET_MS = 80
export const DEFAULT_SETTLE_MAX_WAIT_MS = 2000
export const DEFAULT_LONG_TAIL_REQUEST_MS = 1500

export interface SettleOptions {
  doc?: Document
  win?: Window
  panelContainer?: HTMLElement | null
  quietMs?: number
  maxWaitMs?: number
  longTailMs?: number
  ignoreUrl?: (url: string) => boolean
  isBusy?: () => boolean
  /** Optional external active request timestamps (epoch ms) for deterministic testing. */
  activeRequestTimestamps?: () => number[]
  now?: () => number
}

export interface SettleResult {
  settled: boolean
  reason: 'quiet' | 'timeout'
  ignoredMutations: number
  ignoredLongTailRequests: number
}

export interface PageSettleWatch {
  settle(): Promise<SettleResult>
  dispose(): void
}

function defaultIgnoreUrl(url: string, win: Window | undefined): boolean {
  if (!url) return false
  try {
    const resolved = new URL(url, win?.location?.href || 'http://localhost')
    if (resolved.pathname.startsWith('/woa/')) return true
    if (resolved.pathname.endsWith('/events')) return true
  } catch {
    // Keep tracking unparseable relative URLs unless explicitly ignored.
  }
  return false
}

export function isSubstantiveMutation(
  record: MutationRecord,
  panelContainer?: HTMLElement | null,
): boolean {
  const target = record.target
  if (panelContainer && (target === panelContainer || panelContainer.contains(target))) {
    return false
  }
  if (typeof Element !== 'undefined' && target instanceof Element) {
    if (target.id === 'playwright-highlight-container' ||
        target.closest('#playwright-highlight-container')) {
      return false
    }
  }
  if (record.type === 'attributes') {
    if (!(typeof Element !== 'undefined' && target instanceof Element) || !record.attributeName) {
      return false
    }
    const currentValue = target.getAttribute(record.attributeName)
    if (record.oldValue === currentValue) {
      return false
    }
  }
  return true
}

export function countRecentInFlightRequests(
  timestamps: Iterable<number>,
  nowMs: number,
  longTailMs = DEFAULT_LONG_TAIL_REQUEST_MS,
): {recent: number; ignoredLongTail: number} {
  let recent = 0
  let ignoredLongTail = 0
  for (const startedAt of timestamps) {
    if (!Number.isFinite(startedAt)) continue
    if (nowMs - startedAt > longTailMs) {
      ignoredLongTail += 1
    } else {
      recent += 1
    }
  }
  return {recent, ignoredLongTail}
}

export function beginPageSettleWatch(options: SettleOptions = {}): PageSettleWatch {
  const doc = options.doc ?? (typeof document !== 'undefined' ? document : undefined)
  const win = options.win ?? (typeof window !== 'undefined' ? window : undefined)
  const now = options.now ?? (() => Date.now())
  const quietMs = Math.max(10, options.quietMs ?? DEFAULT_SETTLE_QUIET_MS)
  const maxWaitMs = Math.max(quietMs, options.maxWaitMs ?? DEFAULT_SETTLE_MAX_WAIT_MS)
  const longTailMs = Math.max(50, options.longTailMs ?? DEFAULT_LONG_TAIL_REQUEST_MS)
  const ignoreUrl = options.ignoreUrl ?? ((url: string) => defaultIgnoreUrl(url, win))

  let lastMutationAt = 0
  let ignoredMutations = 0
  let nextRequestId = 1
  const inFlight = new Map<number, number>()
  let disposed = false

  let observer: MutationObserver | null = null
  if (doc?.documentElement && typeof MutationObserver !== 'undefined') {
    try {
      observer = new MutationObserver(records => {
        if (disposed) return
        for (const record of records) {
          if (isSubstantiveMutation(record, options.panelContainer)) {
            lastMutationAt = now()
          } else {
            ignoredMutations += 1
          }
        }
      })
      observer.observe(doc.documentElement, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeOldValue: true,
      })
    } catch {
      observer = null
    }
  }

  let origFetch: typeof fetch | undefined
  let wrappedFetch: typeof fetch | undefined
  if (win && typeof win.fetch === 'function') {
    origFetch = win.fetch
    wrappedFetch = function patchedFetch(this: unknown, input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
      const url = typeof input === 'string' || input instanceof URL
        ? String(input) : String((input as Request)?.url || '')
      if (disposed || ignoreUrl(url)) {
        return origFetch!.call(this, input, init)
      }
      const id = nextRequestId++
      inFlight.set(id, now())
      try {
        return Promise.resolve(origFetch!.call(this, input, init)).finally(() => {
          inFlight.delete(id)
        })
      } catch (error) {
        inFlight.delete(id)
        throw error
      }
    }
    try {
      win.fetch = wrappedFetch
    } catch {
      wrappedFetch = undefined
    }
  }

  const xhrProto = (win as (Window & {XMLHttpRequest?: typeof XMLHttpRequest}) | undefined)?.XMLHttpRequest?.prototype
  const origOpen = xhrProto?.open
  const origSend = xhrProto?.send
  const xhrUrls = new WeakMap<XMLHttpRequest, string>()
  let wrappedOpen: typeof XMLHttpRequest.prototype.open | undefined
  let wrappedSend: typeof XMLHttpRequest.prototype.send | undefined
  if (xhrProto && typeof origOpen === 'function' && typeof origSend === 'function') {
    wrappedOpen = function patchedOpen(this: XMLHttpRequest, method: string, url: string | URL, ...rest: unknown[]) {
      xhrUrls.set(this, String(url))
      return (origOpen as (...args: unknown[]) => unknown).call(this, method, url, ...rest)
    } as typeof XMLHttpRequest.prototype.open
    wrappedSend = function patchedSend(this: XMLHttpRequest, ...args: unknown[]) {
      const url = xhrUrls.get(this) || ''
      if (disposed || ignoreUrl(url)) {
        return (origSend as (...sendArgs: unknown[]) => unknown).apply(this, args)
      }
      const id = nextRequestId++
      inFlight.set(id, now())
      listen('page settle xhr loadend', this, 'loadend', () => { inFlight.delete(id) }, {once: true})
      try {
        return (origSend as (...sendArgs: unknown[]) => unknown).apply(this, args)
      } catch (error) {
        inFlight.delete(id)
        throw error
      }
    } as typeof XMLHttpRequest.prototype.send
    try {
      xhrProto.open = wrappedOpen
      xhrProto.send = wrappedSend
    } catch {
      wrappedOpen = undefined
      wrappedSend = undefined
    }
  }

  const dispose = (): void => {
    if (disposed) return
    disposed = true
    observer?.disconnect()
    observer = null
    if (win && wrappedFetch && win.fetch === wrappedFetch && origFetch) {
      win.fetch = origFetch
    }
    if (xhrProto) {
      if (wrappedOpen && xhrProto.open === wrappedOpen && origOpen) xhrProto.open = origOpen
      if (wrappedSend && xhrProto.send === wrappedSend && origSend) xhrProto.send = origSend
    }
    inFlight.clear()
  }

  const settle = (): Promise<SettleResult> => {
    if (disposed) {
      return Promise.resolve({settled: true, reason: 'quiet', ignoredMutations, ignoredLongTailRequests: 0})
    }
    const startedAt = now()
    const wallStartedAt = Date.now()
    return new Promise<SettleResult>(resolve => {
      let stopTimer: Cancel | null = null
      let maxIgnoredLongTail = 0
      let wallLastMutationAt = 0

      const finish = (settled: boolean, reason: 'quiet' | 'timeout') => {
        if (stopTimer) {
          stopTimer()
          stopTimer = null
        }
        dispose()
        resolve({
          settled,
          reason,
          ignoredMutations,
          ignoredLongTailRequests: maxIgnoredLongTail,
        })
      }

      const check = () => {
        const current = now()
        const wallCurrent = Date.now()
        const elapsed = Math.max(current - startedAt, wallCurrent - wallStartedAt)
        const timestamps = [
          ...inFlight.values(),
          ...(options.activeRequestTimestamps?.() ?? []),
        ]
        const net = countRecentInFlightRequests(timestamps, current, longTailMs)
        maxIgnoredLongTail = Math.max(maxIgnoredLongTail, net.ignoredLongTail)
        const busy = Boolean(options.isBusy?.())
        if (lastMutationAt > 0 && wallLastMutationAt === 0) wallLastMutationAt = wallCurrent
        const quietElapsed = lastMutationAt === 0
          ? elapsed >= quietMs
          : Math.max(current - lastMutationAt, wallCurrent - wallLastMutationAt) >= quietMs
        if (net.recent === 0 && !busy && quietElapsed) {
          finish(true, 'quiet')
          return
        }
        if (elapsed >= maxWaitMs) {
          finish(false, 'timeout')
          return
        }
        const remaining = maxWaitMs - elapsed
        const waitForQuiet = lastMutationAt === 0
          ? Math.max(10, quietMs - elapsed)
          : Math.max(10, quietMs - Math.max(current - lastMutationAt, wallCurrent - wallLastMutationAt))
        const step = Math.min(remaining, Math.max(15, Math.min(waitForQuiet, 50)))
        stopTimer = later('page settle poll', step, check)
      }

      // Allow at least one quiet window (capped by maxWaitMs) before concluding settled.
      stopTimer = later('page settle initial', Math.min(quietMs, maxWaitMs), check)
    })
  }

  return {settle, dispose}
}

export async function waitForPageSettle(options: SettleOptions = {}): Promise<SettleResult> {
  const watch = beginPageSettleWatch(options)
  try {
    return await watch.settle()
  } finally {
    watch.dispose()
  }
}
