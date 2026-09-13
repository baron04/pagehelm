import {memo, useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {
  AlertTriangle, ArrowDown, ChevronDown, ChevronRight, CircleAlert, CircleCheck, CircleDot,
  ExternalLink, MousePointerClick, Search, ShieldQuestion,
} from 'lucide-react'
import type {Labels, PanelLimits, TimelineEntry} from '../contract/panel'
import type {PanelSnapshot} from '../bridge/host'
import {Answer} from './Answer'
import {guarded} from './ErrorBoundary'

/** Inside this many pixels of the floor counts as "at the newest line". */
const NEAR_BOTTOM = 28
/** A small accidental scroll does not need a floating recovery control. */
const JUMP_REVEAL_DISTANCE = 96
/** How long after a gesture a scroll still counts as that person's own doing. */
const GESTURE_WINDOW_MS = 700

const STEP_ICONS: Record<string, typeof CircleDot> = {
  goto: ExternalLink, click: MousePointerClick, query: Search, observe: CircleDot,
}

function stepIcon(text: string): typeof CircleDot {
  if (text.startsWith('打开页面')) return STEP_ICONS.goto!
  if (text.startsWith('点击') || text.startsWith('填写') || text.startsWith('提交')) {
    return STEP_ICONS.click!
  }
  if (text.includes('query_') || text.includes('扫')) return STEP_ICONS.query!
  return STEP_ICONS.observe!
}

/** How long the turn took, from the goal the server accepted to the outcome it recorded. */
function elapsed(turn: Turn, now: number): string {
  const from = turn.goal?.at ?? turn.steps[0]?.at ?? 0
  // While the turn is still running the clock is the wall clock. Reading it off the last
  // entry instead is how a turn that spent twelve seconds inside one tool call reported
  // `0.0s` for all twelve of them: nothing had happened yet, so nothing moved.
  const to = turn.running ? now : (turn.outcome?.at ?? turn.steps.at(-1)?.at ?? 0)
  if (!from || !to || to < from) return ''
  // Whole seconds. A tenth of a second is below the resolution of anything a person
  // decides from here, and a digit that flickers ten times a second reads as noise.
  // Round once, then split that same integer into minutes and seconds. Splitting the raw
  // duration first lets 119.6 seconds become the impossible-looking `1m 60s`.
  const seconds = Math.round((to - from) / 1000)
  return seconds < 60 ? `${seconds}s`
    : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

/** A clock that only ticks while something is running, and stops dead when it is not. */
function useClock(running: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 100)
    return () => window.clearInterval(timer)
  }, [running])
  return now
}

/** One user turn: the goal, the steps it took, and where it ended up. */
interface Turn {
  id: string
  goal: TimelineEntry | null
  steps: TimelineEntry[]
  outcome: TimelineEntry | null
  running: boolean
}

function group(entries: TimelineEntry[], busy: boolean): Turn[] {
  const turns: Turn[] = []
  for (const entry of entries) {
    let turn = turns.at(-1)
    if (!turn || turn.id !== entry.turn_id) {
      turn = {id: entry.turn_id, goal: null, steps: [], outcome: null, running: false}
      turns.push(turn)
    }
    if (entry.kind === 'goal') turn.goal = entry
    else if (entry.kind === 'result') turn.outcome = entry
    else turn.steps.push(entry)
  }
  // Only the newest turn can still be running, and only the Runtime knows whether it is.
  const last = turns.at(-1)
  if (last && busy && last.outcome === null) last.running = true
  return turns
}

const WorkProcess = memo(function WorkProcess({turn, labels}: {
  turn: Turn
  labels: Labels
}): React.JSX.Element {
  // Open while it runs, folded once it lands -- and whatever the reader last chose wins.
  const [override, setOverride] = useState<boolean | null>(null)
  const took = elapsed(turn, useClock(turn.running))
  const approvals = turn.steps.filter(step => step.kind === 'approval').length
  // What the agent *did*: a page action, a confirmation. A turn whose only entries are
  // the server saying it had started has nothing a list can add, so it gets no expander
  // and no card -- just how long it took. An affordance that opens onto one generic line
  // is worse than no affordance.
  const concrete = turn.steps.filter(step => step.kind !== 'progress')
  const open = override ?? (turn.running && concrete.length > 1)
  const summary = turn.running
    // Running, the summary line *is* the live status. Repeating "工作过程 · 第 1 步" above a
    // list holding that one step said the same thing twice and neither time said what it
    // was doing.
    ? (turn.steps.at(-1)?.text ?? labels.statusActing)
    : [
      labels.workProcess,
      `${turn.steps.length} ${labels.stepCount}`,
      approvals ? `${approvals} ${labels.approvalCount}` : '',
    ].filter(Boolean).join(' · ')

  if (!turn.running && concrete.length === 0) {
    return (
      <p className="woa-took">{labels.elapsedPrefix} {took}</p>
    )
  }

  return (
    <div className="woa-work" data-open={open ? 'true' : 'false'}>
      <button type="button" className="woa-work__summary" aria-expanded={open}
              onClick={() => setOverride(!open)}>
        {turn.running
          ? <span className="woa-spinner" aria-hidden="true" />
          : (open
            ? <ChevronDown className="woa-work__chevron" size={13} strokeWidth={1.7} aria-hidden="true" />
            : <ChevronRight className="woa-work__chevron" size={13} strokeWidth={1.7} aria-hidden="true" />)}
        <span className="woa-work__label">{summary}</span>
        <span className="woa-work__elapsed">{took}</span>
      </button>
      {open && (
        <ol className="woa-work__steps">
          {turn.steps.map((step, index) => {
            const Icon = step.kind === 'approval' ? ShieldQuestion : stepIcon(step.text)
            const current = turn.running && index === turn.steps.length - 1
            return (
              <li className="woa-step" key={step.id} data-now={current ? 'true' : undefined}>
                <Icon size={13} strokeWidth={1.7} aria-hidden="true" />
                <span className="woa-step__text">{step.text}</span>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
})

function Outcome({entry, labels, limits, live, allowedOrigins}: {
  entry: TimelineEntry
  labels: Labels
  limits: PanelLimits
  live: boolean
  allowedOrigins: readonly string[]
}): React.JSX.Element {
  // Only the two outcomes a person has to act on get a card of their own. Everything else
  // is prose, because giving every ending a card flattens the two that matter.
  const kind = entry.panel_state === 'indeterminate' ? 'indeterminate'
    : entry.panel_state === 'refused' ? 'refused' : 'plain'
  if (kind === 'plain') {
    return (
      <>
      <Answer text={entry.text.slice(0, limits.result_max_chars)}
              authored={entry.authored ?? 'server'} live={live}
              labels={labels} allowedOrigins={allowedOrigins} />
      {entry.source_details && (
        <details className="woa-answer-sources">
          <summary>{labels.answerSources}</summary>
          <Answer text={entry.source_details.slice(0, limits.result_max_chars)}
                  authored="server" live={false} labels={labels} allowedOrigins={allowedOrigins} />
        </details>
      )}
      </>
    )
  }
  const Icon = kind === 'indeterminate' ? AlertTriangle : CircleAlert
  return (
    <section className="woa-outcome" data-kind={kind}>
      <h3><Icon size={14} strokeWidth={1.7} aria-hidden="true" />
        {kind === 'indeterminate' ? labels.statusIndeterminate : labels.statusRefused}</h3>
      <p>{entry.text.slice(0, limits.result_max_chars)}</p>
    </section>
  )
}

export interface ThreadProps {
  snapshot: PanelSnapshot
  labels: Labels
  limits: PanelLimits
  allowedOrigins: readonly string[]
  onLoadOlder: () => void
  onRetry: () => void
  children?: React.ReactNode
  /** Shown when the newest thing in the thread is a boundary and nothing has followed it. */
}

export function Thread(props: ThreadProps): React.JSX.Element {
  const {snapshot, labels, limits, allowedOrigins, onLoadOlder, onRetry} = props
  const scroller = useRef<HTMLDivElement>(null)
  const content = useRef<HTMLDivElement>(null)
  const bottom = useRef<HTMLDivElement>(null)
  const top = useRef<HTMLDivElement>(null)
  // Following the newest line is the default and stays the default until the reader
  // scrolls away from it *on purpose*. Content arriving -- an entry, a revealed answer,
  // a card -- is not a reason to stop following, which is exactly what the old
  // `entries.length` effect got wrong: the optimistic echo grew the thread without
  // changing that count, the bottom drifted out of view, and every later arrival then
  // counted as unseen behind a button nobody asked for.
  const follow = useRef(true)
  const gesture = useRef(0)
  const [detached, setDetached] = useState(false)
  const [unseen, setUnseen] = useState(0)
  const turns = useMemo(() => group(snapshot.entries, snapshot.connection.busy),
                        [snapshot.entries, snapshot.connection.busy])
  const lastCount = useRef(snapshot.entries.length)
  // Prose already on screen when the panel opened is history, not something arriving now;
  // replaying a typewriter over it on every reconnect would be theatre.
  const settledAtMount = useRef<Set<string> | null>(null)
  if (settledAtMount.current === null) {
    settledAtMount.current = new Set(
      snapshot.entries.filter(entry => entry.kind === 'result').map(entry => entry.id))
  }

  const pin = useCallback(() => {
    const root = scroller.current
    if (!root) return
    root.scrollTo({top: root.scrollHeight, behavior: 'auto'})
    setDetached(false)
    setUnseen(0)
    follow.current = true
  }, [])

  // One scroll listener decides both things: whether to keep following, and whether the
  // reader is far enough away to be offered a way back. Only a deliberate gesture can
  // turn following off -- a programmatic scroll always lands at the bottom, so it cannot.
  useEffect(() => {
    const root = scroller.current
    if (!root) return
    const note = () => { gesture.current = Date.now() }
    const onScroll = guarded('thread scroll', () => {
      const distance = root.scrollHeight - root.scrollTop - root.clientHeight
      const near = distance <= NEAR_BOTTOM
      if (near) {
        follow.current = true
        setUnseen(0)
      } else if (Date.now() - gesture.current < GESTURE_WINDOW_MS) {
        follow.current = false
      }
      setDetached(distance >= JUMP_REVEAL_DISTANCE)
    })
    root.addEventListener('scroll', onScroll, {passive: true})
    root.addEventListener('wheel', note, {passive: true})
    root.addEventListener('touchmove', note, {passive: true})
    root.addEventListener('keydown', note)
    root.addEventListener('pointerdown', note)
    return () => {
      root.removeEventListener('scroll', onScroll)
      root.removeEventListener('wheel', note)
      root.removeEventListener('touchmove', note)
      root.removeEventListener('keydown', note)
      root.removeEventListener('pointerdown', note)
    }
  }, [])

  // Anything that changes the height of the thread -- a new entry, prose revealing one
  // character at a time, a card opening -- keeps the newest line in view while following.
  useEffect(() => {
    const node = content.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(guarded('thread resize', () => {
      if (follow.current) pin()
      else {
        const root = scroller.current
        if (root) {
          const distance = root.scrollHeight - root.scrollTop - root.clientHeight
          setDetached(distance >= JUMP_REVEAL_DISTANCE)
        }
      }
    }))
    observer.observe(node)
    return () => observer.disconnect()
  }, [pin])

  // Submitting is an explicit request to watch what happens next.
  const optimisticTurnId = snapshot.optimistic?.turnId
  useEffect(() => {
    if (optimisticTurnId) pin()
  }, [optimisticTurnId, pin])

  useEffect(() => {
    const node = top.current
    const root = scroller.current
    if (!node || !root || !snapshot.hasOlder || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(
      guarded('load older', (records: IntersectionObserverEntry[]) => {
        if (records.at(-1)?.isIntersecting) onLoadOlder()
      }), {root, rootMargin: '160px 0px 0px 0px'})
    observer.observe(node)
    return () => observer.disconnect()
  }, [snapshot.hasOlder, onLoadOlder])

  useEffect(() => {
    const added = snapshot.entries.length - lastCount.current
    lastCount.current = snapshot.entries.length
    if (added <= 0) return
    if (follow.current) pin()
    else setUnseen(count => count + added)
  }, [snapshot.entries.length, pin])

  const empty = turns.length === 0 && !snapshot.optimistic

  return (
    <div className="woa-thread-shell">
    <div className="woa-thread" ref={scroller} role="log" aria-live="polite"
         aria-label={labels.timeline} tabIndex={0}>
      {/* One box holding everything that can change height, so a single ResizeObserver
          sees every kind of growth instead of one effect per kind. */}
      <div className="woa-thread__content" ref={content}
           data-empty={empty ? 'true' : undefined}>
      <div ref={top} aria-hidden="true" />
      {snapshot.loadingOlder && (
        <div className="woa-skeleton" aria-hidden="true"><i /><i /><i /></div>
      )}
      {empty ? props.children : (
        <ol className="woa-turns">
          {turns.map(turn => (
            <li className="woa-turn" key={turn.id || 'session'}
                data-settled={turn.running ? undefined : 'true'}>
              {turn.goal && <p className="woa-user">{turn.goal.text}</p>}
              <div className="woa-agent">
                {turn.steps.length > 0 && <WorkProcess turn={turn} labels={labels} />}
                {turn.outcome && (
                  <Outcome entry={turn.outcome} labels={labels} limits={limits}
                           live={!settledAtMount.current?.has(turn.outcome.id)}
                           allowedOrigins={allowedOrigins} />
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
      {snapshot.optimistic && (
        <div className="woa-pending">
          <p className="woa-user">{snapshot.optimistic.text}</p>
          {snapshot.optimistic.failed
            ? (
              <span className="woa-undelivered">
                <CircleAlert size={12} strokeWidth={1.8} aria-hidden="true" />
                {labels.undelivered} · <button type="button" onClick={onRetry}>{labels.retry}</button>
              </span>
            )
            : (
              <span className="woa-sending">
                <CircleCheck size={12} strokeWidth={1.8} aria-hidden="true" />
                {labels.statusQueued}
              </span>
            )}
        </div>
      )}
      <div ref={bottom} aria-hidden="true" />
      </div>
    </div>
    {detached && (
      <button type="button" className="woa-jump" onClick={pin}>
        <ArrowDown size={12} strokeWidth={1.9} aria-hidden="true" />
        {labels.jumpLatest}{unseen > 0 ? ` · ${unseen}` : ''}
      </button>
    )}
    </div>
  )
}
