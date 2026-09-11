import {Component, type ErrorInfo, type ReactNode} from 'react'

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
    console.error('[woa] panel subtree failed', error, info.componentStack)
  }

  override render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children
  }
}
