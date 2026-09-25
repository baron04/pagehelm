/** The parts, on their own, where a whole window would hide the detail under review. */
import type {Meta, StoryObj} from '@storybook/react'
import {ApprovalCard} from '../src/components/ApprovalCard'
import {Composer} from '../src/components/Composer'
import {EmptyState} from '../src/components/EmptyState'
import {InlineError} from '../src/components/InlineError'
import {Launcher} from '../src/components/Launcher'
import {APPROVAL, configuration, labels, limits} from './fixtures'
import presentation from '../src/presentation.json'
import {SAMPLE_EXAMPLES} from '../dev/examples'

const frame = (children: React.ReactNode, width = 376) => (
  <div style={{width, background: 'var(--woa-bg)', borderRadius: 13,
               boxShadow: '0 0 0 1px var(--woa-line-strong), 0 24px 56px rgba(20,23,28,.2)',
               overflow: 'hidden'}}>{children}</div>
)

const launcherMeta = {title: 'Panel/Launcher', component: Launcher} satisfies Meta<typeof Launcher>
const launcherKeys = {autoFocus: false, onKeyDown: () => undefined}
export default launcherMeta

export const LauncherIdle: StoryObj<typeof launcherMeta> = {
  args: {state: 'idle', unread: 0, labels, onOpen: () => undefined, ...launcherKeys},
}
export const LauncherWorking: StoryObj<typeof launcherMeta> = {
  args: {state: 'working', unread: 0, labels, onOpen: () => undefined, ...launcherKeys},
}
export const LauncherAwaiting: StoryObj<typeof launcherMeta> = {
  args: {state: 'awaiting', unread: 1, labels, onOpen: () => undefined, ...launcherKeys},
}
export const LauncherClosed: StoryObj<typeof launcherMeta> = {
  args: {state: 'closed', unread: 0, labels, onOpen: () => undefined, ...launcherKeys},
}

export const Confirmation = {
  render: () => frame(
    <ApprovalCard approval={{...APPROVAL, expires_at: Date.now() + 107_000}}
                  labels={labels} limits={limits}
                  visibility={presentation.approval_visibility} busy={false}
                  onDecide={() => undefined} onRefused={() => undefined} />),
}

export const ConfirmationNarrow = {
  render: () => frame(
    <ApprovalCard approval={{...APPROVAL,
      effect: '新建客户 Nordwind Handelsgesellschaft mbH & Co. KG',
      targets: ['res.partner · 新建一条客户记录'],
      expires_at: Date.now() + 41_000}}
      labels={labels} limits={limits}
      visibility={presentation.approval_visibility} busy={false}
      onDecide={() => undefined} onRefused={() => undefined} />, 320),
}

export const Default = {
  render: () => frame(
    <div style={{padding: '0 13px'}}>
      <EmptyState labels={labels} examples={SAMPLE_EXAMPLES} onPick={() => undefined} />
    </div>),
}

export const ComposerIdle = {
  render: () => frame(
    <Composer labels={labels} limits={limits} busy={false} canReplace={false} ended={false} history={[]}
              stopping={false} disabled={false}
              placeholder={labels.placeholder} draft="" onDraft={() => undefined}
              onSubmit={() => undefined} onCancel={() => undefined} />),
}

export const ComposerRunning = {
  render: () => frame(
    <Composer labels={labels} limits={limits} busy canReplace={false} ended={false} history={[]}
              stopping={false} disabled={false}
              placeholder={labels.placeholder} draft="" onDraft={() => undefined}
              onSubmit={() => undefined} onCancel={() => undefined} />),
}

export const FailureRetryable = {
  render: () => frame(
    <InlineError notice={{code: '', retryable: true}} configuration={configuration}
                 labels={labels} limits={limits} onRetry={() => undefined} offline={false} />),
}

export const FailureTerminal = {
  render: () => frame(
    <InlineError notice={{code: 'runtime_replaced', retryable: false}}
                 configuration={configuration} labels={labels} limits={limits}
                 onRetry={() => undefined} offline={false} />),
}
