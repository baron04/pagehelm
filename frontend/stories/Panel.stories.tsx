/**
 * Every state the panel can be in, in one place.
 *
 * The states that matter most are the ones a normal run almost never reaches -- a write
 * whose outcome is unknown, a session another page took over, a confirmation the user
 * cannot actually see. Those are exactly the ones nobody would find by clicking around.
 */
import type {Meta, StoryObj} from '@storybook/react'
import {PanelWindow} from '../src/components/PanelWindow'
import {APPROVAL, APPROVAL_WITH_DIFF, WORKED_TURN, entry, host, snapshot} from './fixtures'
import {DEFAULT_CONFIGURATION, type PanelState} from '../src/contract/panel'
import type {PanelSnapshot} from '../src/bridge/host'

const meta = {
  title: 'Panel/Window',
  component: PanelWindow,
  parameters: {layout: 'fullscreen'},
} satisfies Meta<typeof PanelWindow>
export default meta

type Story = StoryObj<typeof meta>

const show = (patch: Partial<PanelSnapshot>): Story['args'] => ({
  host: host(snapshot(patch)),
  allowedOrigins: [typeof location === 'undefined' ? '' : location.origin],
})

export const FirstOpen: Story = {args: show({})}

export const Working: Story = {
  args: show({
    connection: {state: 'acting', busy: true, stopping: false, online: true},
    entries: WORKED_TURN,
  }),
}

export const Stopping: Story = {
  args: show({
    connection: {state: 'stopping', busy: true, stopping: true, online: true},
    entries: WORKED_TURN,
  }),
}

export const AwaitingConfirmation: Story = {
  args: show({
    connection: {state: 'acting', busy: true, stopping: false, online: true},
    entries: WORKED_TURN,
    pendingApproval: {...APPROVAL, expires_at: Date.now() + 107_000},
  }),
}

export const AwaitingConfirmationDiff: Story = {
  args: show({
    connection: {state: 'acting', busy: true, stopping: false, online: true},
    entries: WORKED_TURN,
    pendingApproval: {...APPROVAL_WITH_DIFF, expires_at: Date.now() + 107_000},
  }),
}

export const ConfirmationExpiringSoon: Story = {
  args: show({
    entries: WORKED_TURN,
    pendingApproval: {...APPROVAL, expires_at: Date.now() + 21_000},
  }),
}

export const VerifiedWrite: Story = {
  args: show({
    connection: {state: 'success', busy: false, stopping: false, online: true},
    entries: [...WORKED_TURN, entry('e5', 'result',
      '报价单 S00002 已确认为销售订单。写入后以同一用户重新读取，确认状态已变更。',
      {tone: 'success', panel_state: 'success', authored: 'server'})],
  }),
}

export const IndeterminateWrite: Story = {
  args: show({
    connection: {state: 'indeterminate', busy: false, stopping: false, online: true},
    entries: [...WORKED_TURN, entry('e5', 'result',
      '这次写入已经发出，但没有拿到可信回执。不会自动重试。请到 Odoo 里核对这条记录后再决定下一步。',
      {tone: 'warning', panel_state: 'indeterminate', authored: 'server'})],
  }),
}

export const Refused: Story = {
  args: show({
    connection: {state: 'refused', busy: false, stopping: false, online: true},
    entries: [entry('e1', 'goal', '把所有客户的信用额度改成 0'),
              entry('e2', 'result', '这项操作超出本次会话获得的授权范围，没有执行。',
                    {tone: 'error', panel_state: 'refused', authored: 'server'})],
  }),
}

export const StructuredAnswer: Story = {
  args: show({
    connection: {state: 'success', busy: false, stopping: false, online: true},
    entries: [
      entry('e1', 'goal', '列出所有待开票的销售订单，带金额'),
      entry('e2', 'action', 'query_sale_orders · 扫完 12 / 12 条', {tone: 'active'}),
      entry('e3', 'result', [
        '3 张销售订单待开票：',
        '',
        '| 单号 | 客户 | 金额 |',
        '| --- | --- | ---: |',
        '| S00007 | Deco Addict | 1,231.00 |',
        '| S00012 | Gemini Furniture | 4,080.00 |',
        '| S00019 | Ready Mat | 725.50 |',
      ].join('\n'), {tone: 'success', panel_state: 'success', authored: 'server'}),
    ],
  }),
}

export const Reconnecting: Story = {
  args: show({
    connection: {state: 'reconnecting', busy: true, stopping: false, online: true},
    entries: WORKED_TURN,
    notice: {code: '', retryable: true},
  }),
}

export const Offline: Story = {
  args: show({
    connection: {state: 'reconnecting', busy: false, stopping: false, online: false},
    entries: WORKED_TURN,
    notice: {code: 'offline', retryable: false},
  }),
}

export const TakenOverByAnotherPage: Story = {
  args: show({
    connection: {state: 'replaced', busy: false, stopping: false, online: true},
    entries: WORKED_TURN,
    notice: {code: 'runtime_replaced', retryable: false},
  }),
}

export const MessageNotDelivered: Story = {
  args: show({
    entries: WORKED_TURN,
    optimistic: {turnId: 'turn-2', text: '再带上金额', failed: true},
  }),
}

export const LoadingOlderHistory: Story = {
  args: show({entries: WORKED_TURN, hasOlder: true, loadingOlder: true}),
}

/** The remaining connection states, so the gallery covers the contract and not a sample. */
export const EveryConnectionState: Story = {
  args: show({}),
  render: args => (
    <div style={{display: 'grid', gap: 24,
                 gridTemplateColumns: 'repeat(auto-fill, minmax(400px, 1fr))',
                 padding: 24, minHeight: '100vh'}}>
      {(Object.keys(DEFAULT_CONFIGURATION.panel_states) as PanelState[]).map(state => (
        <div key={state} style={{position: 'relative', height: 300}}>
          <p style={{font: '500 11px/1 ui-monospace, monospace', letterSpacing: '.08em',
                     color: '#585f6d', margin: '0 0 8px'}}>{state}</p>
          <PanelWindow
            allowedOrigins={args.allowedOrigins}
            host={host(snapshot({
              connection: {state, busy: state === 'acting', stopping: state === 'stopping',
                           online: true},
              entries: WORKED_TURN,
            }))}
          />
        </div>
      ))}
    </div>
  ),
}
