/**
 * One host double for every story, built from the same contract the server sends.
 *
 * Stories that invented their own labels or bounds would be the F8b defect again, in a
 * place nobody checks.
 */
import {DEFAULT_CONFIGURATION, type TimelineEntry} from '../src/contract/panel'
import type {PanelHost, PanelSnapshot} from '../src/bridge/host'

export const configuration = DEFAULT_CONFIGURATION
export const labels = configuration.labels
export const limits = configuration.limits

export function entry(id: string, kind: TimelineEntry['kind'], text: string,
                      extra: Partial<TimelineEntry> = {}): TimelineEntry {
  return {
    id, sequence: Number(id.slice(1)) || 1, turn_id: 'turn-1', at: 1_757_000_000_000,
    kind, tone: 'neutral', text, ...extra,
  }
}

export function snapshot(patch: Partial<PanelSnapshot> = {}): PanelSnapshot {
  return {
    connection: {state: 'ready', busy: false, stopping: false, online: true},
    entries: [],
    pendingApproval: null,
    notice: null,
    hasOlder: false,
    loadingOlder: false,
    closed: false,
    optimistic: null,
    ...patch,
  }
}

export function host(initial: PanelSnapshot): PanelHost {
  let current = initial
  const listeners = new Set<(value: PanelSnapshot) => void>()
  const emit = () => listeners.forEach(listener => listener(current))
  return {
    configuration,
    snapshot: () => current,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    submit(text) {
      current = {...current, optimistic: {turnId: 'turn-new', text, failed: false}}
      emit()
    },
    retrySubmit() {},
    cancel() {
      current = {...current, connection: {...current.connection, stopping: true}}
      emit()
    },
    decide() {
      current = {...current, pendingApproval: null}
      emit()
    },
    newConversation() {},
    loadOlder() {},
    dismissNotice() {
      current = {...current, notice: null}
      emit()
    },
    reload() {},
  }
}

export const APPROVAL = {
  schema_version: 1,
  approval_id: 'approval-1',
  effect: '确认报价单 S00002，使其成为销售订单',
  targets: ['sale.order · S00002 · Acme Corporation', 'state · draft → sale'],
  expires_at: Date.now() + 107_000,
  principal_digest: '',
  turn_id: 'turn-1',
  operation_id: 'operation-1',
}

export const WORKED_TURN: TimelineEntry[] = [
  entry('e1', 'goal', '把 Acme Corporation 的报价单 S00002 确认成销售订单'),
  entry('e2', 'action', '重新检查当前页面', {tone: 'active'}),
  entry('e3', 'action', '打开页面：/odoo/sales', {tone: 'active'}),
  entry('e4', 'action', 'query_quotations · 扫完 12 / 12 条', {tone: 'active'}),
]
