/**
 * A stub Runtime for working on the panel without a server.
 *
 * It answers the same `PanelHost` contract the real Runtime answers, so the panel cannot
 * tell the difference -- which is the point: if the panel needs something this file cannot
 * provide, the contract is wrong, not the harness.
 *
 * It fakes the *transport*, never the rules. Approvals still expire, a write still needs an
 * answer before the composer reopens, and nothing here invents a label or a bound: the
 * configuration is the contract's own defaults, exactly as the server would send them.
 *
 * Development only. `vite build` never sees this directory.
 */
import {DEFAULT_CONFIGURATION, type ApprovalRequest, type TimelineEntry} from '../src/contract/panel'
import type {PanelHost, PanelSnapshot} from '../src/bridge/host'

let sequence = 0

function entry(kind: TimelineEntry['kind'], text: string,
               extra: Partial<TimelineEntry> = {}): TimelineEntry {
  sequence += 1
  return {
    id: `e${String(sequence).padStart(8, '0')}`,
    sequence,
    turn_id: `turn-${Math.max(1, Math.ceil(sequence / 6))}`,
    at: Date.now(),
    kind,
    tone: 'neutral',
    text,
    ...extra,
  }
}

const ANSWER = `已扫完全部 12 条，命中 3 条。

| 单号 | 客户 | 金额 |
| --- | --- | ---: |
| S00007 | Deco Addict | 1,231.00 |
| S00012 | Gemini Furniture | 4,080.00 |
| S00019 | Ready Mat | 725.50 |

这三张都处于 \`to invoice\` 状态。`

export interface DevHost extends PanelHost {
  /** Drive a scripted turn from the dev page, the way a real session would. */
  scenario(name: 'read' | 'write' | 'indeterminate' | 'disconnect' | 'clear'): void
}

export function createDevHost(): DevHost {
  let state: PanelSnapshot = {
    connection: {state: 'ready', busy: false, stopping: false, online: true},
    entries: [],
    pendingApproval: null,
    notice: null,
    hasOlder: false,
    loadingOlder: false,
    closed: false,
    optimistic: null,
  }
  const listeners = new Set<(value: PanelSnapshot) => void>()
  const commit = (patch: Partial<PanelSnapshot>) => {
    state = {...state, ...patch}
    listeners.forEach(listener => listener(state))
  }
  const push = (...added: TimelineEntry[]) =>
    commit({entries: [...state.entries, ...added]})
  const wait = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms))

  let pendingWrite: ApprovalRequest | null = null

  async function runRead(goal: string): Promise<void> {
    commit({connection: {...state.connection, busy: true, state: 'observing'}})
    push(entry('goal', goal))
    commit({optimistic: null})
    await wait(400)
    push(entry('action', '重新检查当前页面', {tone: 'active'}))
    await wait(600)
    push(entry('action', '打开页面：/odoo/sales', {tone: 'active'}))
    await wait(700)
    push(entry('action', 'query_sale_orders · 扫完 12 / 12 条', {tone: 'active'}))
    await wait(500)
    push(entry('result', ANSWER, {
      tone: 'success', panel_state: 'success', authored: 'model',
    }))
    commit({connection: {...state.connection, busy: false, state: 'success'}})
  }

  async function runWrite(goal: string): Promise<void> {
    commit({connection: {...state.connection, busy: true, state: 'observing'}})
    push(entry('goal', goal))
    commit({optimistic: null})
    await wait(500)
    push(entry('action', '重新检查当前页面', {tone: 'active'}))
    await wait(600)
    push(entry('action', 'query_quotations · 扫完 12 / 12 条', {tone: 'active'}))
    await wait(400)
    push(entry('approval', '需要确认后才能继续', {tone: 'warning'}))
    pendingWrite = {
      schema_version: 1,
      approval_id: `approval-${sequence}`,
      effect: '确认报价单 S00002，使其成为销售订单',
      targets: ['sale.order · S00002 · Acme Corporation', 'state · draft → sale'],
      // Two minutes, like the real one, so the countdown is worth looking at.
      expires_at: Date.now() + 120_000,
      principal_digest: '',
      turn_id: state.entries.at(-1)?.turn_id ?? 'turn-1',
      operation_id: `operation-${sequence}`,
    }
    commit({
      pendingApproval: pendingWrite,
      connection: {...state.connection, state: 'awaiting_confirmation'},
    })
  }

  async function settleWrite(decision: 'approved' | 'denied'): Promise<void> {
    commit({
      pendingApproval: null,
      connection: {...state.connection, state: decision === 'approved' ? 'acting' : 'cancelled'},
    })
    if (decision === 'denied') {
      push(entry('result', '已取消，没有执行任何写入。', {
        tone: 'neutral', panel_state: 'cancelled', authored: 'server',
      }))
      commit({connection: {...state.connection, busy: false, state: 'cancelled'}})
      return
    }
    await wait(900)
    push(entry('result',
      '报价单 S00002 已确认为销售订单。写入后以同一用户重新读取，确认状态已变更。', {
        tone: 'success', panel_state: 'success', authored: 'server',
      }))
    commit({connection: {...state.connection, busy: false, state: 'success'}})
  }

  const host: DevHost = {
    configuration: DEFAULT_CONFIGURATION,
    snapshot: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    submit(text) {
      commit({optimistic: {turnId: `turn-${sequence + 1}`, text, failed: false}})
      const write = /确认|开票|新建|创建|删除/.test(text)
      void (write ? runWrite(text) : runRead(text))
    },
    retrySubmit() {
      const pending = state.optimistic
      if (pending) host.submit(pending.text)
    },
    cancel() {
      commit({connection: {...state.connection, stopping: true, state: 'stopping'}})
      window.setTimeout(() => {
        push(entry('result', '已按你的要求停止。', {
          tone: 'neutral', panel_state: 'cancelled', authored: 'server',
        }))
        commit({
          connection: {...state.connection, busy: false, stopping: false, state: 'cancelled'},
        })
      }, 700)
    },
    decide(_approvalId, decision) {
      void settleWrite(decision)
    },
    newConversation() {
      push(entry('notice', DEFAULT_CONFIGURATION.labels.newChatDivider))
    },
    loadOlder() {
      commit({loadingOlder: true})
      window.setTimeout(() => commit({loadingOlder: false, hasOlder: false}), 500)
    },
    dismissNotice() {
      commit({notice: null})
    },
    reload() {
      commit({notice: null, connection: {...state.connection, state: 'ready', online: true}})
    },
    scenario(name) {
      if (name === 'clear') {
        sequence = 0
        commit({entries: [], pendingApproval: null, notice: null, optimistic: null,
                connection: {state: 'ready', busy: false, stopping: false, online: true}})
        return
      }
      if (name === 'read') {
        void runRead('列出所有待开票的销售订单，带金额')
        return
      }
      if (name === 'write') {
        void runWrite('把 Acme Corporation 的报价单 S00002 确认成销售订单')
        return
      }
      if (name === 'indeterminate') {
        push(entry('goal', '给 S00007 开一张客户发票'))
        push(entry('result',
          '这次写入已经发出，但没有拿到可信回执。不会自动重试。请到 Odoo 里核对这条记录后再决定下一步。',
          {tone: 'warning', panel_state: 'indeterminate', authored: 'server'}))
        commit({connection: {...state.connection, busy: false, state: 'indeterminate'}})
        return
      }
      commit({
        notice: {code: 'runtime_replaced', retryable: false},
        connection: {...state.connection, state: 'replaced', online: true},
      })
    },
  }
  return host
}
