import {useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {ChevronDown, Plus} from 'lucide-react'
import {toneOf} from '../contract/panel'
import type {PanelHost} from '../bridge/host'
import {useHostSnapshot} from '../state/useHost'
import {loadOpen, saveOpen, useWindowGeometry, type Handle} from '../state/geometry'
import {ApprovalCard} from './ApprovalCard'
import {Composer} from './Composer'
import {EmptyState} from './EmptyState'
import {InlineError} from './InlineError'
import {Launcher, launcherStateFor} from './Launcher'
import {Mark} from './Mark'
import {Thread} from './Thread'

const HANDLES: Handle[] = ['n', 's', 'e', 'w', 'nw', 'ne', 'sw', 'se']
const CLOSED_STATES = new Set(['ended', 'replaced'])

export function PanelWindow({host, allowedOrigins}: {
  host: PanelHost
  allowedOrigins: readonly string[]
}): React.JSX.Element {
  const snapshot = useHostSnapshot(host)
  const {labels, limits, approval_visibility: visibility} = host.configuration
  const [open, setOpenState] = useState(loadOpen)
  const [draft, setDraft] = useState('')
  const [unread, setUnread] = useState(0)
  const [deciding, setDeciding] = useState(false)
  const {geometry, dragging, onPointerDown} = useWindowGeometry(open)
  const setOpen = useCallback((value: boolean) => {
    setOpenState(value)
    saveOpen(value)
  }, [])

  const pending = snapshot.pendingApproval
  // A pending confirmation outranks whatever the connection is doing: it is the only
  // thing on screen that needs an answer.
  const state = pending ? 'awaiting_confirmation' : snapshot.connection.state
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

  const decide = useCallback((decision: 'approved' | 'denied') => {
    if (!pending) return
    setDeciding(true)
    host.decide(pending.approval_id, decision)
    window.setTimeout(() => setDeciding(false), 400)
  }, [host, pending])

  const submit = useCallback((text: string) => {
    setDraft('')
    host.submit(text)
  }, [host])

  const placeholder = useMemo(() => {
    if (closed) return labels.composerClosed
    if (pending) return labels.composerBlockedByApproval
    return labels.placeholder
  }, [closed, pending, labels])

  if (!open) {
    return (
      <Launcher
        state={launcherStateFor(snapshot.connection.state, pending !== null)}
        unread={pending ? Math.max(1, unread) : unread}
        labels={labels}
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
             aria-label={labels.assistant}
             style={{
               left: `${geometry.left}px`,
               top: `${geometry.top}px`,
               width: `${geometry.width}px`,
               height: `${geometry.height}px`,
             }}>
      {HANDLES.map(handle => (
        <span key={handle} className="woa-handle" data-handle={handle}
              onPointerDown={onPointerDown(handle)} aria-hidden="true" />
      ))}

      <header className="woa-titlebar" onPointerDown={onPointerDown('move')}>
        <span className="woa-brand"><Mark size={19} />
          <span className="woa-brand__word">{labels.product}</span></span>
        <span className="woa-grip" aria-hidden="true">
          {[0, 1, 2, 3, 4, 5].map(dot => <i key={dot} />)}
        </span>
        <button type="button" className="woa-icon-button" aria-label={labels.newChat}
                onClick={host.newConversation}>
          <Plus size={15} strokeWidth={1.6} aria-hidden="true" />
        </button>
        <button type="button" className="woa-icon-button" aria-label={labels.collapse}
                onClick={() => {
                  setOpen(false)
                  setUnread(0)
                }}>
          <ChevronDown size={15} strokeWidth={1.6} aria-hidden="true" />
        </button>
      </header>

      <Thread snapshot={snapshot} labels={labels} limits={limits}
              allowedOrigins={allowedOrigins}
              onLoadOlder={host.loadOlder} onRetry={host.retrySubmit}
              afterFreshBoundary={<EmptyState labels={labels} onPick={setDraft} compact />}>
        <EmptyState labels={labels} onPick={setDraft} />
      </Thread>

      {pending && (
        <ApprovalCard approval={pending} labels={labels} limits={limits}
                      visibility={visibility} busy={deciding}
                      onDecide={decide} onRefused={() => undefined} />
      )}

      {snapshot.notice && (
        <InlineError notice={snapshot.notice} configuration={host.configuration}
                     labels={labels} limits={limits} onRetry={host.reload}
                     offline={!snapshot.connection.online} />
      )}

      <Composer labels={labels} limits={limits}
                busy={snapshot.connection.busy} stopping={snapshot.connection.stopping}
                disabled={closed || pending !== null}
                placeholder={placeholder} draft={draft} onDraft={setDraft}
                onSubmit={submit} onCancel={host.cancel} />
    </section>
  )
}
