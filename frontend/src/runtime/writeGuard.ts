/**
 * The application's page write guard: a same-origin endpoint that makes the signed-in
 * session read-only while the Agent acts on the page.
 *
 * An application that names one gets no page action without it. `openWriteGuard` runs
 * before every action and must see the application confirm the window; a network failure,
 * a non-2xx status, or a body that does not say `active: true` refuses the action rather
 * than running it on a session that could still write. `closeWriteGuard` is best effort:
 * the window also expires on its own.
 */
export const WRITE_GUARD_TTL_S = 120

const GUARD_PATH = /^\/[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+){0,7}$/

type Fetch = typeof fetch

/** The server's rule for `page_write_guard`, repeated so a bad value refuses rather than guesses. */
export function validWriteGuardPath(value: unknown): value is string {
  return typeof value === 'string' && value.length <= 128 && GUARD_PATH.test(value)
    && value.split('/').slice(1).every(segment => segment.replace(/\./g, '') !== '')
}

/**
 * Open the window, and report when it expires so the caller can say afterwards whether it
 * still covered the action. Null means the application did not confirm one.
 */
export async function openWriteGuard(path: string, fetcher: Fetch = fetch): Promise<number | null> {
  if (!validWriteGuardPath(path)) return null
  try {
    const response = await fetcher(`${path}/begin`, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ttl_s: WRITE_GUARD_TTL_S}),
    })
    if (!response.ok) return null
    const body = await response.json() as {active?: unknown; expires_at?: unknown} | null
    if (body?.active !== true || typeof body.expires_at !== 'number'
        || !Number.isFinite(body.expires_at)) return null
    return body.expires_at
  } catch {
    return null
  }
}

export async function closeWriteGuard(path: string, fetcher: Fetch = fetch): Promise<void> {
  if (!validWriteGuardPath(path)) return
  try {
    await fetcher(`${path}/end`, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', keepalive: true,
      headers: {'Content-Type': 'application/json'}, body: '{}',
    })
  } catch {
    // The window closes itself when it expires.
  }
}
