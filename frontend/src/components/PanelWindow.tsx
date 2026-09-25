import {useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {PanelRight, PictureInPicture2, Plus, X} from 'lucide-react'
import {toneOf} from '../contract/panel'
import type {HostNotice, PanelHost} from '../bridge/host'
import {useHostSnapshot} from '../state/useHost'
import {loadOpen, rememberOpen, useWindowGeometry, type Handle} from '../state/geometry'
import {usePageNarrowing} from '../state/pagePush'
import {isPanelShortcut} from '../state/shortcut'
import {guarded} from './ErrorBoundary'
import presentation from '../presentation.json'
import {ApprovalCard} from './ApprovalCard'
import {Composer} from './Composer'
import {EmptyState} from './EmptyState'
import {iconProps} from './icon'
import {InlineError} from './InlineError'
import {Launcher, launcherStateFor} from './Launcher'
import {Mark} from './Mark'
import {Thread} from './Thread'

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
  const {labels, limits, suggestions} = host.configuration
  const visibility = presentation.approval_visibility
  const [open, setOpenState] = useState(loadOpen)
  const [draft, setDraft] = useState('')
  const [unread, setUnread] = useState(0)
  const [deciding, setDeciding] = useState(false)
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
  const newConversation = useCallback(() => call(() => host.newConversation()), [call, host])
  const loadOlder = useCallback(() => call(() => host.loadOlder()), [call, host])
  const retrySubmit = useCallback(() => call(() => host.retrySubmit()), [call, host])
  const cancel = useCallback(() => call(() => host.cancel()), [call, host])
  const reload = useCallback(() => call(() => host.reload()), [call, host])
  const setOpen = useCallback((value: boolean) => {
    rememberOpen(value)
    setOpenState(value)
    host.panelVisibility?.(value)
  }, [host])

  const pending = snapshot.pendingApproval
  // A card past its deadline is gone at once, with nothing said about it: the server ends
  // the turn as a stop at the same moment, and until that lands the person can type again.
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

  // A confirmation is a question that blocks a write. Leaving it behind a collapsed
  // launcher is how an operator ends up holding a half-finished turn without knowing it.
  const wasPending = useRef(false)
  useEffect(() => {
    if (pending && !wasPending.current) setOpen(true)
    wasPending.current = pending !== null
  }, [pending, setOpen])

  // What happened while the panel was collapsed, so the launcher can say there is
  // something to come back to rather than looking idle.
  const seenEntries = useRef(snapshot.entries.length)
  useEffect(() => {
    if (open) {
      seenEntries.current = snapshot.entries.length
      setUnread(0)
      return
    }
    const added = snapshot.entries.length - seenEntries.current
    seenEntries.current = snapshot.entries.length
    if (added > 0) setUnread(count => count + added)
  }, [open, snapshot.entries.length])

  const decidingTimer = useRef(0)
  const decide = useCallback((decision: 'approved' | 'denied') => {
    if (!pending) return
    setDeciding(true)
    call(() => host.decide(pending.approval_id, decision))
    window.clearTimeout(decidingTimer.current)
    decidingTimer.current = window.setTimeout(() => setDeciding(false), DECIDE_SETTLE_MS)
  }, [call, host, pending])
  useEffect(() => () => window.clearTimeout(decidingTimer.current), [])

  const submit = useCallback((text: string) => {
    setDraft('')
    call(() => host.submit(text))
  }, [call, host])

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
    setUnread(0)
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
        onOpen={() => {
          setOpen(true)
          setUnread(0)
        }}
      />
    )
  }

  return (
    <section className="woa-window" data-dragging={dragging ?? undefined}
             data-state={state} data-tone={toneOf(state)}
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

      <header className="woa-titlebar" onPointerDown={onPointerDown('move')}>
        <span className="woa-brand"><Mark size={20} />
          <span className="woa-brand__word">{labels.product}</span></span>
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
                onClick={() => {
                  setOpen(false)
                  setUnread(0)
                }}>
          <X {...iconProps(16, TITLE_INK)} />
        </button>
      </header>

      <Thread snapshot={snapshot} labels={labels} limits={limits}
              allowedOrigins={allowedOrigins}
              onLoadOlder={loadOlder} onRetry={retrySubmit}
              footer={pending && approvalLive && (
                <ApprovalCard approval={pending} labels={labels} limits={limits}
                              visibility={visibility} busy={deciding}
                              onDecide={decide} onRefused={() => undefined} />
              )}>
        <EmptyState labels={labels} suggestions={suggestions} onPick={setDraft} />
      </Thread>

      {(snapshot.notice ?? panelFailure) && (
        <InlineError notice={(snapshot.notice ?? panelFailure)!} configuration={host.configuration}
                     labels={labels} limits={limits} onRetry={reload}
                     offline={!snapshot.connection.online} />
      )}

      <Composer labels={labels} limits={limits}
                busy={snapshot.connection.busy} canReplace={canReplace}
                stopping={snapshot.connection.stopping}
                disabled={closed || (blocking && !canReplace)} ended={closed}
                history={askedBefore}
                placeholder={placeholder} draft={draft} onDraft={setDraft}
                onSubmit={submit} onCancel={cancel} />
    </section>
  )
}
