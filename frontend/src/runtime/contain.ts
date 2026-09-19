/**
 * The only way this code hands a callback to the browser.
 *
 * The assistant runs in the host application's own JavaScript realm -- there is no iframe.
 * An exception thrown in a timer or an event handler, or a promise that rejects with no one
 * waiting, lands in the host's global error handler: Odoo answers with its "Oops!" dialog,
 * and a timer that failed every two seconds stacked them until the page was black (P7.5
 * J5.05). Every timer, listener and detached promise therefore goes through here, where a
 * failure is reported to the console and goes no further. An ESLint rule keeps the raw
 * browser calls out of every other file in this directory, so this holds by construction
 * rather than by each call site remembering it.
 */

export function report(where: string, error: unknown): void {
  try { console.warn(`[woa] ${where}:`, error) } catch { /* nothing left to do */ }
}

/** `fn`, except that nothing it throws or rejects with ever leaves it. */
export function contained<Args extends unknown[]>(
  where: string, fn: (...args: Args) => unknown,
): (...args: Args) => void {
  return (...args: Args) => {
    try {
      const result = fn(...args)
      if (result && typeof (result as PromiseLike<unknown>).then === 'function') {
        (result as PromiseLike<unknown>).then(undefined, error => report(where, error))
      }
    } catch (error) {
      report(where, error)
    }
  }
}

/** Run `promise` to completion without anyone awaiting it, and without anyone hearing it fail. */
export function detach(where: string, promise: PromiseLike<unknown>): void {
  promise.then(undefined, error => report(where, error))
}

export type Cancel = () => void

// This is the one module the containment rule (`eslint.config.js`) exempts: everywhere
// else in this directory, a raw setTimeout/setInterval/addEventListener is an ESLint error.

export function later(where: string, ms: number, fn: () => unknown): Cancel {
  const id = setTimeout(contained(where, fn), Math.min(Math.max(0, ms), 2_147_483_647))
  return () => clearTimeout(id)
}

export function every(where: string, ms: number, fn: () => unknown): Cancel {
  const id = setInterval(contained(where, fn), ms)
  return () => clearInterval(id)
}

export function listen<E extends Event>(
  where: string, target: EventTarget | null | undefined, type: string,
  fn: (event: E) => unknown, options?: AddEventListenerOptions | boolean,
): Cancel {
  if (!target?.addEventListener) return () => {}
  const handler = contained(where, fn as (event: Event) => unknown)
  target.addEventListener(type, handler, options)
  return () => target.removeEventListener(type, handler, options)
}

export function sleep(ms: number): Promise<void> {
  return new Promise(resolve => { later('sleep', ms, resolve) })
}
