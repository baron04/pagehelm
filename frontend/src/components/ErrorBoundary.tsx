import {Component, type ErrorInfo, type ReactNode} from 'react'
import {TriangleAlert} from 'lucide-react'

/**
 * Where every panel failure ends.
 *
 * It logs and returns. It never rethrows, and it is emphatically *not* a global handler:
 * the panel shares a realm with the host application, so installing `window.onerror`
 * would be the same trespass in the other direction -- it would catch the application's
 * errors too, and quietly take over a slot the application may already own.
 */
export function reportPanelFailure(scope: string, error: unknown, detail?: unknown): void {
  try {
    console.error(`[woa] ${scope}`, error, detail ?? '')
  } catch {
    // A host that has replaced console is not a reason to throw from a failure path.
  }
}

/** Run a callback the panel scheduled -- an observer, a timer -- without leaking a throw. */
export function guarded<T extends unknown[]>(
  scope: string, run: (...args: T) => void): (...args: T) => void {
  return (...args: T) => {
    try {
      run(...args)
    } catch (error) {
      reportPanelFailure(scope, error)
    }
  }
}

/**
 * What a person sees when the panel itself has failed.
 *
 * Rendering nothing was worse than rendering this: a panel that disappears reads as one
 * that never loaded, and the reader has no way to tell the difference between broken and
 * absent.
 */
export function PanelFailure({text, reloadLabel}: {
  text: string
  reloadLabel: string
}): React.JSX.Element {
  return (
    <div className="woa-fatal" role="status">
      <TriangleAlert size={14} strokeWidth={1.7} aria-hidden="true" />
      <span>{text}</span>
      <button type="button" onClick={() => {
        try {
          location.reload()
        } catch (error) {
          reportPanelFailure('reload from the failure notice', error)
        }
      }}>{reloadLabel}</button>
    </div>
  )
}

/**
 * The thread renders untrusted model output. If something in there throws, it must not
 * take the confirmation card down with it -- an operator who cannot answer an approval is
 * stuck holding a half-finished write. So the thread gets its own boundary and the rest of
 * the window keeps working.
 */
export class ErrorBoundary extends Component<
  {fallback: ReactNode; children: ReactNode},
  {failed: boolean}
> {
  override state = {failed: false}

  static getDerivedStateFromError(): {failed: boolean} {
    return {failed: true}
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    reportPanelFailure('panel subtree failed', error, info.componentStack)
  }

  override render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children
  }
}
