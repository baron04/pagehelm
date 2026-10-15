import {useEffect, useId, useState} from 'react'
import {MousePointer2} from 'lucide-react'
import type {PanelSnapshot} from '../bridge/host'
import {DEFAULT_CONFIGURATION} from '../contract/panel'
import {iconProps} from './icon'
import tokens from '../styles/design_tokens.json'
import {EdgeGlow} from './EdgeGlow'

export type EffectPhase = 'off' | 'active' | 'waiting'
export interface EffectConfirmation {turnId: string; approvalId: string}

// Derive terminal states from the server's contract, including a transport takeover
// whose last busy flag or approval may still be present in the browser snapshot.
const TERMINAL_STATES = new Set([
  ...Object.values(DEFAULT_CONFIGURATION.progress_states)
    .filter(state => state.terminal).map(state => state.panel_state),
  ...Object.values(DEFAULT_CONFIGURATION.terminal_panel_state_by_code),
])

/** Presentation only: none of these states authorises or delays a page action. */
export function effectPhase(snapshot: PanelSnapshot, confirmation: EffectConfirmation | null = null,
                            now = Date.now()): EffectPhase {
  // Busy means a request is running, not that the person pressed its confirmation.
  // This browser-only latch never grants execution permission and is never persisted.
  if (!confirmation) return 'off'
  if (snapshot.closed || TERMINAL_STATES.has(snapshot.connection.state)) return 'off'
  const turnId = snapshot.pendingApproval?.turn_id ?? snapshot.optimistic?.turnId
    ?? snapshot.entries.at(-1)?.turn_id
  if (turnId && turnId !== confirmation.turnId) return 'off'
  if (snapshot.pendingApproval) {
    if (snapshot.pendingApproval.expires_at <= now) return 'off'
    if (snapshot.pendingApproval.approval_id !== confirmation.approvalId) return 'off'
  }
  if (!snapshot.connection.busy) return 'off'
  return !snapshot.connection.online || snapshot.connection.state === 'reconnecting'
    ? 'waiting' : 'active'
}

const EXIT_MS = parseFloat(tokens.scale['motion-effect-exit'])

/** Fixed, inert light inside the panel's shadow root, including while it is collapsed. */
export function ScreenEffects({snapshot, confirmation}: {
  snapshot: PanelSnapshot
  confirmation: EffectConfirmation | null
}): React.JSX.Element | null {
  const phase = effectPhase(snapshot, confirmation)
  const point = snapshot.activityPoint
  const gradient = useId()
  const [visible, setVisible] = useState(phase !== 'off')
  useEffect(() => {
    if (phase !== 'off') {
      setVisible(true)
      return
    }
    const timer = window.setTimeout(() => setVisible(false), EXIT_MS)
    return () => window.clearTimeout(timer)
  }, [phase])
  if (phase === 'off' && !visible) return null
  return (
    <div className="woa-screen-effects" data-phase={phase} aria-hidden="true" inert>
      <EdgeGlow active={phase === 'active'} />
      {point && <div className="woa-agent-cursor" data-targeted="true"
           style={{left: `${point.x}px`, top: `${point.y}px`}}>
        <MousePointer2 {...iconProps(16, 1.5)} width={44} height={44}
                       strokeWidth={1.25}
                       fill="var(--woa-cursor-fill)" stroke={`url(#${gradient})`}
                       className="woa-agent-cursor__pointer">
          <defs>
            <linearGradient id={gradient} x1="0" y1="0" x2="1" y2="1">
              <stop stopColor="var(--woa-glow-blue)" />
              <stop offset="1" stopColor="var(--woa-glow-violet)" />
            </linearGradient>
          </defs>
        </MousePointer2>
      </div>}
    </div>
  )
}
