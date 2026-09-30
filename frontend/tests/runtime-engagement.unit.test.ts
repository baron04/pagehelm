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

  it('lets a new tab know there is a conversation, but not that a turn is under way', () => {
    const make = () => {
      const store = new Map<string, string>()
      return {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => { store.set(key, value) },
      } as unknown as Storage
    }
    const site = make()
    engagementRecord(make(), site).note(snapshot({}, {busy: true}), true)
    expect(engagementRecord(make(), site).read()).toBe('engaged')
    expect(engagementRecord(make(), make()).read()).toBe('none')
  })
})
