import {useState} from 'react'
import {ChevronDown} from 'lucide-react'
import type {Labels, TurnDebugTrace} from '../contract/panel'
import {CopyButton} from './CopyButton'
import {iconProps} from './icon'

function formatTimings(trace: TurnDebugTrace): string[] {
  const parts: string[] = []
  if (trace.wall_ms !== undefined) parts.push(`wall ${trace.wall_ms}ms`)
  if (trace.stage_ms) {
    for (const [stage, ms] of Object.entries(trace.stage_ms)) {
      parts.push(`${stage} ${ms}ms`)
    }
  }
  return parts
}

function formatTokens(trace: TurnDebugTrace): string[] {
  const parts: string[] = []
  if (trace.tokens) {
    const {input, output, total} = trace.tokens
    if (input !== undefined) parts.push(`in ${input}`)
    if (output !== undefined) parts.push(`out ${output}`)
    if (total !== undefined) parts.push(`total ${total}`)
  }
  if (trace.model_calls !== undefined) parts.push(`calls ${trace.model_calls}`)
  return parts
}

/** Collapsible diagnostic trace inspector rendered for settled turns only in local-dev mode. */
export function DebugTracePanel({trace, labels}: {
  trace: TurnDebugTrace
  labels: Labels
}): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const timings = formatTimings(trace)
  const tokens = formatTokens(trace)
  const toolChain = trace.tool_chain ?? []
  const checks = trace.checks ?? []

  return (
    <div className="woa-trace" data-testid="woa-debug-trace" data-open={open ? 'true' : undefined}>
      <div className="woa-trace__bar">
        <button type="button" className="woa-trace__toggle" aria-expanded={open}
                onClick={() => setOpen(prev => !prev)}>
          <span>{open ? labels.debugTraceHide : labels.debugTrace}</span>
          <ChevronDown {...iconProps(14)} className="woa-trace__chevron" />
        </button>
        <CopyButton text={() => JSON.stringify(trace, null, 2)}
                    label={labels.debugTraceCopy} copiedLabel={labels.copied}
                    className="woa-trace__copy" showLabel />
      </div>
      {open && (
        <dl className="woa-trace__detail">
          {trace.first_route_kind && (
            <>
              <dt>{labels.debugTraceRoute}</dt>
              <dd><code>{trace.first_route_kind}</code></dd>
            </>
          )}
          {trace.stop_reason && (
            <>
              <dt>{labels.debugTraceStopReason}</dt>
              <dd><code>{trace.stop_reason}</code></dd>
            </>
          )}
          {timings.length > 0 && (
            <>
              <dt>{labels.debugTraceTimings}</dt>
              <dd>{timings.join(' · ')}</dd>
            </>
          )}
          {tokens.length > 0 && (
            <>
              <dt>{labels.debugTraceTokens}</dt>
              <dd>{tokens.join(' · ')}</dd>
            </>
          )}
          {toolChain.length > 0 && (
            <>
              <dt>{labels.debugTraceToolChain}</dt>
              <dd>
                <ol className="woa-trace__list">
                  {toolChain.map((item, index) => (
                    <li className="woa-trace__item" key={`${item.step ?? index}:${item.tool}`}>
                      {item.step !== undefined && <span className="woa-trace__meta">#{item.step}</span>}
                      <code>{item.tool}</code>
                      <span>{item.elapsed_ms}ms</span>
                      {item.ok !== undefined && (
                        <span className="woa-trace__status" data-ok={String(item.ok)}>
                          {item.ok ? 'ok' : 'err'}
                        </span>
                      )}
                      {item.error_code && <code>{item.error_code}</code>}
                    </li>
                  ))}
                </ol>
              </dd>
            </>
          )}
          {checks.length > 0 && (
            <>
              <dt>{labels.debugTraceChecks}</dt>
              <dd>
                <ul className="woa-trace__list">
                  {checks.map((item, index) => (
                    <li className="woa-trace__item" key={`${item.check}:${item.attempt ?? index}`}>
                      {item.attempt !== undefined && <span className="woa-trace__meta">#{item.attempt}</span>}
                      <code>{item.check}</code>
                      <span>{item.outcome}</span>
                    </li>
                  ))}
                </ul>
              </dd>
            </>
          )}
        </dl>
      )}
    </div>
  )
}
