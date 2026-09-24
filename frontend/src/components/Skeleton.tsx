import {useEffect, useState} from 'react'

/**
 * How long a wait may take before it is shown. A load that finishes sooner shows nothing at
 * all: a placeholder that flashes up and is replaced reads as flicker, not as progress.
 */
export const SKELETON_DELAY_MS = 300

/** What is still loading, as three lines -- long, short, medium -- once the wait is real. */
export function Skeleton(): React.JSX.Element | null {
  const [shown, setShown] = useState(false)
  useEffect(() => {
    const timer = window.setTimeout(() => setShown(true), SKELETON_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [])
  if (!shown) return null
  return <div className="woa-skeleton" aria-hidden="true"><i /><i /><i /></div>
}
