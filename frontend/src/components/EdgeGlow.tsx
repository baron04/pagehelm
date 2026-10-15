import {useEffect, useRef} from 'react'
import {createEdgeLight, type EdgeLight} from './edgeLight'

/** Original ai-motion rendering with an inert CSS fallback. */
export function EdgeGlow({active}: {active: boolean}): React.JSX.Element {
  const renderer = useRef<HTMLDivElement>(null)
  const light = useRef<EdgeLight | null>(null)
  useEffect(() => {
    try { light.current = createEdgeLight(renderer.current!) }
    catch { /* Optional rendering must not interrupt the panel or the host page. */ }
    return () => { light.current?.dispose(); light.current = null }
  }, [])
  useEffect(() => { light.current?.setActive(active) }, [active])
  return (
    <div className="woa-screen-effects__glow">
      <div ref={renderer} className="woa-screen-effects__renderer" />
      <div className="woa-screen-effects__fallback">
        <div className="woa-screen-effects__light" />
        <div className="woa-screen-effects__light woa-screen-effects__light--alternate" />
      </div>
    </div>
  )
}
