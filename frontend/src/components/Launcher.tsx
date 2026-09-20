import type {Labels, PanelState} from '../contract/panel'
import {Mark} from './Mark'

export type LauncherState = 'idle' | 'working' | 'awaiting' | 'closed'

export function launcherStateFor(state: PanelState, pendingApproval: boolean): LauncherState {
  if (pendingApproval) return 'awaiting'
  if (state === 'ended' || state === 'replaced') return 'closed'
  if (['queued', 'observing', 'acting', 'stopping'].includes(state)) return 'working'
  return 'idle'
}

/**
 * The launcher is the only thing on screen while the panel is collapsed, so it carries the
 * state the header deliberately no longer shows. The mark itself never changes -- a mark
 * that keeps changing stops being one -- so the state lives on the ring around it.
 */
export function Launcher({state, unread, labels, onOpen}: {
  state: LauncherState
  unread: number
  labels: Labels
  onOpen: () => void
}): React.JSX.Element {
  return (
    <button type="button" className="woa-launcher" data-state={state}
            aria-label={labels.expand} onClick={onOpen}>
      <span className="woa-launcher__ring" aria-hidden="true">
        <svg viewBox="0 0 60 60">
          {state === 'working' && (
            <circle cx="30" cy="30" r="28" fill="none" stroke="var(--woa-accent)"
                    strokeWidth="2.5" strokeLinecap="round" strokeDasharray="46 130">
              <animateTransform attributeName="transform" type="rotate"
                                from="0 30 30" to="360 30 30" dur="1.1s"
                                repeatCount="indefinite" />
            </circle>
          )}
          {state === 'awaiting' && (
            <circle cx="30" cy="30" r="28" fill="none" stroke="var(--woa-warning)"
                    strokeWidth="2.5" />
          )}
          {state === 'closed' && (
            <circle cx="30" cy="30" r="28" fill="none" stroke="var(--woa-ink-faint)"
                    strokeWidth="2" strokeDasharray="3 5" strokeLinecap="round" />
          )}
        </svg>
      </span>
      <span className="woa-launcher__disc"><Mark size={25} /></span>
      {unread > 0 && (
        <span className="woa-launcher__nub" data-await={state === 'awaiting' ? 'true' : undefined}>
          {unread > 9 ? '9+' : unread}
        </span>
      )}
    </button>
  )
}
