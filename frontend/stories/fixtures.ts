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
    decide(_approvalId, decision) {
      // What the server does next. A denial ends the turn, so the running step settles; an
      // approval lets the turn carry on, which a static story can only leave running.
      current = decision === 'denied'
        ? {
          ...current,
          pendingApproval: null,
          connection: {...current.connection, state: 'cancelled', busy: false},
          entries: [...current.entries,
            entry('e90', 'result', '已取消，没有执行任何写入。', {panel_state: 'cancelled'})],
        }
        : {...current, pendingApproval: null}
      emit()
    },
    newConversation() {},
    loadOlder() {},
    dismissNotice() {
      current = {...current, notice: null}
      emit()
    },
    reload() {},
    reconcile() { return Promise.resolve({outcome: 'unknown', settled: false}) },
  }
}

export const APPROVAL = {
  schema_version: 1,
  approval_id: 'approval-1',
  effect: '在系统中确认 1 张报价单',
  targets: ['报价单：S00002', '客户：Acme Corporation', '金额：1,231.00'],
  expires_at: Date.now() + 107_000,
  principal_digest: '',
  turn_id: 'turn-1',
  operation_id: 'operation-1',
}

export const APPROVAL_WITH_DIFF = {
  schema_version: 1,
  approval_id: 'approval-diff-1',
  effect: '更新 1 位客户的主要联系信息',
  targets: ['客户：Acme Corporation', '编号：C-0042'],
  details: [
    {kind: 'change', label: '联系电话', text: '010-65001122 → 010-65008899'},
    {kind: 'change', label: '结算周期', text: '月结 30 天 → 月结 45 天'},
    {kind: 'derived', label: '影响范围', text: '仅更新主档联系信息，不修改已签订单'},
    {kind: 'preserved', label: '客户等级', text: 'VIP'},
    {kind: 'preserved', label: '所属销售', text: '张敏'},
    {kind: 'preserved', label: '税号', text: '91110105MA0001234X'},
  ],
  expires_at: Date.now() + 107_000,
  principal_digest: '',
  turn_id: 'turn-1',
  operation_id: 'operation-diff-1',
}

export const WORKED_TURN: TimelineEntry[] = [
  entry('e1', 'goal', '把 Acme Corporation 的报价单 S00002 确认成销售订单'),
  entry('e2', 'progress', '正在思考', {tone: 'active', at: 1_757_000_000_000,
                                       step: {stage: 'thinking'}}),
  entry('e3', 'progress', '正在选择操作方式', {
    tone: 'active', at: 1_757_000_002_000,
    step: {stage: 'tool', tool: 'load_skill',
           arguments: 'skill_id=crm.sales.sale-orders-confirm'}}),
  entry('e4', 'action', '点击“销售”菜单项', {tone: 'active', at: 1_757_000_004_000}),
  entry('e5', 'progress', '正在读取业务数据', {
    tone: 'active', at: 1_757_000_005_000,
    step: {stage: 'tool', tool: 'query_quotations', arguments: 'limit=50'}}),
  entry('e6', 'progress', '已读取 12 条：未确认、未取消的报价单', {
    tone: 'active', at: 1_757_000_008_000,
    step: {stage: 'read_done', tool: 'query_quotations',
           result: '12 条 · 未确认、未取消的报价单'}}),
]
