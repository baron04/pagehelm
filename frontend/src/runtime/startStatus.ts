/**
 * What the panel says about a session being started, and about one that could not be.
 *
 * A start fails before any server has spoken, so its words are the panel's own
 * (`presentation.json`, `start`), and they are said where everything else about the
 * assistant is said: in the panel, above the composer, with Retry beside them.
 */
import {start as startText} from '../presentation.json'
import type {PanelHost} from './panelHost'

export type StartText = Omit<typeof startText, 'comment'>
export const START_TEXT: StartText = startText

/** Why starting failed: an HTTP status when the server answered, `null` when it did not. */
export interface StartFailure {
  status: number | null
  /** Whether another attempt is already scheduled. */
  retrying: boolean
}

/** The sentence for a failure: its cause, then what happens next, then the status. */
export function failureSentence(failure: StartFailure, text: StartText = START_TEXT): string {
  const key = failure.status === null ? 'default' : String(failure.status)
  const causes = text.causes as Record<string, string>
  const cause = causes[key] ?? text.causes.default
  const final = failure.status !== null && text.final_statuses.includes(failure.status)
  const next = final ? '' : `，${failure.retrying ? text.retrying : text.stopped}`
  const status = failure.status === null ? '' : `（${failure.status}）`
  return `${cause}${next}${status}。`
}

/** How a start is reported. The supervisor drives it; the panel is where it shows. */
export interface StartStatus {
  /** A session is being made. */
  connecting(): void
  /** It could not be; `retry` starts over when the person asks. */
  unavailable(failure: StartFailure, retry: () => void): void
  /** A session is up: whatever was said about starting is over. */
  started(): void
  /** No session, and none needed yet: the panel is ready for the next message. */
  rested(): void
}

/** The notice a failed start raises; it is the panel's own, not one of the server's codes. */
export const START_FAILED = 'start_failed'

export function panelStartStatus(host: PanelHost): StartStatus {
  return {
    connecting() {
      host.setConnection({state: 'connecting'})
    },
    unavailable(failure, retry) {
      host.setConnection({state: failure.retrying ? 'reconnecting' : 'error'})
      host.failOptimistic()
      host.setRetryStart(retry)
      host.raise(START_FAILED, {retryable: true, message: failureSentence(failure)})
    },
    started() {
      host.setRetryStart(null)
      if (host.snapshot().notice?.code === START_FAILED) host.clearNotice()
    },
    rested() {
      host.setRetryStart(null)
      host.rest()
    },
  }
}
