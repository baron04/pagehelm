import {Motion} from 'ai-motion'

export interface EdgeLight {
  setActive(active: boolean): void
  dispose(): void
}

/** ai-motion owns rendering; this adapter only manages browser presentation lifecycle. */
export function createEdgeLight(container: HTMLElement): EdgeLight {
  const document = container.ownerDocument
  const view = document.defaultView!
  const root = container.getRootNode()
  const themeHost = root instanceof ShadowRoot ? root.host : document.documentElement
  const reducedMotion = view.matchMedia('(prefers-reduced-motion: reduce)')
  const colourScheme = view.matchMedia('(prefers-color-scheme: dark)')
  let active = false, lost = false, disposed = false
  let motion: Motion | null = null
  let mode: 'light' | 'dark' | null = null

  function currentMode(): 'light' | 'dark' {
    const explicit = themeHost.getAttribute('data-theme')
    return explicit === 'dark' || (explicit !== 'light' && colourScheme.matches) ? 'dark' : 'light'
  }
  function schedule(): void {
    if (!motion || disposed) return
    if (active && !reducedMotion.matches && !document.hidden && !lost) motion.start()
    else motion.pause()
  }
  function resize(): void {
    if (!motion || disposed || lost) return
    const {width, height} = container.getBoundingClientRect()
    // ai-motion only resizes/renders while running. Draw one frame even when frozen
    // for accessibility, then pause synchronously before any animation callback runs.
    motion.resize(width, height)
    motion.start()
    schedule()
  }
  function contextLost(event: Event): void {
    event.preventDefault()
    lost = true
    if (motion) delete motion.element.dataset.ready
    schedule()
  }
  function release(): void {
    if (!motion) return
    motion.element.removeEventListener('webglcontextlost', contextLost)
    motion.dispose()
    motion = null
  }
  function initialise(): void {
    const nextMode = currentMode()
    if (mode === nextMode || disposed) return
    release()
    mode = nextMode
    lost = false
    try {
      const {width, height} = container.getBoundingClientRect()
      // Original shader, default palette, glow depth, speed and light/dark tuning.
      // Only the opaque border is disabled to keep the viewport edge soft.
      motion = new Motion({width, height, mode, borderWidth: 0, borderRadius: 0,
        classNames: 'woa-screen-effects__canvas', skipGreeting: true})
      motion.element.addEventListener('webglcontextlost', contextLost)
      container.replaceChildren(motion.element)
      motion.start()
      if ((motion.element as HTMLCanvasElement).getContext('webgl2')!.isContextLost()) {
        lost = true
      } else motion.element.dataset.ready = 'true'
      schedule()
    } catch {
      release()
      // Keep the CSS fallback and the panel usable when WebGL is unavailable.
      const canvas = document.createElement('canvas')
      canvas.className = 'woa-screen-effects__canvas'
      container.replaceChildren(canvas)
    }
  }

  initialise()
  const sizes = new ResizeObserver(resize)
  const themes = new MutationObserver(initialise)
  sizes.observe(container)
  themes.observe(themeHost, {attributes: true, attributeFilter: ['data-theme']})
  reducedMotion.addEventListener('change', schedule)
  colourScheme.addEventListener('change', initialise)
  document.addEventListener('visibilitychange', schedule)

  return {
    setActive(value) { active = value; schedule() },
    dispose() {
      disposed = true
      sizes.disconnect()
      themes.disconnect()
      reducedMotion.removeEventListener('change', schedule)
      colourScheme.removeEventListener('change', initialise)
      document.removeEventListener('visibilitychange', schedule)
      release()
    },
  }
}
