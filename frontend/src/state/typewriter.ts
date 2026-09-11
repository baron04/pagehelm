/**
 * Reveal model-authored prose one grapheme cluster at a time.
 *
 * Only prose the *model* wrote is revealed this way. A server-projected answer -- the
 * typed query result, its completeness badge, its table -- appears whole, because pretending
 * a fact the server computed is being typed out would be a small, avoidable lie.
 * `TimelineEntry.authored` is what decides, and it comes from the server.
 *
 * The parsed tree is never re-parsed per frame: only a character budget moves.
 */
import {useEffect, useRef, useState} from 'react'

const CHARACTERS_PER_SECOND = 900

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

export interface Reveal {
  /** How many characters of the entry are currently visible. */
  visible: number
  done: boolean
  /** Show the rest immediately -- any real gesture should be able to do this. */
  skip: () => void
}

export function useReveal(total: number, active: boolean): Reveal {
  const [visible, setVisible] = useState(active ? 0 : total)
  const frame = useRef(0)
  const skipped = useRef(!active)

  useEffect(() => {
    if (!active || prefersReducedMotion()) {
      setVisible(total)
      skipped.current = true
      return
    }
    skipped.current = false
    setVisible(0)
    const started = performance.now()
    const step = () => {
      if (skipped.current) return
      const elapsed = (performance.now() - started) / 1000
      const shown = Math.min(total, Math.ceil(elapsed * CHARACTERS_PER_SECOND))
      setVisible(shown)
      if (shown < total) frame.current = requestAnimationFrame(step)
    }
    frame.current = requestAnimationFrame(step)
    return () => cancelAnimationFrame(frame.current)
  }, [active, total])

  return {
    visible,
    done: visible >= total,
    skip: () => {
      skipped.current = true
      cancelAnimationFrame(frame.current)
      setVisible(total)
    },
  }
}
