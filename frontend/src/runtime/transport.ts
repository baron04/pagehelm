/**
 * Everything this Runtime says to the gateway, and how a failure comes back.
 *
 * A failed request is a `TransportError` carrying the server's code and the HTTP status;
 * `classifyTransportError` decides once, from the server's own table, whether it ends the
 * session. The two request helpers used to build that error separately and slightly
 * differently; they share one construction now.
 */

export class TransportError extends Error {
  readonly code: string
  readonly status: number

  constructor(message: string, code: string, status: number) {
    super(message)
    this.name = 'TransportError'
    this.code = code
    this.status = status
  }
}

async function failure(response: Response, what: string): Promise<TransportError> {
  const payload = await response.json().catch(() => ({})) as {error?: unknown; code?: unknown}
  return new TransportError(
    String(payload.error || `${what} returned ${response.status}`),
    String(payload.code || ''), response.status)
}

export interface Credentials {
  endpoint: string
  sessionId: string
  token: string
  /** The Runtime lease once hello granted it; `''` before. */
  lease(): string
}

export class SessionClient {
  constructor(private readonly credentials: Credentials) {}

  private url(path: string): string {
    const {endpoint, sessionId} = this.credentials
    return `${endpoint}/v1/sessions/${encodeURIComponent(sessionId)}${path}`
  }

  headers(extra: Record<string, string> = {}, includeLease = true): Record<string, string> {
    const headers: Record<string, string> = {Authorization: `Bearer ${this.credentials.token}`, ...extra}
    const lease = this.credentials.lease()
    if (includeLease && lease) headers['X-WOA-Lease'] = lease
    return headers
  }

  async post<T = Record<string, unknown>>(path: string, body: unknown,
                                          {includeLease = true}: {includeLease?: boolean} = {}): Promise<T> {
    const response = await fetch(this.url(path), {
      method: 'POST', credentials: 'omit', cache: 'no-store',
      headers: this.headers({'content-type': 'application/json'}, includeLease),
      body: JSON.stringify(body),
    })
    if (!response.ok) throw await failure(response, path)
    return await response.json().catch(() => ({})) as T
  }

  async timeline<T>({after = '', before = ''}: {after?: string; before?: string} = {}): Promise<T> {
    const query = new URLSearchParams()
    if (after) query.set('after', after)
    if (before) query.set('before', before)
    const suffix = query.toString()
    const response = await fetch(this.url(`/timeline${suffix ? `?${suffix}` : ''}`),
      {headers: this.headers(), credentials: 'omit', cache: 'no-store'})
    if (!response.ok) throw await failure(response, 'timeline')
    return await response.json() as T
  }

  /** The server's event stream after `lastEventId`; ends when `signal` aborts. */
  async events(lastEventId: string, signal: AbortSignal): Promise<ReadableStream<Uint8Array>> {
    const response = await fetch(this.url('/events'), {
      headers: this.headers({'last-event-id': lastEventId}),
      credentials: 'omit', cache: 'no-store', signal,
    })
    if (!response.ok || !response.body) throw await failure(response, 'event stream')
    return response.body
  }
}

/** One server-sent event: its id and the JSON its data lines carry. */
export interface SseFrame {
  id: string
  data: string
}

/**
 * A server-sent event stream as the batches it arrives in: each value is the complete
 * frames one network chunk finished, possibly none. The caller handles the frames and then
 * does per-batch work once -- refreshing the panel's timeline once per delivery rather
 * than once per event. Comments and data-less frames are skipped; `stopped` ends reading
 * between chunks.
 */
export async function* sseBatches(stream: ReadableStream<Uint8Array>,
                                  stopped: () => boolean = () => false): AsyncGenerator<SseFrame[]> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  // The spec's "last event ID buffer": a frame without an `id:` line dispatches under the
  // id the last one carried, so a cursor-less event can never rewind `last-event-id` to
  // ''. The gateway labels every frame today -- this is what makes the parser agree with
  // `EventSource` about a stream that ever stops doing so.
  let lastId = ''
  while (!stopped()) {
    const {value, done} = await reader.read()
    if (done) return
    buffer += decoder.decode(value, {stream: true}).replace(/\r\n/g, '\n')
    const batch: SseFrame[] = []
    let end: number
    while ((end = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, end)
      buffer = buffer.slice(end + 2)
      if (!frame || frame.startsWith(':')) continue
      let id = lastId
      let hasId = false
      const data: string[] = []
      for (const line of frame.split('\n')) {
        if (line.startsWith('id:')) {
          id = line.slice(3).trim()
          hasId = true
        }
        if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
      }
      if (hasId) lastId = id
      if (data.length) batch.push({id, data: data.join('\n')})
    }
    yield batch
  }
}
