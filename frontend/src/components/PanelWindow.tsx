import {useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {PanelRight, PictureInPicture2, Plus, X} from 'lucide-react'
import {toneOf, type HandoffRestrictedControl} from '../contract/panel'
import type {HostNotice, PanelHost} from '../bridge/host'
import {useHostSnapshot} from '../state/useHost'
import {loadOpen, rememberOpen, useWindowGeometry, type Handle} from '../state/geometry'
import {usePageNarrowing} from '../state/pagePush'
import {isPanelShortcut} from '../state/shortcut'
import {latestAnswer, loadSeen, rememberSeen, unreadAnswers} from '../state/unread'
import {guarded} from './ErrorBoundary'
import presentation from '../presentation.json'
import avatar from '../assets/assistant-avatar.webp'
import {ApprovalCard} from './ApprovalCard'
import {Composer} from './Composer'
import {ExamplePrompts, WelcomeHeading} from './EmptyState'
import {iconProps} from './icon'
import {InlineError} from './InlineError'
import {Launcher, launcherStateFor} from './Launcher'
import {Thread} from './Thread'
import {ScreenEffects, effectPhase, type EffectConfirmation} from './ScreenEffects'

const HANDLES: Handle[] = ['n', 's', 'e', 'w', 'nw', 'ne', 'sw', 'se']
const DRAWER_HANDLES: Handle[] = ['w']
// The title bar's controls are the window's own and sit on its lightest surface, so they
// draw a heavier line than the icons inside the conversation. The mode glyph is drawn a
// size down from the plus and the cross: lucide fills nearly its whole grid with the frame,
// and a frame reads heavier than a cross, so at 14 its 10.5px of ink sits level with them.
const TITLE_INK = 1.25
const CLOSED_STATES = new Set(['ended', 'replaced'])
// A pressed approval card settles its own spinner: the server answers within a tick, and
// this is how long the press stays visibly "registered" before the card comes down. It is
// a floor for the animation, not a race -- the real end is the pending approval clearing.
const DECIDE_SETTLE_MS = 400

export function PanelWindow({host, allowedOrigins}: {
  host: PanelHost
  allowedOrigins: readonly string[]
}): React.JSX.Element {
  const snapshot = useHostSnapshot(host)
  const {labels, limits} = host.configuration
  const visibility = presentation.approval_visibility
  const [open, setOpenState] = useState(loadOpen)
  const [draft, setDraft] = useState('')
  // Bumped when a new conversation's example fills the draft, so the composer takes the caret.
  const [picked, setPicked] = useState(0)
  const [seen, setSeen] = useState(loadSeen)
  const [deciding, setDeciding] = useState(false)
  const [effectConfirmation, setEffectConfirmation] = useState<EffectConfirmation | null>(null)
  const [panelFailure, setPanelFailure] = useState<HostNotice | null>(null)
  const {mode, fullScreen, geometry, docked, drawer, dragging, onPointerDown, toggleMode} =
    useWindowGeometry(open)
  // Only an open drawer on a wide enough viewport takes room from the page.
  usePageNarrowing(open && mode === 'drawer' && !fullScreen && drawer.pushes ? drawer.width : null)

  /**
   * Every call into the host goes through here, for two reasons.
   *
   * The host's methods are methods: `onClick={host.newConversation}` hands React a bare
   * function reference, and the real `PanelHost` -- unlike the object literal the tests
   * used to stand in for it -- loses its receiver that way and throws on `this`.
   *
   * And there is no iframe, so an exception the panel lets escape is an exception in the
   * host application's realm, where the host application's own error handler catches it:
   * clicking "new conversation" in Odoo raised its "Oops! Something went wrong" dialog
   * over a page nothing had gone wrong on. The panel owns its failures.
   */
  const call = useCallback((run: () => void) => {
    try {
      run()
      setPanelFailure(null)
    } catch {
      setPanelFailure({code: 'unexpected', retryable: false})
    }
  }, [])
  const clearRestoredDraft = useCallback(() => {
    const failedText = snapshot.optimistic?.failed ? snapshot.optimistic.text : null
    if (failedText) setDraft(prev => (prev === failedText ? '' : prev))
  }, [snapshot.optimistic])
  const newConversation = useCallback(() => {
    setEffectConfirmation(null)
    clearRestoredDraft()
    call(() => host.newConversation())
  }, [call, clearRestoredDraft, host])
  const loadOlder = useCallback(() => call(() => host.loadOlder()), [call, host])
  const retrySubmit = useCallback(() => {
    setEffectConfirmation(null)
    clearRestoredDraft()
    call(() => host.retrySubmit())
  }, [call, clearRestoredDraft, host])
  const cancel = useCallback(() => call(() => host.cancel()), [call, host])
  const reload = useCallback(() => call(() => host.reload()), [call, host])
  const setOpen = useCallback((value: boolean) => {
    rememberOpen(value)
    setOpenState(value)
    host.panelVisibility?.(value)
  }, [host])
  const initiallyOpen = useRef(open)
  useEffect(() => {
    // A restored window asks the host for its history just as pressing the launcher does.
    if (initiallyOpen.current) call(() => host.panelVisibility?.(true))
  }, [call, host])

  const pending = snapshot.pendingApproval
  const effectStatus = effectPhase(snapshot, effectConfirmation)
  useEffect(() => {
    if (effectStatus === 'off') setEffectConfirmation(null)
  }, [effectStatus, effectConfirmation])
  // Expired cards cannot be answered. Show the server's expiry guidance immediately while
  // the terminal timeline entry is still travelling back; that entry keeps the explanation.
  const [approvalLive, setApprovalLive] = useState(true)
  useEffect(() => {
    if (!pending) {
      setApprovalLive(true)
      return
    }
    const tick = () => setApprovalLive(pending.expires_at > Date.now())
    tick()
    const timer = window.setInterval(tick, 1000)
    return () => window.clearInterval(timer)
  }, [pending])
  const blocking = pending !== null && approvalLive
  // A draft written before the card arrived remains an intentional escape hatch. An empty
  // composer stays locked, but sending the preserved draft replaces the waiting turn: the
  // Runtime cancels that turn before it submits the new message.
  const canReplace = blocking && pending?.kind === 'operate' && draft.trim().length > 0
  // A pending confirmation outranks whatever the connection is doing: it is the only
  // thing on screen that needs an answer.
  const state = blocking ? 'awaiting_confirmation' : snapshot.connection.state
  const closed = CLOSED_STATES.has(snapshot.connection.state) || snapshot.closed
  const welcome = snapshot.entries.length === 0 && !snapshot.optimistic && !pending && !closed
    && !['connecting', 'reconnecting', 'error'].includes(snapshot.connection.state)

  // A confirmation is a question that blocks a write. Leaving it behind a collapsed
  // launcher is how an operator ends up holding a half-finished turn without knowing it.
  const wasPending = useRef(false)
  useEffect(() => {
    if (pending && !wasPending.current) setOpen(true)
    wasPending.current = pending !== null
  }, [pending, setOpen])

  // Answers that arrived while the panel was collapsed, so the launcher can say there is
  // something to come back to rather than looking idle. What is on screen is seen.
  const latest = latestAnswer(snapshot.entries)
  useEffect(() => {
    if (!open || latest <= seen) return
    rememberSeen(latest)
    setSeen(latest)
  }, [open, latest, seen])
  const unread = open ? 0 : unreadAnswers(snapshot.entries, seen)

  const decidingTimer = useRef(0)
  const decide = useCallback((decision: 'approved' | 'denied' | 'prefill') => {
    if (!pending) return
    setDeciding(true)
    setEffectConfirmation(decision === 'approved'
      ? {turnId: pending.turn_id, approvalId: pending.approval_id} : null)
    call(() => {
      try { host.decide(pending.approval_id, decision) }
      catch (error) { setEffectConfirmation(null); throw error }
    })
    window.clearTimeout(decidingTimer.current)
    decidingTimer.current = window.setTimeout(() => setDeciding(false), DECIDE_SETTLE_MS)
  }, [call, host, pending])
  useEffect(() => () => window.clearTimeout(decidingTimer.current), [])

  const submit = useCallback((text: string) => {
    setEffectConfirmation(null)
    setDraft('')
    call(() => host.submit(text))
  }, [call, host])

  // Restore the composer draft if the handshake/delivery fails so the user can edit or resend.
  const restoredOptimisticTurn = useRef<string | null>(null)
  useEffect(() => {
    const failed = snapshot.optimistic?.failed ? snapshot.optimistic : null
    if (!failed) {
      restoredOptimisticTurn.current = null
      return
    }
    if (restoredOptimisticTurn.current === failed.turnId) return
    restoredOptimisticTurn.current = failed.turnId
    setDraft(prev => (prev ? prev : failed.text))
  }, [snapshot.optimistic])

  const reconcile = useMemo(
    () => (typeof host.reconcile === 'function'
      ? (turnId: string, proposalDigest?: string) => host.reconcile!(turnId, proposalDigest)
      : undefined),
    [host],
  )
  const undoFormFill = useMemo(
    () => (typeof host.undoFormFill === 'function'
      ? (turnId: string) => host.undoFormFill!(turnId)
      : undefined),
    [host],
  )
  const focusHandoffControl = useMemo(
    () => (typeof host.focusHandoffControl === 'function'
      ? (control: HandoffRestrictedControl) => host.focusHandoffControl!(control)
      : undefined),
    [host],
  )

  // The shortcut works wherever focus is. On the host page it reaches the document; inside
  // the panel it never gets there -- the panel seals its events at its own boundary -- so
  // the window and the launcher answer it themselves.
  const openNow = useRef(open)
  openNow.current = open
  // A window put away from the keyboard hands focus to the launcher, so the person keeps
  // their place instead of landing back at the top of the page.
  const [closedByKeyboard, setClosedByKeyboard] = useState(false)
  const toggle = useCallback((byKeyboard: boolean) => {
    const next = !openNow.current
    setClosedByKeyboard(byKeyboard && !next)
    setOpen(next)
  }, [setOpen])
  useEffect(() => {
    const onKey = guarded('panel shortcut', (event: KeyboardEvent) => {
      if (!isPanelShortcut(event)) return
      event.preventDefault()
      toggle(true)
    })
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [toggle])
  const onWindowKey = (event: React.KeyboardEvent) => {
    if (isPanelShortcut(event)) {
      event.preventDefault()
      toggle(true)
    } else if (event.key === 'Escape' && !event.defaultPrevented
               && !event.nativeEvent.isComposing) {
      // A confirmation answers Escape itself (as its decline) and marks it handled, so the
      // window only closes on an Escape nothing inside it wanted; an input method choosing
      // a character uses Escape too, and that is not a request to leave.
      event.preventDefault()
      toggle(true)
    }
  }

  const askedBefore = useMemo(
    () => snapshot.entries.filter(entry => entry.kind === 'goal').map(entry => entry.text),
    [snapshot.entries])

  const placeholder = useMemo(() => {
    if (closed) return labels.composerClosed
    if (blocking) return labels.composerBlockedByApproval
    return labels.placeholder
  }, [closed, blocking, labels])

  if (!open) {
    return (
      <>
      <ScreenEffects snapshot={snapshot} confirmation={effectConfirmation} />
      <Launcher
        state={launcherStateFor(snapshot.connection.state, pending !== null)}
        unread={pending ? Math.max(1, unread) : unread}
        labels={labels}
        autoFocus={closedByKeyboard}
        onKeyDown={event => {
          if (!isPanelShortcut(event)) return
          event.preventDefault()
          toggle(true)
        }}
        onOpen={() => setOpen(true)}
      />
      </>
    )
  }

  return (
    <>
    <ScreenEffects snapshot={snapshot} confirmation={effectConfirmation} />
    <section className="woa-window" data-dragging={dragging ?? undefined}
             data-state={state} data-tone={toneOf(state)}
             data-welcome={welcome ? 'true' : undefined}
             aria-label={labels.assistant} onKeyDown={onWindowKey}
             data-mode={fullScreen ? 'full' : mode}
             data-docked={mode === 'float' && docked && !fullScreen ? 'true' : undefined}
             style={fullScreen ? undefined : mode === 'drawer'
               ? {width: `${drawer.width}px`}
               : {
                   left: `${geometry.left}px`,
                   top: `${geometry.top}px`,
                   width: `${geometry.width}px`,
                   height: `${geometry.height}px`,
                 }}>
      {(fullScreen ? [] : mode === 'drawer' ? DRAWER_HANDLES : HANDLES).map(handle => (
        <span key={handle} className="woa-handle" data-handle={handle}
              onPointerDown={onPointerDown(handle)} aria-hidden="true" />
      ))}

      <header className="woa-titlebar" data-welcome={welcome ? 'true' : undefined}
              onPointerDown={onPointerDown('move')}>
        {/* The assistant's face and name belong to a conversation. A new conversation opens
            on the face in its own greeting instead, so the bar carries only its controls. */}
        {!welcome && <span className="woa-brand">
          <img className="woa-brand__face" src={avatar} alt="" aria-hidden="true" />
          <span className="woa-brand__word">{labels.product}</span></span>}
        {host.configuration.read_only_mode && (
          <span className="woa-readonly-badge">{labels.readOnlyMode}</span>
        )}
        <button type="button" className="woa-icon-button" aria-label={labels.newChat}
                data-tip={labels.newChat} onClick={newConversation}>
          <Plus {...iconProps(16, TITLE_INK)} />
        </button>
        {/* A window that is the whole screen has no other way to be shown. */}
        {!fullScreen && <button type="button" className="woa-icon-button"
                aria-label={mode === 'float' ? labels.dockDrawer : labels.dockFloat}
                data-tip={mode === 'float' ? labels.dockDrawer : labels.dockFloat}
                onClick={toggleMode}>
          {mode === 'float'
            ? <PanelRight {...iconProps(14, TITLE_INK)} />
            : <PictureInPicture2 {...iconProps(14, TITLE_INK)} />}
        </button>}
        {/* The cross needs no words: closing is what a cross in a window's corner means. */}
        <button type="button" className="woa-icon-button" aria-label={labels.collapse}
                onClick={() => setOpen(false)}>
          <X {...iconProps(16, TITLE_INK)} />
        </button>
      </header>

      <div className={welcome ? 'woa-body woa-empty' : 'woa-body'}>
        {welcome && <WelcomeHeading labels={labels} />}
        <Thread snapshot={snapshot} labels={labels} limits={limits}
                allowedOrigins={allowedOrigins}
                localDevMode={Boolean(host.configuration.local_dev_mode)}
                onOpenTarget={target => host.openAnswerTarget?.(target) ?? false}
                onLoadOlder={loadOlder} onRetry={retrySubmit}
                onRetryTurn={submit} onReconcile={reconcile}
                onSelectOption={submit} onUndoFormFill={undoFormFill}
                onFocusHandoffControl={focusHandoffControl}
                footer={pending && approvalLive && (
                  <ApprovalCard approval={pending} labels={labels} limits={limits}
                                visibility={visibility} busy={deciding}
                                onDecide={decide} onRefused={() => undefined} />
                )} />

        {(snapshot.notice ?? panelFailure ?? (pending && !approvalLive)) && (
          <InlineError notice={snapshot.notice ?? panelFailure ?? {code: 'approval_expired', retryable: false}}
                       configuration={host.configuration}
                       labels={labels} limits={limits} onRetry={reload}
                       offline={!snapshot.connection.online} />
        )}

        <Composer labels={labels} limits={limits} welcome={welcome}
                  busy={snapshot.connection.busy} canReplace={canReplace}
                  stopping={snapshot.connection.stopping}
                  disabled={closed || (blocking && !canReplace)} ended={closed}
                  history={askedBefore}
                  placeholder={placeholder} draft={draft} focusRequest={picked} onDraft={setDraft}
                  onSubmit={submit} onCancel={cancel} />
        {welcome && <ExamplePrompts labels={labels} examples={host.examples ?? []}
                      onPick={text => { setDraft(text); setPicked(count => count + 1) }} />}
        {welcome && <p className="woa-composer__disclaimer">{labels.aiDisclaimer}</p>}
      </div>
    </section>
    </>
  )
}
