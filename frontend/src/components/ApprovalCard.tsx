import {useEffect, useRef, useState} from 'react'
import {ShieldAlert, Clock, TriangleAlert} from 'lucide-react'
import {declinable, type ApprovalRequest, type Labels, type PanelLimits} from '../contract/panel'
import {useVisibilityGate} from '../state/visibility'

function remaining(expiresAt: number): string {
  const left = Math.max(0, expiresAt - Date.now())
  const seconds = Math.floor(left / 1000)
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

export interface ApprovalCardProps {
  approval: ApprovalRequest
  labels: Labels
  limits: PanelLimits
  visibility: {min_visible_ratio: number; min_visible_ms: number}
  busy: boolean
  onDecide: (decision: 'approved' | 'denied') => void
  onRefused: (code: string) => void
}

export function ApprovalCard(props: ApprovalCardProps): React.JSX.Element {
  const {approval, labels, limits, visibility, busy, onDecide, onRefused} = props
  const card = useRef<HTMLElement>(null)
  const cancel = useRef<HTMLButtonElement>(null)
  const confirm = useRef<HTMLButtonElement>(null)
  const [countdown, setCountdown] = useState(() => remaining(approval.expires_at))
  const [blocked, setBlocked] = useState(false)
  // A navigation card asks before the assistant leaves the page and has one button: the
  // person who does not want to leave stops the task, as with anything else in flight.
  const canDecline = declinable(approval.kind)
  const gate = useVisibilityGate({
    minVisibleRatio: visibility.min_visible_ratio,
    minVisibleMs: visibility.min_visible_ms,
  })

  useEffect(() => {
    gate.attach(card.current)
    ;(canDecline ? cancel : confirm).current?.focus()
    return () => gate.attach(null)
    // A new approval re-arms the gate and takes focus; the same one must not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [approval.approval_id])

  useEffect(() => {
    // Expiry is in the protocol. It used to pass silently and the card would simply
    // vanish; showing the time left is the difference between a timeout and a mystery.
    const timer = window.setInterval(() => setCountdown(remaining(approval.expires_at)), 1000)
    return () => window.clearInterval(timer)
  }, [approval.expires_at])

  const urgent = approval.expires_at - Date.now() < 30_000

  const answer = (decision: 'approved' | 'denied') => (event: React.MouseEvent) => {
    if (busy) return
    // Refusing never needs proof that anything was seen; only approving does.
    const refusal = decision === 'approved' ? gate.refusalFor(event.nativeEvent) : ''
    if (refusal) {
      setBlocked(true)
      onRefused(refusal)
      return
    }
    setBlocked(false)
    onDecide(decision)
  }

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      if (canDecline) onDecide('denied')
      return
    }
    if (event.key !== 'Tab') return
    if (!canDecline) {
      event.preventDefault()
      confirm.current?.focus()
      return
    }
    const root = card.current?.getRootNode() as ShadowRoot | Document | undefined
    const active = (root as ShadowRoot | undefined)?.activeElement ?? document.activeElement
    if (event.shiftKey && active === cancel.current) {
      event.preventDefault()
      confirm.current?.focus()
    } else if (!event.shiftKey && active === confirm.current) {
      event.preventDefault()
      cancel.current?.focus()
    }
  }

  const titleId = `woa-approval-${approval.approval_id.replace(/[^A-Za-z0-9_-]/g, '')}`
  const effectId = `${titleId}-effect`

  return (
    <section className="woa-approval" ref={card} role="alertdialog" aria-modal="false"
             data-kind={approval.kind ?? 'write'}
             aria-labelledby={titleId} aria-describedby={effectId} onKeyDown={onKeyDown}>
      <span className="woa-approval__edge" aria-hidden="true" />
      <div className="woa-approval__inner">
        <h2 id={titleId}>
          <ShieldAlert size={15} strokeWidth={1.7} aria-hidden="true" />
          {canDecline ? labels.approvalTitle : labels.navigationTitle}
          <span className="woa-approval__time" data-urgent={urgent ? 'true' : undefined}>
            <Clock size={11} strokeWidth={1.8} aria-hidden="true" />{countdown}
          </span>
        </h2>
        <p className="woa-approval__label">{labels.approvalEffect}</p>
        <p className="woa-approval__effect" id={effectId}>
          {approval.effect.slice(0, limits.effect_max_chars)}
        </p>
        <p className="woa-approval__label">{labels.approvalTargets}</p>
        <ul className="woa-approval__targets">
          {approval.targets.slice(0, limits.max_targets).map((target, index) => (
            <li key={`${index}-${target}`}>{target.slice(0, limits.target_max_chars)}</li>
          ))}
        </ul>
        {blocked
          ? (
            <p className="woa-approval__blocked">
              <TriangleAlert size={14} strokeWidth={1.7} aria-hidden="true" />
              <span>{labels.approvalObscured}</span>
            </p>
          )
          : <p className="woa-approval__fine">
              {canDecline ? labels.approvalHint : labels.navigationHint}
            </p>}
        <div className="woa-approval__actions">
          {canDecline && (
            <button type="button" className="woa-btn woa-btn--ghost" ref={cancel}
                    data-testid="woa-deny" disabled={busy} onClick={answer('denied')}>
              {labels.deny}
            </button>
          )}
          <span className="woa-approval__spacer" />
          {/* A refusal explains itself and stays pressable: the next press succeeds once
              the card is genuinely visible. Disabling it here would leave a person with a
              dead button and no way back. */}
          <button type="button" className="woa-btn woa-btn--primary" ref={confirm}
                  data-testid="woa-approve" disabled={busy}
                  onClick={answer('approved')}>
            {canDecline ? labels.approve : labels.navigationConfirm}
          </button>
        </div>
      </div>
    </section>
  )
}
