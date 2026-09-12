import {memo, useEffect, useMemo, useRef, useState} from 'react'
import {
  AlertTriangle, ChevronDown, ChevronRight, CircleAlert, CircleCheck, CircleDot,
  ExternalLink, MousePointerClick, RotateCcw, Search, ShieldQuestion,
} from 'lucide-react'
import type {Labels, PanelLimits, TimelineEntry} from '../contract/panel'
import type {PanelSnapshot} from '../bridge/host'
import {Answer} from './Answer'

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
function elapsed(turn: Turn): string {
  const from = turn.goal?.at ?? turn.steps[0]?.at ?? 0
  const to = turn.outcome?.at ?? turn.steps.at(-1)?.at ?? 0
  if (!from || !to || to < from) return ''
  const seconds = (to - from) / 1000
  return seconds < 60 ? `${seconds.toFixed(1)}s`
    : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
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

const WorkProcess = memo(function WorkProcess({turn, labels, took}: {
  turn: Turn
  labels: Labels
  took: string
}): React.JSX.Element {
  // Open while it runs, folded once it lands -- and whatever the reader last chose wins.
  const [override, setOverride] = useState<boolean | null>(null)
  const open = override ?? turn.running
  const approvals = turn.steps.filter(step => step.kind === 'approval').length
  const summary = [
    labels.workProcess,
    turn.running ? `第 ${turn.steps.length} ${labels.stepCount}`
      : `${turn.steps.length} ${labels.stepCount}`,
    approvals ? `${approvals} ${labels.approvalCount}` : '',
  ].filter(Boolean).join(' · ')

  return (
    <div className="woa-work" data-open={open ? 'true' : 'false'}>
      <button type="button" className="woa-work__summary" aria-expanded={open}
              onClick={() => setOverride(!open)}>
        {turn.running
          ? <span className="woa-spinner" aria-hidden="true" />
          : (open
            ? <ChevronDown className="woa-work__chevron" size={13} strokeWidth={1.7} aria-hidden="true" />
            : <ChevronRight className="woa-work__chevron" size={13} strokeWidth={1.7} aria-hidden="true" />)}
        <span>{summary}</span>
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
      <Answer text={entry.text.slice(0, limits.result_max_chars)}
              authored={entry.authored ?? 'server'} live={live}
              labels={labels} allowedOrigins={allowedOrigins} />
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
  const bottom = useRef<HTMLDivElement>(null)
  const top = useRef<HTMLDivElement>(null)
  const [atBottom, setAtBottom] = useState(true)
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

  // A sentinel plus IntersectionObserver is both cheaper and more accurate than measuring
  // scrollHeight on every scroll event.
  useEffect(() => {
    const node = bottom.current
    const root = scroller.current
    if (!node || !root || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(records => {
      const visible = records.at(-1)?.isIntersecting ?? true
      setAtBottom(visible)
      if (visible) setUnseen(0)
    }, {root, threshold: 0.01})
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const node = top.current
    const root = scroller.current
    if (!node || !root || !snapshot.hasOlder || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(records => {
      if (records.at(-1)?.isIntersecting) onLoadOlder()
    }, {root, rootMargin: '160px 0px 0px 0px'})
    observer.observe(node)
    return () => observer.disconnect()
  }, [snapshot.hasOlder, onLoadOlder])

  useEffect(() => {
    const added = snapshot.entries.length - lastCount.current
    lastCount.current = snapshot.entries.length
    if (added <= 0) return
    if (atBottom) bottom.current?.scrollIntoView({block: 'end'})
    else setUnseen(count => count + added)
  }, [snapshot.entries.length, atBottom])

  const empty = turns.length === 0 && !snapshot.optimistic

  return (
    <div className="woa-thread" ref={scroller} role="log" aria-live="polite"
         aria-label={labels.timeline} tabIndex={0}>
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
                {turn.steps.length > 0 && (
                  <WorkProcess turn={turn} labels={labels} took={elapsed(turn)} />
                )}
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
      {!atBottom && unseen > 0 && (
        <button type="button" className="woa-jump"
                onClick={() => bottom.current?.scrollIntoView({behavior: 'smooth', block: 'end'})}>
          <RotateCcw size={12} strokeWidth={1.9} aria-hidden="true" />
          {labels.jumpLatest} · {unseen}
        </button>
      )}
    </div>
  )
}
