import {memo, useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {
  AlertTriangle, ArrowDown, ChevronDown, CircleAlert, CircleCheck, CircleSlash, CircleX,
} from 'lucide-react'
import type {Labels, PanelLimits, TimelineEntry} from '../contract/panel'
import type {PanelSnapshot} from '../bridge/host'
import {plainText} from '../markdown/plain'
import {formatFullTime, formatTime} from '../state/clock'
import {Answer} from './Answer'
import {CopyButton} from './CopyButton'
import {guarded} from './ErrorBoundary'
import {Skeleton} from './Skeleton'
import {iconProps} from './icon'

/** Inside this many pixels of the floor counts as "at the newest line". */
const NEAR_BOTTOM = 28
/** A modest reading scroll does not need a floating recovery control. */
const JUMP_REVEAL_DISTANCE = 160
/** How long after a gesture a scroll still counts as that person's own doing. */
const GESTURE_WINDOW_MS = 700

/** Longer than this is a clock disagreeing with itself, not something that took that long. */
const IMPLAUSIBLE_S = 24 * 60 * 60

function spell(seconds: number): string {
  return seconds < 60 ? `${seconds}s`
    : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

/** How long one node took, in the same whole seconds the turn clock uses.
 *
 * Entry times come from the server and `now` from this browser, so a machine whose clock
 * disagrees can produce a difference that is not a duration at all. Showing nothing beats
 * showing `546884m 15s`.
 */
function took(fromAt: number, toAt: number): string {
  const seconds = Math.round((toAt - fromAt) / 1000)
  if (!fromAt || !toAt || toAt < fromAt || seconds > IMPLAUSIBLE_S) return ''
  return seconds < 1 ? '<1s' : spell(seconds)
}

/**
 * How long the turn spent waiting on the person: from each confirmation it put up to
 * whatever it recorded next -- the answer, or the end of the turn when nobody answered. A
 * confirmation still waiting runs to `to`, so the turn's clock stands still while it does.
 */
function waited(turn: Turn, to: number): number {
  const after = turn.outcome ? [...turn.steps, turn.outcome] : turn.steps
  return after.reduce((total, entry, index) => entry.kind !== 'approval' ? total
    : total + Math.max(0, (after[index + 1]?.at ?? to) - entry.at), 0)
}

/** How long the turn took, from the goal the server accepted to the outcome it recorded,
 * less the time it waited for the person -- the assistant's time, not the person's. */
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
  const seconds = Math.round((to - from - waited(turn, to)) / 1000)
  return seconds < 0 || seconds > IMPLAUSIBLE_S ? '' : spell(seconds)
}

/** A clock that only ticks while something is running, and stops dead when it is not. */
function useClock(running: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 500)
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

/** What a node's marker says happened at that point, which is all it is allowed to say. */
type NodeStatus = 'running' | 'done' | 'failed' | 'waiting' | 'stopped'

const FAILED_STATES = new Set(['refused', 'error', 'indeterminate'])
/** A turn the person ended -- stopped, declined, or replaced. Nothing went wrong. */
const STOPPED_STATE = 'cancelled'

function statusOf(turn: Turn, index: number): NodeStatus {
  const step = turn.steps[index]!
  if (step.kind === 'approval') {
    // A confirmation node is only "waiting" while it is the newest thing that happened.
    return turn.running && index === turn.steps.length - 1 ? 'waiting' : 'done'
  }
  if (turn.running) return index === turn.steps.length - 1 ? 'running' : 'done'
  // A turn that ended badly ended at its last step: that is the one that gets the mark,
  // and the steps before it really did complete.
  const ended = turn.outcome?.panel_state ?? ''
  if (index !== turn.steps.length - 1) return 'done'
  return FAILED_STATES.has(ended) ? 'failed' : ended === STOPPED_STATE ? 'stopped' : 'done'
}

/**
 * A node's marker. Completed steps are quiet dots, not a column of green ticks: a tick on
 * every line stops meaning "succeeded". Colour is kept for what needs noticing -- the step
 * being worked on, a confirmation waiting on the person, the step a turn failed on.
 */
function NodeMark({status}: {status: NodeStatus}): React.JSX.Element {
  if (status === 'waiting') return <span className="woa-pulse" />
  if (status === 'failed') return <CircleX {...iconProps(14)} />
  if (status === 'stopped') return <CircleSlash {...iconProps(14)} />
  return <span className="woa-dot" />
}

/** One node of the timeline: what happened, and -- opened -- with what and for how long. */
function StepNode({step, status, duration, labels}: {
  step: TimelineEntry
  status: NodeStatus
  duration: string
  labels: Labels
}): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const facts = step.step
  const detail = facts && (facts.tool || facts.arguments || facts.result)
  // Most steps take about a second, so a column of durations says nothing; how long one
  // step took is for whoever opened it to find out what was slow.
  const line = (
    <>
      <span className="woa-step__text">{step.text}</span>
      {detail && <ChevronDown {...iconProps(14)} className="woa-step__chevron" />}
    </>
  )
  return (
    <li className="woa-step" data-status={status} data-open={open ? 'true' : undefined}>
      <span className="woa-step__mark" aria-hidden="true"><NodeMark status={status} /></span>
      {detail
        ? (
          <button type="button" className="woa-step__line" aria-expanded={open}
                  onClick={() => setOpen(!open)}>{line}</button>
        )
        : <span className="woa-step__line">{line}</span>}
      {open && detail && (
        <dl className="woa-step__detail">
          {facts?.tool && (<><dt>{labels.stepTool}</dt><dd><code>{facts.tool}</code></dd></>)}
          {facts?.arguments && (<><dt>{labels.stepArguments}</dt><dd>{facts.arguments}</dd></>)}
          {facts?.result && (<><dt>{labels.stepResult}</dt><dd>{facts.result}</dd></>)}
          {duration && (<><dt>{labels.stepDuration}</dt><dd>{duration}</dd></>)}
        </dl>
      )}
    </li>
  )
}

const WorkProcess = memo(function WorkProcess({turn, labels, stopping}: {
  turn: Turn
  labels: Labels
  /** The person pressed stop and the turn has not ended yet. */
  stopping: boolean
}): React.JSX.Element {
  const now = useClock(turn.running)
  const total = elapsed(turn, now)
  const [open, setOpen] = useState(false)
  // A turn whose only entry is the server saying it had started -- an old session replayed
  // from before steps were reported -- has nothing a list can add, so it gets no list, just
  // how long it took. Everything else keeps its steps, including after the answer lands.
  const bare = turn.steps.length <= 1 && !turn.steps.some(step => step.step)
  if (!turn.running && bare) {
    return <p className="woa-took">{labels.elapsedPrefix} {total}</p>
  }
  // Folded either way, one press from the whole record. While it runs, the line is what it
  // is doing now and how long it has been at it: enough to tell working from stuck, without
  // a growing list pushing the question off screen. Once it has landed the answer leads, and
  // the line says how much work stood behind it.
  const failed = !turn.running && FAILED_STATES.has(turn.outcome?.panel_state ?? '')
  // Ended by the person: a grey slashed circle -- cancelled, as a CI run marks it -- not the
  // red cross of a failure. The words for why are the answer's, directly below.
  const stopped = !turn.running && turn.outcome?.panel_state === STOPPED_STATE
  const current = turn.steps.at(-1)!
  // A stop takes effect at the next safe point, which can be seconds away. Until the turn
  // ends, the line says it heard the press rather than carrying on about the step.
  const waiting = turn.running && !stopping && current.kind === 'approval'
  const header = turn.running
    ? (
      <button type="button" className="woa-work__live" aria-expanded={open}
              data-waiting={waiting ? 'true' : undefined}
              data-stopping={stopping ? 'true' : undefined} onClick={() => setOpen(!open)}>
        <span className="woa-work__mark" aria-hidden="true">
          {waiting ? <span className="woa-pulse" /> : <span className="woa-spinner" />}
        </span>
        {/* Keyed by the step, so each new one arrives rather than overwriting the last. */}
        {stopping
          ? <span className="woa-work__now" key="stopping">{labels.statusStopping}</span>
          : <span className="woa-work__now" key={current.id}>{current.text}</span>}
        <ChevronDown {...iconProps(14)} className="woa-work__chevron" />
        {total && <span className="woa-work__clock">{total}</span>}
      </button>
    )
    : (
      <button type="button" className="woa-work__summary" aria-expanded={open}
              data-failed={failed ? 'true' : undefined} data-stopped={stopped ? 'true' : undefined}
              onClick={() => setOpen(!open)}>
        {failed ? <CircleX {...iconProps(14)} />
          : stopped ? <CircleSlash {...iconProps(14)} /> : <CircleCheck {...iconProps(14)} />}
        <span>{labels.workSteps.replace('{count}', String(turn.steps.length))}
          {total && ` · ${labels.elapsedPrefix} ${total}`}</span>
        <ChevronDown {...iconProps(14)} className="woa-work__chevron" />
      </button>
    )
  return (
    <div className="woa-work" data-running={turn.running ? 'true' : undefined}
         data-open={open ? 'true' : undefined}>
      {header}
      {open && <ol className="woa-work__steps">
        {turn.steps.map((step, index) => {
          const next = turn.steps[index + 1]
          const until = next ? next.at
            : (turn.outcome?.at ?? (turn.running ? now : step.at))
          return (
            <StepNode key={step.id} step={step} status={statusOf(turn, index)}
                      duration={took(step.at, until)} labels={labels} />
          )
        })}
      </ol>}
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
      <div className="woa-answer-actions">
        <CopyButton text={() => plainText(entry.text.slice(0, limits.result_max_chars))}
                    label={labels.copyAnswer} copiedLabel={labels.copied}
                    className="woa-icon-button" />
        {entry.at > 0 && (
          <time className="woa-answer-time" dateTime={new Date(entry.at).toISOString()}
                title={formatFullTime(entry.at, labels.locale)}>
            {formatTime(entry.at, Date.now(), labels.locale)}
          </time>
        )}
      </div>
      </>
    )
  }
  const Icon = kind === 'indeterminate' ? AlertTriangle : CircleAlert
  return (
    <section className="woa-outcome" data-kind={kind}>
      <h3><Icon {...iconProps(14)} />
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
  /** Rendered as the last thing in the conversation: a confirmation belongs where the
      work it is about is, and inside the one box that scrolls. */
  footer?: React.ReactNode
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
  // Whether anything sits scrolled up under the title bar, which then draws its rule.
  const [scrolled, setScrolled] = useState(false)
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
      setDetached(distance > JUMP_REVEAL_DISTANCE)
      setScrolled(root.scrollTop > 0)
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
          setDetached(distance > JUMP_REVEAL_DISTANCE)
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
  // A conversation this tab already has, being fetched: not the greeting of a new one.
  const loading = empty && snapshot.connection.state === 'connecting'

  return (
    <div className="woa-thread-shell" data-scrolled={scrolled ? 'true' : undefined}>
    <div className="woa-thread" ref={scroller} role="log" aria-live="polite"
         aria-label={labels.timeline} tabIndex={0}>
      {/* One box holding everything that can change height, so a single ResizeObserver
          sees every kind of growth instead of one effect per kind. */}
      <div className="woa-thread__content" ref={content}
           data-empty={empty && !loading ? 'true' : undefined}>
      <div ref={top} aria-hidden="true" />
      {snapshot.loadingOlder && <Skeleton />}
      {loading ? <Skeleton /> : empty ? props.children : (
        <ol className="woa-turns">
          {turns.map(turn => (
            <li className="woa-turn" key={turn.id || 'session'}
                data-settled={turn.running ? undefined : 'true'}>
              {turn.goal && <p className="woa-user">{turn.goal.text}</p>}
              <div className="woa-agent">
                {turn.steps.length > 0 && (
                  <WorkProcess turn={turn} labels={labels}
                               stopping={turn.running && snapshot.connection.stopping} />
                )}
                {/* An ending the server leaves unspoken -- a card nobody answered in time --
                    still ends the turn and marks it stopped, and adds no line of its own. */}
                {turn.outcome?.text && (
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
                <CircleAlert {...iconProps(12)} />
                {labels.undelivered} · <button type="button" onClick={onRetry}>{labels.retry}</button>
              </span>
            )
            : (
              <span className="woa-sending">
                <CircleCheck {...iconProps(12)} />
                {labels.statusQueued}
              </span>
            )}
        </div>
      )}
      {props.footer}
      <div ref={bottom} aria-hidden="true" />
      </div>
    </div>
    {detached && (
      <button type="button" className="woa-jump" onClick={pin}>
        <ArrowDown {...iconProps(12)} />
        {labels.jumpLatest}{unseen > 0 ? ` · ${unseen}` : ''}
      </button>
    )}
    </div>
  )
}
