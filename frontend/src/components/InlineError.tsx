import {RotateCcw, TriangleAlert, WifiOff} from 'lucide-react'
import type {Labels, PanelConfiguration, PanelLimits} from '../contract/panel'
import type {HostNotice} from '../bridge/host'
import {iconProps} from './icon'

/**
 * Not a card, and not one fixed place either: a failure is shown where the thing that
 * failed lives. This is the session-level line -- it sits directly above the composer,
 * because that is where "can I keep going?" is asked.
 *
 * Retry only appears when the request can actually be retried; the server's own
 * `terminal_error_codes` decides, so the button cannot outlive the truth.
 */
export function InlineError({notice, configuration, labels, limits, onRetry, offline}: {
  notice: HostNotice
  configuration: PanelConfiguration
  labels: Labels
  limits: PanelLimits
  onRetry: () => void
  offline: boolean
}): React.JSX.Element {
  const terminal = configuration.terminal_error_codes.includes(notice.code)
  // A code with no guidance of its own is *not* a dropped connection. Borrowing the
  // reconnecting line for it told people their network had failed whenever the server
  // did, which is both wrong and unactionable.
  const message = (notice.message ?? configuration.notices[notice.code]
    ?? configuration.notices[offline ? 'offline' : 'unexpected']
    ?? configuration.notices.reconnecting
    ?? '').slice(0, limits.notice_max_chars)
  const Icon = offline ? WifiOff : TriangleAlert
  return (
    <div className="woa-inline-error" role="status" data-code={notice.code}
         data-terminal={terminal ? 'true' : undefined}>
      <Icon {...iconProps(14)} />
      <span className="woa-inline-error__text">{message}</span>
      {notice.retryable && !terminal && (
        <button type="button" className="woa-inline-error__retry" onClick={onRetry}>
          <RotateCcw {...iconProps(12)} />
          {labels.retry}
        </button>
      )}
    </div>
  )
}
