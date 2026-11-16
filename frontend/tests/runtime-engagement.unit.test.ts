import {describe, expect, it} from 'vitest'
import type {PanelSnapshot} from '../src/bridge/host'
import {engagementOf, engagementRecord} from '../src/runtime/engagement'

function snapshot(patch: Partial<PanelSnapshot> = {},
                  connection: Partial<PanelSnapshot['connection']> = {}): PanelSnapshot {
  return {
    connection: {state: 'ready', busy: false, stopping: false, online: true, ...connection},
    entries: [], pendingApproval: null, notice: null, hasOlder: false, loadingOlder: false,
    closed: false, optimistic: null, ...patch,
  }
}

describe('how far a tab has gone with the assistant', () => {
  it('stays at nothing until something is sent, and never goes back to it', () => {
    expect(engagementOf(snapshot(), false, 'none')).toBe('none')
    expect(engagementOf(snapshot({optimistic: {turnId: '', text: '列出订单', failed: false}}),
                        false, 'none')).toBe('engaged')
    expect(engagementOf(snapshot(), true, 'none')).toBe('engaged')
  })

  it('is active only while a connected session has a turn or a card under way', () => {
    expect(engagementOf(snapshot({}, {busy: true}), true, 'engaged')).toBe('active')
    expect(engagementOf(snapshot({}, {stopping: true}), true, 'engaged')).toBe('active')
    expect(engagementOf(snapshot(), true, 'active')).toBe('engaged')
  })

  it('keeps a turn under way across a page the agent opened, which shows nothing yet', () => {
    // The new page's panel is empty and unconnected: that says nothing about the turn.
    expect(engagementOf(snapshot(), false, 'active')).toBe('active')
    expect(engagementOf(snapshot(), false, 'engaged')).toBe('engaged')
  })

  it('is kept for the tab, and a profile that blocks storage still works', () => {
    const store = new Map<string, string>()
    const storage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => { store.set(key, value) },
    } as unknown as Storage
    const first = engagementRecord(storage, null)
    first.remember('session-1')
    first.note(snapshot({}, {busy: true}), true)
    expect(engagementRecord(storage, null).read()).toBe('active')

    const blocked = {
      getItem: () => { throw new Error('blocked') },
      setItem: () => { throw new Error('blocked') },
    } as unknown as Storage
    const record = engagementRecord(blocked, blocked)
    expect(record.read()).toBe('none')
    record.note(snapshot(), true)
    expect(record.read()).toBe('engaged')
  })

  it('keeps each tab on its own conversation and removes a legacy site session', () => {
    const make = () => {
      const store = new Map<string, string>()
      return {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => { store.set(key, value) },
        removeItem: (key: string) => { store.delete(key) },
      } as unknown as Storage
    }
    const site = make()
    const first = engagementRecord(make(), site)
    first.remember('session-1')
    first.note(snapshot({}, {busy: true}), true)
    // A new tab -- its own sessionStorage over the same site -- has no conversation of
    // its own; another tab's session is never handed to it.
    expect(engagementRecord(make(), site).read()).toBe('none')
    expect(engagementRecord(make(), site).sessionId()).toBe('')
    // A session an older version left in site storage is dropped, not restored.
    site.setItem('woa.assistant.session.v1', 'old-shared-session')
    const tab = engagementRecord(make(), site)
    expect(tab.read()).toBe('none')
    expect(tab.sessionId()).toBe('')
    expect(site.getItem('woa.assistant.session.v1')).toBeNull()
    expect(engagementRecord(make(), make()).read()).toBe('none')
  })

  it('ignores an old engagement flag when there is no saved session identifier', () => {
    const storage = {getItem: () => 'engaged'} as unknown as Storage
    // Only the engagement key exists in this pre-upgrade store.
    const legacy = {getItem: (key: string) => key.includes('engagement')
      ? storage.getItem(key) : null} as unknown as Storage
    expect(engagementRecord(legacy, legacy).read()).toBe('none')
    expect(engagementRecord(legacy, legacy).sessionId()).toBe('')
  })
})
