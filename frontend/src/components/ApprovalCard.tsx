import {useEffect, useRef, useState} from 'react'
import {ChevronDown, Clock, Route, ShieldAlert, Sparkles, TriangleAlert} from 'lucide-react'
import {declinable, type ApprovalChoice, type ApprovalRequest, type Labels, type PanelLimits} from '../contract/panel'
import {useVisibilityGate} from '../state/visibility'
import {iconProps} from './icon'

const DIFF_ARROW = ' → '

interface ApprovalDetail {
  kind: string
  label: string
  text: string
}

function renderDetailValue(detail: ApprovalDetail): React.JSX.Element {
  if (detail.kind === 'change') {
    const arrowIndex = detail.text.indexOf(DIFF_ARROW)
    if (arrowIndex !== -1) {
      const oldValue = detail.text.slice(0, arrowIndex)
      const newValue = detail.text.slice(arrowIndex + DIFF_ARROW.length)
      return (
        <span className="woa-approval__diff">
          <span className="woa-approval__diff-old">{oldValue}</span>
          <span className="woa-approval__diff-arrow">{DIFF_ARROW}</span>
          <span className="woa-approval__diff-new">{newValue}</span>
        </span>
      )
    }
  }
  return <pre>{detail.text}</pre>
}

function renderDetailRow(detail: ApprovalDetail, index: number): React.JSX.Element {
  const multiline = detail.kind === 'body' || detail.text.includes('\n')
  return (
    <div className="woa-approval__detail"
         data-kind={detail.kind}
         data-layout={multiline ? 'block' : undefined}
         key={index}>
      <dt className="woa-approval__detail-label"><strong>{detail.label}</strong></dt>
      <dd className="woa-approval__detail-value">
        {renderDetailValue(detail)}
      </dd>
    </div>
  )
}

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
  onDecide: (decision: ApprovalChoice) => void
  onRefused: (code: string) => void
}

export function ApprovalCard(props: ApprovalCardProps): React.JSX.Element {
  const {approval, labels, visibility, busy, onDecide, onRefused} = props
  const card = useRef<HTMLElement>(null)
  const preservedToggle = useRef<HTMLButtonElement>(null)
  const cancel = useRef<HTMLButtonElement>(null)
  const prefill = useRef<HTMLButtonElement>(null)
  const confirm = useRef<HTMLButtonElement>(null)
  const [countdown, setCountdown] = useState(() => remaining(approval.expires_at))
  const [blocked, setBlocked] = useState(false)
  const [showPreserved, setShowPreserved] = useState(false)
  // Both kinds can be declined; a draft that predates a card for leave to work the page can
  // also replace the waiting turn from the composer.
  const canDecline = declinable(approval.kind)
  const operate = approval.kind === 'operate'
  const offerPrefill = !operate && approval.offer_prefill === true
  const details = approval.details ?? []
  const visibleDetails = details.filter(detail => detail.kind !== 'preserved')
  const preservedDetails = details.filter(detail => detail.kind === 'preserved')
  const gate = useVisibilityGate({
    minVisibleRatio: visibility.min_visible_ratio,
    minVisibleMs: visibility.min_visible_ms,
  })

  useEffect(() => {
    setShowPreserved(false)
    setBlocked(false)
    gate.attach(card.current)
    // The conversation follows its own tail, which can leave the top of the card above the
    // fold -- unreadable, and below the ratio the visibility gate asks for. Bring the whole
    // card into view if it fits, the least amount that does it.
    card.current?.scrollIntoView({block: 'nearest'})
    card.current?.focus({preventScroll: true})
    return () => gate.attach(null)
    // A new approval re-arms the gate and takes focus; the same one must not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [approval.approval_id])

  useEffect(() => {
    // The time left shows in the last minute, so the end does not come as a surprise; at
    // the end the window takes the card away and the turn stops, with nothing to answer.
    const tick = () => setCountdown(remaining(approval.expires_at))
    tick()
    const timer = window.setInterval(tick, 1000)
    return () => window.clearInterval(timer)
  }, [approval.expires_at])

  // The last minute: long enough to notice and answer, short enough not to hurry anyone.
  const urgent = approval.expires_at - Date.now() < 60_000

  const answer = (decision: ApprovalChoice) => (event: React.MouseEvent) => {
    if (busy) return
    // Both forms of permission need the same visibility proof; refusal does not.
    const refusal = decision !== 'denied' ? gate.refusalFor(event.nativeEvent) : ''
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
    event.preventDefault()
    const focusables = [
      preservedToggle.current,
      cancel.current,
      prefill.current,
      confirm.current,
    ].filter((button): button is HTMLButtonElement => Boolean(button && !button.disabled))
    if (focusables.length === 0) {
      card.current?.focus({preventScroll: true})
      return
    }
    const root = card.current?.getRootNode() as ShadowRoot | Document | undefined
    const active = (root && 'activeElement' in root ? root.activeElement : null) ?? document.activeElement
    const currentIndex = focusables.indexOf(active as HTMLButtonElement)
    if (currentIndex === -1) {
      const target = event.shiftKey ? focusables[focusables.length - 1] : focusables[0]
      target?.focus()
      return
    }
    const nextIndex = (currentIndex + (event.shiftKey ? -1 : 1) + focusables.length) % focusables.length
    focusables[nextIndex]?.focus()
  }

  const titleId = `woa-approval-${approval.approval_id.replace(/[^A-Za-z0-9_-]/g, '')}`
  const effectId = `${titleId}-effect`
  const targetsId = `${titleId}-targets`
  const hintId = `${titleId}-hint`
  const preservedId = `${titleId}-preserved`

  return (
    <section className="woa-approval" ref={card} role="alertdialog" aria-modal="false" tabIndex={-1}
             data-kind={approval.kind ?? 'write'}
             data-approval-id={approval.approval_id}
             aria-labelledby={titleId}
             aria-describedby={`${effectId} ${targetsId} ${hintId}`}
             onKeyDown={onKeyDown}>
      {/* Three things and two answers: what will happen, to what, and until when. The card
          keeps a frame model output cannot draw -- the header, the monospaced record, the
          buttons -- but no labels restating what the layout already says. */}
      <div className="woa-approval__inner">
        <h2 id={titleId}>
          {/* A change is guarded; working the page is a route through it. */}
          {operate ? <Route {...iconProps(14)} /> : <ShieldAlert {...iconProps(14)} />}
          {operate ? labels.operateTitle : labels.approvalTitle}
          {/* The time left shows only once it is short. The server waits a bounded time for
              an answer -- the turn is held open until then, and an answer given long after
              the page was shown is not a fresh one -- but a clock ticking from the first
              second pressed a person who had minutes. Named when it does show, because a
              bare mm:ss next to a running assistant reads as how long it has been working. */}
          {urgent && (
            <span className="woa-approval__time" data-urgent="true">
              <Clock {...iconProps(12)} />
              {labels.approvalValidFor} {countdown}
            </span>
          )}
        </h2>
        <p className="woa-approval__effect" id={effectId}>
          {approval.effect}
        </p>
        {/* Read out with the effect: an effect names what kind of change, the targets which
            record it falls on, and an answer given without hearing them is not informed. */}
        <ul className="woa-approval__targets" id={targetsId}>
          {approval.targets.map((target, index) => (
            <li key={`${index}-${target}`}>{target}</li>
          ))}
        </ul>
        {visibleDetails.length > 0 && (
          <dl className="woa-approval__details">
            {visibleDetails.map(renderDetailRow)}
          </dl>
        )}
        {preservedDetails.length > 0 && (
          <div className="woa-approval__preserved" data-open={showPreserved ? 'true' : undefined}>
            <button type="button"
                    ref={preservedToggle}
                    className="woa-approval__preserved-toggle"
                    data-testid="woa-preserved-toggle"
                    aria-expanded={showPreserved}
                    aria-controls={preservedId}
                    onClick={() => setShowPreserved(open => !open)}>
              <ChevronDown {...iconProps(14)} className="woa-approval__preserved-chevron" />
              <span>
                {showPreserved
                  ? labels.approvalHidePreserved
                  : `${labels.approvalShowPreserved}（${preservedDetails.length}）`}
              </span>
            </button>
            {showPreserved && (
              <dl className="woa-approval__details woa-approval__details--preserved" id={preservedId}>
                {preservedDetails.map(renderDetailRow)}
              </dl>
            )}
          </div>
        )}
        {blocked
          ? (
            <p className="woa-approval__blocked">
              <TriangleAlert {...iconProps(14)} />
              <span>{labels.approvalObscured}</span>
            </p>
          )
          // Leave to work the page carries its terms -- it can be stopped, a change is still
          // asked about, unsaved work may be lost -- so they show. A write's restates what the
          // buttons already mean, so it is read, not shown.
          : <p className="woa-approval__fine" id={hintId}
               data-quiet={operate ? undefined : 'true'}>
              {operate ? labels.operateHint : labels.approvalHint}
            </p>}
        <div className="woa-approval__actions">
          {canDecline && (
            <button type="button" className="woa-btn woa-btn--ghost" ref={cancel}
                    data-testid="woa-deny" disabled={busy} onClick={answer('denied')}>
              {labels.deny}
            </button>
          )}
          <span className="woa-approval__spacer" />
          {offerPrefill && (
            <button type="button" className="woa-btn woa-btn--ghost" ref={prefill}
                    data-testid="woa-prefill" disabled={busy} onClick={answer('prefill')}>
              {labels.prefillChoice}
            </button>
          )}
          {/* A refusal explains itself and stays pressable: the next press succeeds once
              the card is genuinely visible. Disabling it here would leave a person with a
              dead button and no way back. */}
          <button type="button" className="woa-btn woa-btn--primary" ref={confirm}
                  data-testid="woa-approve" disabled={busy}
                  onClick={answer('approved')}>
            {operate ? <><Sparkles {...iconProps(14)} />{labels.operateConfirm}</>
              : offerPrefill ? labels.saveChoice : labels.approve}
          </button>
        </div>
      </div>
    </section>
  )
}
