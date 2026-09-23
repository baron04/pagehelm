import {describe, expect, it} from 'vitest'
import {renderState} from '../src/runtime/renderState'

const page = (url: string, content: string, title = 'Application') =>
  ({url, content, title})

describe('page render state across SPA routes', () => {
  it('keeps a new URL with the old view pending until a later observation replaces it', () => {
    const before = page('https://app.test/inbox', 'Inbox rows')
    const early = page('https://app.test/orders', 'Inbox rows')
    const pending = renderState(before, early)
    expect(pending.state).toBe('pending')
    expect(renderState(early, early, pending.pendingView).state).toBe('pending')
    expect(renderState(early, page(early.url, 'Sales orders'),
      pending.pendingView).state).toBe('ready')
  })

  it('accepts a route whose page projection was already replaced', () => {
    expect(renderState(page('https://app.test/inbox', 'Inbox'),
      page('https://app.test/orders', 'Orders')).state).toBe('ready')
  })

  it('does not call a fragment-only change a pending route', () => {
    expect(renderState(page('https://app.test/order#top', 'Order'),
      page('https://app.test/order#terms', 'Order')).state).toBe('ready')
  })

  it('clears pending if the user returns to the preceding route', () => {
    const before = page('https://app.test/inbox', 'Inbox')
    const early = page('https://app.test/orders', 'Inbox')
    const pending = renderState(before, early)
    expect(renderState(early, before, pending.pendingView).state).toBe('ready')
  })
})
