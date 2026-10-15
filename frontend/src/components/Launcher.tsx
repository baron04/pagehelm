import type {Labels, PanelState} from '../contract/panel'
import presentation from '../presentation.json'
import {DOCK_INSET} from '../state/geometry'
import {SHORTCUT_ARIA} from '../state/shortcut'
import avatar from '../assets/assistant-avatar.webp'

export type LauncherState = 'idle' | 'working' | 'awaiting' | 'closed'

export function launcherStateFor(state: PanelState, pendingApproval: boolean): LauncherState {
  if (pendingApproval) return 'awaiting'
  if (state === 'ended' || state === 'replaced') return 'closed'
  // A clarification is a question that reached the launcher. It gets the same ring a
  // confirmation does, because it asks the same thing of the person: answer it here.
  if (state === 'needs_input') return 'awaiting'
  if (['queued', 'observing', 'acting', 'stopping'].includes(state)) return 'working'
  return 'idle'
}

/**
 * The launcher is the only thing on screen while the panel is collapsed, so it carries the
 * state the header deliberately no longer shows. A ring appears while it works, turns
 * amber when it waits on the person, and is dashed once the session has ended.
 */
export function Launcher({state, unread, labels, onOpen, autoFocus, onKeyDown}: {
  state: LauncherState
  unread: number
  labels: Labels
  onOpen: () => void
  /** Set only when the keyboard put the window away: focus comes back here, not to the page. */
  autoFocus: boolean
  onKeyDown: (event: React.KeyboardEvent) => void
}): React.JSX.Element {
  return (
    <button type="button" className="woa-launcher" data-state={state}
            aria-label={labels.expand} aria-keyshortcuts={SHORTCUT_ARIA}
            autoFocus={autoFocus} onClick={onOpen} onKeyDown={onKeyDown}
            style={{right: `${DOCK_INSET}px`, bottom: `${DOCK_INSET}px`,
                    width: `${presentation.layout.launcher_size}px`,
                    height: `${presentation.layout.launcher_size}px`}}>
      <span className="woa-launcher__ring" aria-hidden="true">
        <svg viewBox="0 0 60 60">
          {state === 'working' && (
            <circle cx="30" cy="30" r="28" fill="none" stroke="var(--woa-accent)"
                    strokeWidth="2.5" strokeLinecap="round" strokeDasharray="46 130">
              <animateTransform attributeName="transform" type="rotate"
                                from="0 30 30" to="360 30 30" dur="1.6s"
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
      <span className="woa-launcher__disc"><img src={avatar} alt="" /></span>
      {unread > 0 && (
        <span className="woa-launcher__nub" data-await={state === 'awaiting' ? 'true' : undefined}>
          {unread > 9 ? '9+' : unread}
        </span>
      )}
    </button>
  )
}
