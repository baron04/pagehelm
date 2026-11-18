import {memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState} from 'react'
import {
  AlertTriangle, ArrowDown, ArrowUpRight, ChevronDown, CircleAlert, CircleCheck, CircleSlash, CircleX,
  FileText, PanelTop, RefreshCw, RotateCcw,
} from 'lucide-react'
import type {
  AnswerTarget, HandoffRestrictedControl, Labels, PanelLimits, ReconciliationStatus, TimelineEntry,
} from '../contract/panel'
import type {PanelSnapshot} from '../bridge/host'
import {plainText} from '../markdown/plain'
import {formatFullTime, formatTime} from '../state/clock'
import {Answer} from './Answer'
import {CopyButton} from './CopyButton'
import {DataTable, structuredTableToBlock} from './DataTable'
import {DebugTracePanel} from './DebugTracePanel'
import {guarded} from './ErrorBoundary'
import {HandoffCard} from './HandoffCard'
import {Skeleton} from './Skeleton'
import {iconProps} from './icon'
import {isAllowedOrigin} from './inline'

/** Scroll back within this many pixels of the floor re-engages auto-follow. */
const REATTACH_BOTTOM = 4
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

function isToolReadPair(prev: TimelineEntry | undefined, next: TimelineEntry): boolean {
  const p = prev?.step
  const n = next.step
  return Boolean(p && n && p.stage === 'tool' && n.stage === 'read_done' && p.tool && p.tool === n.tool)
}

function mergeReadSteps(steps: TimelineEntry[]): TimelineEntry[] {
  const merged: TimelineEntry[] = []
  for (const entry of steps) {
    const prev = merged.at(-1)
    if (prev && isToolReadPair(prev, entry)) {
      // Collapse a `tool` start line into its matching `read_done` result while keeping both
      // the call's starting timestamp and its arguments alongside the returned summary.
      merged[merged.length - 1] = {
        ...entry,
        id: prev.id,
        at: prev.at || entry.at,
        step: {...prev.step, ...entry.step},
      }
      continue
    }
    merged.push(entry)
  }
  return merged
}

const TRANSIENT_STEP_STAGES = new Set(['thinking', 'settle', 'awaiting_approval'])

function group(entries: TimelineEntry[], busy: boolean): Turn[] {
  const turns: Turn[] = []
  // A turn keeps its place even when another's lines arrive between its own -- two pages of
  // one conversation can each have asked something -- so its answer stays under its question.
  const byId = new Map<string, Turn>()
  for (const entry of entries) {
    let turn = entry.turn_id ? byId.get(entry.turn_id) : turns.at(-1)
    if (!turn || turn.id !== entry.turn_id) {
      turn = {id: entry.turn_id, goal: null, steps: [], outcome: null, running: false}
      turns.push(turn)
      if (entry.turn_id) byId.set(entry.turn_id, turn)
    }
    if (entry.kind === 'goal') turn.goal = entry
    else if (entry.kind === 'result') turn.outcome = entry
    else turn.steps.push(entry)
  }
  // Only the newest turn can still be running, and only the Runtime knows whether it is.
  const last = turns.at(-1)
  if (last && busy && last.outcome === null) last.running = true
  // A line that only says the assistant is working is true while nothing newer has happened.
  for (const turn of turns) {
    const visible = turn.steps.filter((step, index) => {
      const isTransient = step.transient ?? TRANSIENT_STEP_STAGES.has(step.step?.stage ?? '')
      return !isTransient || (turn.running && index === turn.steps.length - 1)
    })
    turn.steps = mergeReadSteps(visible)
  }
  return turns
}

/** What a node's marker says happened at that point, which is all it is allowed to say. */
type NodeStatus = 'running' | 'done' | 'failed' | 'waiting' | 'stopped'

const FAILED_STATES = new Set(['refused', 'error', 'indeterminate'])
/** A turn the person ended -- stopped, declined, or replaced. Nothing went wrong. */
const STOPPED_STATE = 'cancelled'

function statusOf(turn: Turn, index: number): NodeStatus {
  const step = turn.steps[index]!
  if (step.kind === 'approval' || step.step?.stage === 'awaiting_approval') {
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
    <li className="woa-step" data-entry-id={step.id} data-status={status} data-open={open ? 'true' : undefined}>
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
  const failed = !turn.running && FAILED_STATES.has(turn.outcome?.panel_state ?? '')
  // Ended by the person: a grey slashed circle -- cancelled, as a CI run marks it -- not the
  // red cross of a failure. The words for why are the answer's, directly below.
  const stopped = !turn.running && turn.outcome?.panel_state === STOPPED_STATE
  const mark = failed ? <CircleX {...iconProps(14)} />
    : stopped ? <CircleSlash {...iconProps(14)} /> : <CircleCheck {...iconProps(14)} />
  // A turn whose only entry is the server saying it had started -- a goal turn, which
  // reports no steps, or an old session replayed from before steps were reported -- has
  // nothing a list can add, so it gets no list: how it ended and how long it took, on the
  // same line a turn with steps has. Everything else keeps its steps after the answer lands.
  const bare = turn.steps.length <= 1 && !turn.steps.some(step => step.step)
  if (!turn.running && bare) {
    return (
      <p className="woa-took" data-failed={failed ? 'true' : undefined}
         data-stopped={stopped ? 'true' : undefined}>
        {mark}<span>{labels.elapsedPrefix} {total}</span>
      </p>
    )
  }
  // Folded either way, one press from the whole record. While it runs, the line is what it
  // is doing now and how long it has been at it: enough to tell working from stuck, without
  // a growing list pushing the question off screen. Once it has landed the answer leads, and
  // the line says how much work stood behind it.
  const current = turn.steps.at(-1)!
  // A stop takes effect at the next safe point, which can be seconds away. Until the turn
  // ends, the line says it heard the press rather than carrying on about the step.
  const waiting = turn.running && !stopping
    && (current.kind === 'approval' || current.step?.stage === 'awaiting_approval')
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
          : <span className="woa-work__now" key={`${current.id}:${current.text}`}>{current.text}</span>}
        <ChevronDown {...iconProps(14)} className="woa-work__chevron" />
        {total && <span className="woa-work__clock">{total}</span>}
      </button>
    )
    : (
      <button type="button" className="woa-work__summary" aria-expanded={open}
              data-failed={failed ? 'true' : undefined} data-stopped={stopped ? 'true' : undefined}
              onClick={() => setOpen(!open)}>
        {mark}
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

/**
 * What a turn shows between being sent and the server's first step: the same live line a
 * working turn has, at once, so sending never looks like nothing happening.
 */
function Starting({labels}: {labels: Labels}): React.JSX.Element {
  return (
    <div className="woa-work" data-running="true">
      <span className="woa-work__live">
        <span className="woa-work__mark" aria-hidden="true"><span className="woa-spinner" /></span>
        <span className="woa-work__now">{labels.turnStarting}</span>
      </span>
    </div>
  )
}

function Outcome({
  entry, labels, limits, live, allowedOrigins, optionsDisabled,
  onOpenTarget, onReconcile, onSelectOption, onUndoFormFill, onFocusHandoffControl,
}: {
  entry: TimelineEntry
  labels: Labels
  limits: PanelLimits
  live: boolean
  allowedOrigins: readonly string[]
  optionsDisabled: boolean
  onOpenTarget?: ((target: AnswerTarget) => boolean) | undefined
  onReconcile?: ((turnId: string, proposalDigest?: string) => Promise<ReconciliationStatus> | void) | undefined
  onSelectOption?: ((text: string) => void) | undefined
  onUndoFormFill?: ((turnId: string) => Promise<boolean> | boolean) | undefined
  onFocusHandoffControl?: ((control: HandoffRestrictedControl) => Promise<boolean> | boolean) | undefined
}): React.JSX.Element {
  const [reconciling, setReconciling] = useState(false)
  const [localReconciliation, setLocalReconciliation] = useState<ReconciliationStatus | null>(null)
  const allow = useMemo(() => isAllowedOrigin(allowedOrigins), [allowedOrigins])
  const tableBlock = useMemo(
    () => (entry.table && entry.table.columns.length > 0 ? structuredTableToBlock(entry.table) : null),
    [entry.table],
  )
  useEffect(() => {
    if (entry.reconciliation) setLocalReconciliation(null)
  }, [entry.reconciliation])

  // Only the two outcomes a person has to act on get a card of their own. Everything else
  // is prose, because giving every ending a card flattens the two that matter.
  const kind = entry.panel_state === 'indeterminate' ? 'indeterminate'
    : entry.panel_state === 'refused' ? 'refused' : 'plain'
  if (kind === 'plain') {
    const hasText = Boolean(entry.text)
    return (
      <div className="woa-result" data-entry-id={entry.id}>
      {hasText && (
        <Answer text={entry.text.slice(0, limits.result_max_chars)}
                authored={entry.authored ?? 'server'} live={live}
                labels={labels} allowedOrigins={allowedOrigins} />
      )}
      {tableBlock && (
        <div className="woa-result__table">
          {entry.table?.title && <h4 className="woa-result__table-title">{entry.table.title}</h4>}
          <DataTable block={tableBlock} labels={labels} allow={allow} />
        </div>
      )}
      {entry.panel_state === 'needs_input' && entry.input_options?.length ? (
        <div className="woa-input-options" role="group" aria-label={labels.statusNeedsInput}>
          {entry.input_options.map(option => (
            <button
              key={option.id}
              type="button"
              className="woa-input-option"
              disabled={optionsDisabled}
              onClick={() => onSelectOption?.(option.prompt ?? option.label)}
            >
              {option.label}
            </button>
          ))}
        </div>
      ) : null}
      {entry.handoff && (
        <HandoffCard
          handoff={entry.handoff}
          turnId={entry.turn_id}
          labels={labels}
          onUndoFormFill={onUndoFormFill}
          onFocusHandoffControl={onFocusHandoffControl}
        />
      )}
      {entry.answer_targets?.length ? (
        <nav className="woa-answer-targets" aria-label={labels.relatedLinks}>
          <span>{labels.relatedLinks}</span>
          <ul>{entry.answer_targets.map(target => (
            <li key={`${target.read_ref}:${target.identity}`}><a href={target.address}
               target="_blank" rel="noreferrer noopener"
               onClick={event => {
                 if (event.button === 0 && !event.metaKey && !event.ctrlKey
                     && !event.shiftKey && !event.altKey && onOpenTarget?.(target)) {
                   event.preventDefault()
                 }
               }}>
              {target.kind === 'record' ? <FileText {...iconProps(14)} /> : <PanelTop {...iconProps(14)} />}
              <span>{target.label}</span>
              <ArrowUpRight {...iconProps(12)} />
            </a></li>
          ))}</ul>
        </nav>
      ) : null}
      {(hasText || entry.at > 0) && (
        <div className="woa-answer-actions">
          {hasText && (
            <CopyButton text={() => plainText(entry.text.slice(0, limits.result_max_chars))}
                        label={labels.copyAnswer} copiedLabel={labels.copied}
                        className="woa-icon-button" />
          )}
          {entry.at > 0 && (
            <time className="woa-answer-time" dateTime={new Date(entry.at).toISOString()}
                  title={formatFullTime(entry.at, labels.locale)}>
              {formatTime(entry.at, Date.now(), labels.locale)}
            </time>
          )}
        </div>
      )}
      </div>
    )
  }
  const Icon = kind === 'indeterminate' ? AlertTriangle : CircleAlert
  const reconciliation = kind === 'indeterminate' ? (localReconciliation ?? entry.reconciliation ?? null) : null
  const statusLabel = reconciliation
    ? reconciliation.outcome === 'completed'
      ? labels.reconcileCompleted
      : reconciliation.outcome === 'failed'
        ? labels.reconcileNotApplied
        : labels.reconcileUnknown
    : null
  const handleReconcile = async () => {
    if (!onReconcile || reconciling) return
    setReconciling(true)
    try {
      const result = await onReconcile(entry.turn_id, entry.proposal_digest)
      if (result && typeof result === 'object' && 'outcome' in result) {
        setLocalReconciliation(result)
      }
    } catch {
      setLocalReconciliation({outcome: 'unknown', settled: false})
    } finally {
      setReconciling(false)
    }
  }
  return (
    <section className="woa-outcome" data-entry-id={entry.id} data-kind={kind}>
      <h3><Icon {...iconProps(14)} />
        {kind === 'indeterminate' ? labels.statusIndeterminate : labels.statusRefused}</h3>
      <Answer text={entry.text.slice(0, limits.result_max_chars)}
              authored={entry.authored ?? 'server'} live={false}
              labels={labels} allowedOrigins={allowedOrigins} />
      {kind === 'indeterminate' && reconciliation && statusLabel && (
        <p className="woa-outcome__reconcile" data-outcome={reconciliation.outcome} role="status">
          {reconciliation.outcome === 'completed'
            ? <CircleCheck {...iconProps(14)} />
            : reconciliation.outcome === 'failed'
              ? <CircleSlash {...iconProps(14)} />
              : <AlertTriangle {...iconProps(14)} />}
          <span>{statusLabel}</span>
          {reconciliation.message && (
            <span className="woa-outcome__reconcile-msg">{reconciliation.message}</span>
          )}
        </p>
      )}
      {kind === 'indeterminate' && onReconcile && !reconciliation?.settled && (
        <div className="woa-outcome__actions">
          <button type="button" className="woa-btn woa-btn--ghost woa-outcome__reconcile-btn"
                  disabled={reconciling} onClick={() => { void handleReconcile() }}>
            <RefreshCw {...iconProps(14)} />
            <span>{reconciling ? labels.reconciling : labels.reconcileStatus}</span>
          </button>
        </div>
      )}
    </section>
  )
}

export interface ThreadProps {
  snapshot: PanelSnapshot
  labels: Labels
  limits: PanelLimits
  allowedOrigins: readonly string[]
  localDevMode?: boolean
  onOpenTarget?: (target: AnswerTarget) => boolean
  onLoadOlder: () => void
  onRetry: () => void
  onRetryTurn?: ((text: string) => void) | undefined
  onReconcile?: ((turnId: string, proposalDigest?: string) => Promise<ReconciliationStatus> | void) | undefined
  onSelectOption?: ((text: string) => void) | undefined
  onUndoFormFill?: ((turnId: string) => Promise<boolean> | boolean) | undefined
  onFocusHandoffControl?: ((control: HandoffRestrictedControl) => Promise<boolean> | boolean) | undefined
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
  const lastScrollTop = useRef(0)
  const [detached, setDetached] = useState(false)
  // Whether anything sits scrolled up under the title bar, which then draws its rule.
  const [scrolled, setScrolled] = useState(false)
  const [unseen, setUnseen] = useState(0)
  const turns = useMemo(() => group(snapshot.entries, snapshot.connection.busy),
                        [snapshot.entries, snapshot.connection.busy])
  const previousEntries = useRef(snapshot.entries)
  const anchor = useRef<{node: HTMLElement; offset: number; height: number} | null>(null)
  const anchorRelease = useRef<number | undefined>(undefined)

  const restoreAnchor = useCallback(() => {
    const root = scroller.current
    const saved = anchor.current
    if (!root || !saved) return
    // Restoring history emits a scroll event; it must not make the earlier wheel gesture
    // change whether the thread follows new messages.
    gesture.current = 0
    if (saved.node.isConnected) {
      root.scrollTop += saved.node.getBoundingClientRect().top
        - root.getBoundingClientRect().top - saved.offset
    } else {
      // React may replace the anchored node while prepending. Keep the scroll position
      // the reader reached meanwhile, not the earlier near-top position at request start.
      root.scrollTop += root.scrollHeight - saved.height
    }
  }, [])

  const captureAnchor = useCallback(() => {
    const root = scroller.current
    if (!root) return
    const top = root.getBoundingClientRect().top
    const visible = (node: HTMLElement) => node.getBoundingClientRect().bottom > top
    const node = Array.from(root.querySelectorAll<HTMLElement>('[data-entry-id]')).find(visible)
      ?? Array.from(root.querySelectorAll<HTMLElement>('[data-turn-id]')).find(visible)
    anchor.current = node ? {node, offset: node.getBoundingClientRect().top - top,
      height: root.scrollHeight} : null
  }, [])

  const scheduleAnchorRelease = useCallback(() => {
    window.clearTimeout(anchorRelease.current)
    anchorRelease.current = window.setTimeout(() => {
      restoreAnchor()
      anchor.current = null
    }, 500)
  }, [restoreAnchor])
  useEffect(() => () => window.clearTimeout(anchorRelease.current), [])

  const loadOlder = useCallback(() => {
    if (!scroller.current || snapshot.loadingOlder || !snapshot.hasOlder) return
    window.clearTimeout(anchorRelease.current)
    follow.current = false
    captureAnchor()
    onLoadOlder()
  }, [snapshot.loadingOlder, snapshot.hasOlder, onLoadOlder, captureAnchor])

  useLayoutEffect(() => {
    // The loading flag can flip while the user's wheel is still moving toward the top.
    // Restoring at that point jumps back to the first near-top position where the request
    // began. Only added entries change the geometry that needs restoring.
    if (previousEntries.current !== snapshot.entries) restoreAnchor()
    // The new list can reflow again after this commit. ResizeObserver keeps the same
    // visible node steady until the layout has been quiet for half a second.
    if (!snapshot.loadingOlder && anchor.current) scheduleAnchorRelease()
  }, [snapshot.entries, snapshot.loadingOlder, restoreAnchor, scheduleAnchorRelease])
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
    if (typeof root.scrollTo === 'function') {
      root.scrollTo({top: root.scrollHeight, behavior: 'auto'})
    } else {
      root.scrollTop = root.scrollHeight
    }
    lastScrollTop.current = root.scrollTop
    setDetached(false)
    setUnseen(0)
    follow.current = true
  }, [])

  const jumpToLatest = useCallback(() => {
    const root = scroller.current
    if (!root) return
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    follow.current = true
    setDetached(false)
    setUnseen(0)
    if (typeof root.scrollTo === 'function') {
      root.scrollTo({top: root.scrollHeight, behavior: reducedMotion ? 'auto' : 'smooth'})
    } else {
      root.scrollTop = root.scrollHeight
    }
    lastScrollTop.current = root.scrollTop
  }, [])

  // One scroll listener decides both things: whether to keep following, and whether the
  // reader is far enough away to be offered a way back. Any upward scroll immediately
  // detaches, and following re-engages only within REATTACH_BOTTOM (4px), via jumpLatest,
  // or when submitting a new message.
  useEffect(() => {
    const root = scroller.current
    if (!root) return
    lastScrollTop.current = root.scrollTop
    const note = () => { gesture.current = Date.now() }
    const onScroll = guarded('thread scroll', () => {
      const currentTop = root.scrollTop
      const delta = currentTop - lastScrollTop.current
      lastScrollTop.current = currentTop
      const distance = root.scrollHeight - currentTop - root.clientHeight
      // The person can keep scrolling while the request is in flight. Capture the actual
      // resting position, even when a slow history response outlives the gesture window.
      // The anchor retires after the newly prepended list has finished reflowing.
      if (anchor.current) captureAnchor()
      const gestured = Date.now() - gesture.current < GESTURE_WINDOW_MS
      if (delta < 0 && (gestured || distance > 0)) {
        follow.current = false
      } else if (distance <= REATTACH_BOTTOM) {
        follow.current = true
        setUnseen(0)
      } else if (gestured) {
        follow.current = false
      }
      setDetached(!follow.current && distance > JUMP_REVEAL_DISTANCE)
      setScrolled(currentTop > 0)
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
  }, [captureAnchor])

  // Anything that changes the height of the thread -- a new entry, prose revealing one
  // character at a time, a card opening -- keeps the newest line in view while following.
  useEffect(() => {
    const node = content.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(guarded('thread resize', () => {
      // A new conversation's greeting is read from the top; there is no newest line to keep.
      if (anchor.current) {
        restoreAnchor()
        if (!snapshot.loadingOlder) scheduleAnchorRelease()
      }
      else if (follow.current && !node.hasAttribute('data-empty')) pin()
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
  }, [pin, restoreAnchor, scheduleAnchorRelease, snapshot.loadingOlder])

  // Submitting is an explicit request to watch what happens next.
  const optimisticTurnId = snapshot.optimistic?.turnId
  useEffect(() => {
    if (optimisticTurnId) pin()
  }, [optimisticTurnId, pin])

  useEffect(() => {
    const node = top.current
    const root = scroller.current
    if (!node || !root || !snapshot.hasOlder || snapshot.loadingOlder || snapshot.historyError
        || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(
      guarded('load older', (records: IntersectionObserverEntry[]) => {
        if (records.at(-1)?.isIntersecting && !follow.current) loadOlder()
      }), {root, rootMargin: '160px 0px 0px 0px'})
    observer.observe(node)
    return () => observer.disconnect()
  }, [snapshot.hasOlder, snapshot.loadingOlder, snapshot.historyError, loadOlder])

  useEffect(() => {
    const previous = previousEntries.current
    previousEntries.current = snapshot.entries
    const last = previous.at(-1)
    // Prepending history is not a new arrival. Count only entries after the former tail.
    const tail = last ? snapshot.entries.findIndex(
      entry => entry.id === last.id && entry.turn_id === last.turn_id) : -1
    const added = tail >= 0 ? snapshot.entries.length - tail - 1
      : Math.max(0, snapshot.entries.length - previous.length)
    if (added <= 0) return
    if (follow.current) pin()
    else setUnseen(count => count + added)
  }, [snapshot.entries, pin])

  const empty = turns.length === 0 && !snapshot.optimistic
  // A conversation this tab already has, being fetched: not the greeting of a new one.
  const loading = empty && ['connecting', 'reconnecting'].includes(snapshot.connection.state)
  const historyUnavailable = empty && snapshot.connection.state === 'error'

  return (
    <div className="woa-thread-shell" data-scrolled={scrolled ? 'true' : undefined}>
    <div className="woa-thread" ref={scroller} role="log" aria-live="polite"
         aria-label={labels.timeline} tabIndex={0}>
      {/* One box holding everything that can change height, so a single ResizeObserver
          sees every kind of growth instead of one effect per kind. */}
      <div className="woa-thread__content" ref={content}
           data-approval={props.footer ? 'true' : undefined}
           data-empty={empty && !loading && !historyUnavailable ? 'true' : undefined}>
      <div ref={top} aria-hidden="true" />
      {snapshot.hasOlder && (
        <div className="woa-history">
          {snapshot.historyError && <p role="status">{labels.historyLoadFailed}</p>}
          <button type="button" disabled={snapshot.loadingOlder} onClick={loadOlder}>
            {snapshot.loadingOlder ? labels.loadingTimeline
              : snapshot.historyError ? labels.retry : labels.loadOlder}
          </button>
        </div>
      )}
      {loading ? <Skeleton /> : historyUnavailable ? null : empty ? props.children : (
        <ol className="woa-turns">
          {turns.map((turn, turnIndex) => {
            const isLatestTurn = turnIndex === turns.length - 1
            const optionsDisabled = snapshot.connection.busy || snapshot.closed || !isLatestTurn
            const hasOutcomeContent = Boolean(
              turn.outcome && (
                turn.outcome.text
                || (turn.outcome.table && turn.outcome.table.columns.length > 0)
                || turn.outcome.input_options?.length
                || turn.outcome.handoff
              ),
            )
            const retryableFailure = !turn.running && Boolean(turn.goal?.text) && (
              turn.outcome
                ? (turn.outcome.panel_state === 'error' && Boolean(turn.outcome.retryable))
                : turn.steps.some(step => step.panel_state === 'error' && Boolean(step.retryable))
            )
            return (
            <li className="woa-turn" key={turn.id || 'session'} data-turn-id={turn.id}
                data-settled={turn.running ? undefined : 'true'}>
              {turn.goal && <p className="woa-user" data-entry-id={turn.goal.id}>{turn.goal.text}</p>}
              <div className="woa-agent">
                {/* While the card is up, the card is what the turn is doing: a line above it
                    saying that a confirmation is needed said it twice. */}
                {turn.running && turn.steps.length === 0 && !turn.outcome
                  && <Starting labels={labels} />}
                {turn.steps.length > 0 && !(props.footer && turn.running
                    && turn.steps.at(-1)?.kind === 'approval') && (
                  <WorkProcess turn={turn} labels={labels}
                               stopping={turn.running && snapshot.connection.stopping} />
                )}
                {/* The server supplies the outcome, including why a confirmation expired. */}
                {hasOutcomeContent && turn.outcome && (
                  <Outcome entry={turn.outcome} labels={labels} limits={limits}
                           live={!turn.outcome.replayed && !settledAtMount.current?.has(turn.outcome.id)}
                           allowedOrigins={allowedOrigins}
                           optionsDisabled={optionsDisabled}
                           onOpenTarget={props.onOpenTarget}
                           onReconcile={props.onReconcile}
                           onSelectOption={props.onSelectOption}
                           onUndoFormFill={props.onUndoFormFill}
                           onFocusHandoffControl={props.onFocusHandoffControl} />
                )}
                {retryableFailure && props.onRetryTurn && turn.goal?.text && (
                  <div className="woa-turn__actions">
                    <button type="button" className="woa-btn woa-btn--ghost woa-turn__retry"
                            disabled={snapshot.connection.busy || snapshot.closed}
                            onClick={() => props.onRetryTurn?.(turn.goal!.text)}>
                      <RotateCcw {...iconProps(14)} />
                      <span>{labels.retryTurn}</span>
                    </button>
                  </div>
                )}
                {props.localDevMode === true && !turn.running && turn.outcome?.debug_trace && (
                  <DebugTracePanel trace={turn.outcome.debug_trace} labels={labels} />
                )}
              </div>
            </li>
            )
          })}
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
            : <div className="woa-agent"><Starting labels={labels} /></div>}
        </div>
      )}
      {props.footer}
      <div ref={bottom} aria-hidden="true" />
      </div>
    </div>
    {detached && (
      <button type="button" className="woa-jump" onClick={jumpToLatest}
              aria-label={`${labels.jumpLatest}${unseen > 0 ? ` · ${unseen}` : ''}`}
              title={labels.jumpLatest}>
        <ArrowDown {...iconProps(16)} />
      </button>
    )}
    </div>
  )
}
