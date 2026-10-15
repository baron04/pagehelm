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
import {SAMPLE_EXAMPLES} from './examples'

let sequence = 0
let turn = 1

/**
 * The scripted turn still allowed to publish. A stop, or a newer turn, retires the one in
 * flight: it gives up at its next pause, as the server's would, instead of writing its
 * steps, its confirmation and its answer into whatever came after it.
 */
let live = 0
let generation = 0

/** Start a new user turn. Deriving it from the entry count guessed wrong. */
function nextTurn(): number {
  turn += 1
  live = ++generation
  return live
}

function entry(kind: TimelineEntry['kind'], text: string,
               extra: Partial<TimelineEntry> = {}): TimelineEntry {
  sequence += 1
  return {
    id: `e${String(sequence).padStart(8, '0')}`,
    sequence,
    turn_id: `turn-${turn}`,
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

export type Scenario =
  | 'read' | 'write' | 'operate' | 'refused' | 'indeterminate'
  | 'reconnecting' | 'offline' | 'disconnect' | 'undelivered'
  | 'many' | 'older' | 'longtext' | 'clear' | 'effects'

export interface DevHost extends PanelHost {
  /** Drive a scripted turn from the dev page, the way a real session would. */
  scenario(name: Scenario): void
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

  /** Put a confirmation up, and end the turn if nobody answers it in time, as the server
      does: at `expires_at` its wait gives up, the turn ends cancelled with its own sentence
      for why, and the runtime takes the card down when that terminal state arrives. */
  function ask(request: ApprovalRequest): void {
    const mine = live
    commit({
      pendingApproval: request,
      connection: {...state.connection, state: 'awaiting_confirmation'},
    })
    window.setTimeout(() => {
      if (mine !== live || state.pendingApproval?.approval_id !== request.approval_id) return
      // Nothing is said: a timeout interrupts the message, and the server projects its
      // ending with no text.
      push(entry('result', '', {
        tone: 'neutral', panel_state: 'cancelled', authored: 'server',
      }))
      commit({
        pendingApproval: null,
        connection: {...state.connection, busy: false, stopping: false, state: 'cancelled'},
      })
    }, Math.max(0, request.expires_at - Date.now()))
  }

  /** Play steps one after another, the way the server's feed publishes them. Each line is
      the sentence the server's `describe_step` / `describe_action` writes for it. False when
      the turn was retired on the way, and nothing more of it should follow. */
  async function play(
    mine: number,
    steps: [number, TimelineEntry['kind'], string, TimelineEntry['step']?][],
  ): Promise<boolean> {
    for (const [pause, kind, text, step] of steps) {
      await wait(pause)
      if (mine !== live) return false
      push(entry(kind, text, {tone: 'active', ...(step ? {step} : {}),
                              ...(step?.stage === 'thinking' ? {transient: true} : {})}))
    }
    return true
  }

  async function runRead(goal: string): Promise<void> {
    const mine = nextTurn()
    commit({connection: {...state.connection, busy: true, state: 'observing'}})
    push(entry('goal', goal))
    commit({optimistic: null})
    if (!await play(mine, [
      [300, 'progress', '正在理解你的需求'],
      [500, 'progress', '正在思考', {stage: 'thinking'}],
      [700, 'progress', '正在选择操作方式',
       {stage: 'tool', tool: 'load_skill', arguments: 'skill_id=odoo.sales.query'}],
      [400, 'action', '重新检查当前页面'],
      [600, 'progress', '正在思考（第 2 步）', {stage: 'thinking'}],
      [800, 'progress', '正在读取业务数据',
       {stage: 'tool', tool: 'query_sale_orders',
        arguments: 'invoice_status=to invoice · limit=50'}],
      [900, 'progress', '已读取 12 条：待开票的销售订单',
       {stage: 'read_done', tool: 'query_sale_orders', result: '12 条 · 待开票的销售订单'}],
      [500, 'progress', '正在计算',
       {stage: 'tool', tool: 'compute', arguments: 'sum(amount_total) · 3 条'}],
      [600, 'action', '点击“销售”菜单项'],
      [500, 'action', '等待页面状态更新'],
    ])) return
    await wait(500)
    if (mine !== live) return
    push(entry('result', ANSWER, {
      tone: 'success', panel_state: 'success', authored: 'model',
    }))
    commit({connection: {...state.connection, busy: false, state: 'success'}})
  }

  /** The visual review follows the product's own click-to-start boundary. */
  function previewEffects(): void {
    const mine = nextTurn()
    push(entry('goal', '预览屏幕特效（合成演示）'))
    commit({connection: {...state.connection, state: 'awaiting_confirmation', busy: true},
      activityPoint: null})
    ask({schema_version: 1, approval_id: `effects-${mine}`, kind: 'operate',
      effect: '预览 15 秒屏幕辉光和助手光标', targets: ['仅演示视觉效果，不执行页面操作'],
      expires_at: Date.now() + 120_000, principal_digest: '', turn_id: `turn-${turn}`,
      operation_id: `effects-preview-${mine}`})
  }

  async function playEffects(): Promise<void> {
    const mine = live
    push(entry('progress', '正在预览屏幕特效'))
    commit({pendingApproval: null})
    commit({connection: {...state.connection, state: 'acting', busy: true}, activityPoint: null})
    for (let index = 0; index < 5; index++) {
      const target = document.querySelector(`tbody tr:nth-child(${index + 1}) td:first-child`)
      const rect = target?.getBoundingClientRect()
      if (rect) commit({activityPoint: {x: rect.x + rect.width / 2,
        y: rect.y + rect.height / 2, at: Date.now()}})
      await wait(3000)
      if (mine !== live) return
    }
    push(entry('result', '特效预览结束。', {panel_state: 'success', authored: 'server'}))
    commit({connection: {...state.connection, state: 'success', busy: false}, activityPoint: null})
  }

  async function runWrite(goal: string): Promise<void> {
    const mine = nextTurn()
    commit({connection: {...state.connection, busy: true, state: 'observing'}})
    push(entry('goal', goal))
    commit({optimistic: null})
    if (!await play(mine, [
      [300, 'progress', '正在理解你的需求'],
      [500, 'progress', '正在思考', {stage: 'thinking'}],
      [600, 'progress', '正在选择操作方式',
       {stage: 'tool', tool: 'load_skill', arguments: 'skill_id=odoo.sales.confirm'}],
      [400, 'action', '重新检查当前页面'],
      [700, 'progress', '正在读取业务数据',
       {stage: 'tool', tool: 'query_quotations', arguments: 'name=S00002 · state=draft'}],
      [700, 'progress', '已读取 1 条：报价单 S00002',
       {stage: 'read_done', tool: 'query_quotations', result: '1 条 · Acme Corporation · 草稿'}],
      [500, 'progress', '正在思考（第 3 步）', {stage: 'thinking'}],
      [600, 'progress', '正在准备这次更改',
       {stage: 'tool', tool: 'confirm_quotation', arguments: 'order=S00002'}],
    ])) return
    await wait(400)
    if (mine !== live) return
    push(entry('approval', '需要确认后才能继续', {tone: 'warning'}))
    ask({
      schema_version: 1,
      approval_id: `approval-${sequence}`,
      effect: '在 Odoo 中确认 1 张报价单',
      targets: ['报价单：S00002', '客户：Acme Corporation', '金额：1,231.00'],
      // Two minutes, like the real one, so the countdown is worth looking at.
      expires_at: Date.now() + 120_000,
      principal_digest: '',
      turn_id: state.entries.at(-1)?.turn_id ?? 'turn-1',
      operation_id: `operation-${sequence}`,
    })
  }

  async function runOperate(goal: string): Promise<void> {
    const mine = nextTurn()
    commit({connection: {...state.connection, busy: true, state: 'observing'}})
    push(entry('goal', goal))
    commit({optimistic: null})
    if (!await play(mine, [
      [300, 'progress', '正在理解你的需求'],
      [500, 'progress', '正在思考', {stage: 'thinking'}],
      [700, 'progress', '已读取 1 条：客户 Azure Interior',
       {stage: 'read_done', tool: 'query_partners', result: '1 条 · Azure Interior'}],
    ])) return
    await wait(300)
    if (mine !== live) return
    // The first page operation of the turn asks once; the rest go ahead on that answer.
    push(entry('approval', '需要确认后才能操作页面', {tone: 'warning'}))
    ask({
      schema_version: 1,
      approval_id: `approval-${sequence}`,
      effect: '在页面上帮你完成这件事',
      targets: ['会打开页面、点击、填写和选择'],
      // Five minutes, like the real one (`operation_approval_ttl_s`).
      expires_at: Date.now() + 300_000,
      principal_digest: '',
      turn_id: state.entries.at(-1)?.turn_id ?? 'turn-1',
      operation_id: `operation-${sequence}`,
      kind: 'operate',
    })
  }

  async function settleOperate(decision: 'approved' | 'denied'): Promise<void> {
    const mine = live
    if (decision === 'denied') {
      push(entry('result', '已取消，助手没有动这个页面。', {
        tone: 'neutral', panel_state: 'cancelled', authored: 'server',
      }))
      commit({pendingApproval: null,
              connection: {...state.connection, busy: false, state: 'cancelled'}})
      return
    }
    commit({pendingApproval: null, connection: {...state.connection, state: 'acting'}})
    if (!await play(mine, [
      [300, 'progress', '已收到你的确认'],
      [700, 'action', '打开“Azure Interior”'],
      [600, 'action', '点击“联系人”标签页'],
      [500, 'action', '等待页面状态更新'],
    ])) return
    await wait(400)
    if (mine !== live) return
    push(entry('result', '已打开 Azure Interior 的客户页面，并切到了“联系人”。', {
      tone: 'success', panel_state: 'success', authored: 'model',
    }))
    commit({connection: {...state.connection, busy: false, state: 'success'}})
  }

  async function settleWrite(decision: 'approved' | 'denied'): Promise<void> {
    const mine = live
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
    if (!await play(mine, [
      [300, 'progress', '已收到你的确认'],
      [900, 'progress', '更改已执行，正在核对结果',
       {stage: 'write_done', tool: 'confirm_quotation', result: '已执行，正在核对'}],
      [700, 'progress', '已读取 1 条：销售订单 S00002',
       {stage: 'read_done', tool: 'query_sale_orders', result: '1 条 · 状态：销售订单'}],
      [500, 'action', '打开“S00002”'],
    ])) return
    await wait(500)
    if (mine !== live) return
    push(entry('result',
      '报价单 S00002 已确认为销售订单。写入后以同一用户重新读取，确认状态已变更。', {
        tone: 'success', panel_state: 'success', authored: 'server',
      }))
    commit({connection: {...state.connection, busy: false, state: 'success'}})
  }

  const host: DevHost = {
    configuration: DEFAULT_CONFIGURATION,
    examples: SAMPLE_EXAMPLES,
    snapshot: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    submit(text) {
      commit({optimistic: {turnId: `turn-${turn + 1}`, text, failed: false}})
      if (state.pendingApproval) {
        commit({connection: {...state.connection, stopping: true, state: 'stopping'}})
        window.setTimeout(() => {
          push(entry('result', '已中断上一项操作，没有动这个页面。', {
            tone: 'neutral', panel_state: 'cancelled', authored: 'server',
          }))
          commit({pendingApproval: null,
                  connection: {...state.connection, busy: false, stopping: false,
                               state: 'cancelled'}})
          void runRead(text)
        }, 500)
        return
      }
      const write = /确认|开票|新建|创建|删除/.test(text)
      void (write ? runWrite(text) : runRead(text))
    },
    retrySubmit() {
      const pending = state.optimistic
      if (pending) host.submit(pending.text)
    },
    cancel() {
      // The server declines a confirmation the stop overtakes, so the card goes with the turn.
      live = 0
      commit({pendingApproval: null,
              connection: {...state.connection, stopping: true, state: 'stopping'}})
      push(entry('progress', '正在停止', {tone: 'warning'}))
      // The answer belongs to the turn that was stopped, even if another began meanwhile.
      const stopped = `turn-${turn}`
      window.setTimeout(() => {
        push(entry('result', '已按你的要求停止。', {
          tone: 'neutral', panel_state: 'cancelled', authored: 'server', turn_id: stopped,
        }))
        if (live !== 0) return
        commit({
          connection: {...state.connection, busy: false, stopping: false, state: 'cancelled'},
        })
      }, 700)
    },
    decide(_approvalId, decision) {
      void (state.pendingApproval?.operation_id.startsWith('effects-preview-')
        && decision === 'approved' ? playEffects()
        : state.pendingApproval?.kind === 'operate' ? settleOperate(decision)
        : settleWrite(decision))
    },
    newConversation() {
      // The real host clears the panel's view and tells the server; the server's own event
      // log keeps what happened. Nothing here is the record, so clearing is the whole job.
      live = 0
      sequence = 0
      turn = 1
      commit({entries: [], pendingApproval: null, notice: null, optimistic: null,
              hasOlder: false,
              connection: {state: 'ready', busy: false, stopping: false, online: true}})
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
      // The composer cannot start a turn over one in flight; a scenario button can, so it
      // ends that turn first, the way a replacing message does, rather than letting two
      // scripts write into one conversation.
      if (['read', 'write', 'operate', 'effects'].includes(name)
          && (state.connection.busy || state.pendingApproval)) {
        live = 0
        push(entry('result', '已中断上一项操作。', {
          tone: 'neutral', panel_state: 'cancelled', authored: 'server',
        }))
        commit({pendingApproval: null,
                connection: {...state.connection, busy: false, stopping: false,
                             state: 'cancelled'}})
      }
      switch (name) {
        case 'clear':
          live = 0
          sequence = 0
          turn = 1
          commit({entries: [], pendingApproval: null, notice: null, optimistic: null,
                  hasOlder: false,
                  connection: {state: 'ready', busy: false, stopping: false, online: true}})
          return
        case 'read':
          void runRead('列出所有待开票的销售订单，带金额')
          return
        case 'effects':
          void previewEffects()
          return
        case 'write':
          void runWrite('把 Acme Corporation 的报价单 S00002 确认成销售订单')
          return
        case 'operate':
          void runOperate('打开 Azure Interior 的客户页面，看看联系人')
          return
        case 'refused':
          nextTurn()
          push(entry('goal', '把所有客户的信用额度改成 0'))
          push(entry('result',
            '这个请求会一次改动全部客户记录，超出我能代你执行的范围。没有执行任何写入。',
            {tone: 'error', panel_state: 'refused', authored: 'server'}))
          commit({connection: {...state.connection, busy: false, state: 'refused'}})
          return
        case 'indeterminate':
          nextTurn()
          push(entry('goal', '给 S00007 开一张客户发票'))
          push(entry('result',
            '这次写入已经发出，但没有拿到可信回执。不会自动重试。请到 Odoo 里核对这条记录后再决定下一步。',
            {tone: 'warning', panel_state: 'indeterminate', authored: 'server'}))
          commit({connection: {...state.connection, busy: false, state: 'indeterminate'}})
          return
        case 'reconnecting':
          commit({
            notice: {code: 'reconnecting', retryable: true},
            connection: {...state.connection, state: 'reconnecting', online: true},
          })
          return
        case 'offline':
          commit({
            notice: {code: 'offline', retryable: true},
            connection: {...state.connection, state: 'reconnecting', online: false},
          })
          return
        case 'disconnect':
          commit({
            notice: {code: 'runtime_replaced', retryable: false},
            connection: {...state.connection, state: 'replaced', online: true},
          })
          return
        case 'undelivered':
          commit({optimistic: {turnId: 'turn-failed', text: '再带上金额', failed: true}})
          return
        case 'many':
          for (let index = 0; index < 6; index += 1) {
            nextTurn()
            push(entry('goal', `第 ${index + 1} 轮：列出第 ${index + 1} 页销售订单`))
            push(entry('progress', '正在思考', {tone: 'active', step: {stage: 'thinking'},
                                                 transient: true}))
            push(entry('action', '重新检查当前页面', {tone: 'active'}))
            push(entry('progress', '正在读取业务数据', {tone: 'active', step: {
              stage: 'tool', tool: 'query_sale_orders', arguments: `page=${index + 1} · limit=12`}}))
            push(entry('progress', '已读取 12 条：销售订单', {tone: 'active', step: {
              stage: 'read_done', tool: 'query_sale_orders', result: '12 条 · 其中 3 张待开票'}}))
            push(entry('action', `打开“第 ${index + 1} 页”`, {tone: 'active'}))
            push(entry('result', `第 ${index + 1} 页共 12 张，其中 3 张待开票。`, {
              tone: 'success', panel_state: 'success', authored: 'server',
            }))
          }
          return
        case 'older':
          commit({hasOlder: true})
          return
        case 'longtext':
          nextTurn()
          push(entry('goal',
            '把 Nordwind-Handelsgesellschaft-mbH-und-Co-Kommanditgesellschaft-Muenchen 的'
            + '报价单确认成销售订单并立刻开票然后把结果发给财务'))
          push(entry('result',
            '`sale.order.line.product_uom_qty` 这种没有空格的超长标识符不能把面板撑破：'
            + 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n\n'
            + '| 单号 | 客户 | 金额 |\n| --- | --- | ---: |\n'
            + '| S00007 | Nordwind Handelsgesellschaft mbH & Co. KG | 1,231.00 |',
            {tone: 'success', panel_state: 'success', authored: 'model'}))
          commit({connection: {...state.connection, busy: false, state: 'success'}})
          return
      }
    },
  }
  return host
}
