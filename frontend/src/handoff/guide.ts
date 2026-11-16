/** One browser-local guide to an exact handoff target; it never operates it. */
import {contained, listen} from '../runtime/contain'
import {AVOID_TARGET_EVENT} from '../state/geometry'
import {clippedBox, targetView, type Box, type Direction} from './visibility'

export interface ControlGuide {
  clear(): void
  active(): boolean
  readonly preserveOnInteraction: true
}
const SVG_NS = 'http://www.w3.org/2000/svg'
const middle = (b: Box) => ({x: (b.left+b.right)/2, y: (b.top+b.bottom)/2})
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(v, Math.max(lo, hi)))
function attr(node: Element, name: string, value: string) {
  if (node.getAttribute(name) !== value) node.setAttribute(name, value)
}
function position(node: HTMLElement, x: number, y: number, width?: number) {
  for (const [key, value] of Object.entries({left: `${Math.round(x)}px`, top: `${Math.round(y)}px`,
    ...(width === undefined ? {} : {'max-width': `${Math.max(0, Math.round(width))}px`})}))
    if (node.style.getPropertyValue(key) !== value) node.style.setProperty(key, value)
}

export function createControlGuide(options: {target: HTMLElement; container: HTMLElement;
  turnId: string; current: () => boolean; purpose: 'submit' | 'action'}): ControlGuide | null {
  const {target, container, turnId, current} = options
  const name = options.purpose === 'submit' ? '提交按钮' : '操作位置'
  const directions: Record<Direction, string> = {above: `↑ ${name}在上方`, below: `↓ ${name}在下方`,
    left: `← ${name}在左侧`, right: `→ ${name}在右侧`}
  const messages = {hidden: `${name}暂未显示，请检查所在区域。`,
    disabled: options.purpose === 'submit' ? '提交按钮暂不可用，请检查表单必填项和校验提示。'
      : '操作位置暂不可用，请检查页面状态。',
    assistant: `${name}被助手遮挡，请移开或收起助手。`,
    covered: `${name}被其他内容遮挡，请先处理遮挡。`}
  // A containing page cannot draw accurate lines in another document's coordinates.
  if (target.ownerDocument !== container.ownerDocument) return null
  const host = container.querySelector<HTMLElement>('#woa-assistant')
  const root = host?.shadowRoot
  if (!host || !root) return null
  const doc = target.ownerDocument, win = doc.defaultView!
  const overlay = doc.createElement('div')
  overlay.className = 'woa-submit-guide'
  overlay.setAttribute('data-page-agent-ignore', 'true')
  overlay.setAttribute('data-page-agent-not-interactive', 'true')
  const svg = doc.createElementNS(SVG_NS, 'svg')
  svg.setAttribute('aria-hidden', 'true')
  const path = doc.createElementNS(SVG_NS, 'path'), head = doc.createElementNS(SVG_NS, 'path')
  const dot = doc.createElementNS(SVG_NS, 'circle'); dot.setAttribute('r', '3')
  svg.append(path, head, dot)
  const hint = doc.createElement('div'); hint.className = 'woa-submit-guide__hint'
  hint.setAttribute('role', 'status'); hint.setAttribute('aria-live', 'polite')
  overlay.append(svg, hint)
  root.append(overlay)
  let alive = true, frame = 0, avoidedKey = ''
  const originalUrl = win.location.href
  const dialog = target.closest('[role="dialog"],dialog')
  const stops: (() => void)[] = []
  function active() {
    return alive && target.isConnected && container.isConnected && win.location.href === originalUrl && current()
      && (!dialog || (dialog.isConnected && win.getComputedStyle(dialog).display !== 'none'
        && !dialog.hasAttribute('hidden') && dialog.getAttribute('aria-hidden') !== 'true'))
  }
  function clear() {
    if (!alive) return
    alive = false
    win.cancelAnimationFrame(frame)
    observer.disconnect(); panelObserver.disconnect(); resize?.disconnect()
    stops.forEach(stop => stop())
    overlay.remove()
  }
  function showHint(text: string, x: number, y: number, width: number, kind: string) {
    attr(svg, 'hidden', '')
    hint.hidden = false
    attr(overlay, 'data-state', kind)
    if (hint.textContent !== text) hint.textContent = text
    position(hint, x, y, width)
  }
  function updateLayout() {
    frame = 0
    if (!active()) {clear(); return}
    const panel = root!.querySelector<HTMLElement>('.woa-window')
    if (!panel) {attr(svg, 'hidden', ''); hint.hidden = true; return}
    const turns = [...root!.querySelectorAll<HTMLElement>('.woa-turn')]
    if (turnId && turns.length && turns.at(-1)!.dataset.turnId !== turnId) {clear(); return}
    const turn = turnId ? turns.find(node => node.dataset.turnId === turnId) : turns.at(-1)
    const answer = turn?.querySelector<HTMLElement>('.woa-result .woa-md')
    const source = answer?.lastElementChild
    // The line starts from this turn's answer, never an older conversation or a typing caret.
    const sourceRect = source?.tagName === 'P' ? source.getBoundingClientRect() : null
    const p = panel.getBoundingClientRect(), viewport = win.visualViewport
    const maxX = (viewport?.offsetLeft ?? 0) + (viewport?.width ?? win.innerWidth)
    const maxY = (viewport?.offsetTop ?? 0) + (viewport?.height ?? win.innerHeight)
    const view = targetView(target, container)
    attr(overlay, 'data-state', view.kind)
    if (view.kind === 'outside') {
      const c = view.clip, point = middle(target.getBoundingClientRect())
      const width = Math.min(240, Math.max(120, c.right-c.left-16))
      const x = view.direction === 'left' ? c.left+8 : view.direction === 'right' ? c.right-width-8
        : clamp(point.x-width/2, c.left+8, c.right-width-8)
      const y = view.direction === 'above' ? c.top+8 : view.direction === 'below' ? c.bottom-44
        : clamp(point.y-18, c.top+8, c.bottom-44)
      showHint(directions[view.direction], clamp(x, 8, maxX-width-8), clamp(y, 8, maxY-44), width, 'outside')
      return
    }
    if (view.kind !== 'visible') {
      if (view.kind === 'assistant' && panel.dataset.mode !== 'full') {
        const tr = target.getBoundingClientRect()
        const key = `${Math.round(tr.left)}:${Math.round(tr.top)}:${Math.round(tr.right)}:${Math.round(tr.bottom)}`
        if (avoidedKey !== key) {
          avoidedKey = key
          win.dispatchEvent(new win.CustomEvent(AVOID_TARGET_EVENT, {
            detail: {left: tr.left, top: tr.top, right: tr.right, bottom: tr.bottom},
          }))
          schedule()
          return
        }
      }
      showHint(messages[view.kind], p.left+20,
        clamp((sourceRect?.bottom ?? p.bottom-170)+12, p.top+70, p.bottom-130), p.width-40, view.kind)
      return
    }
    avoidedKey = ''
    hint.hidden = true
    if (!sourceRect || !sourceRect.width || !sourceRect.height || answer?.querySelector('.woa-md__caret')) {
      attr(svg, 'hidden', ''); return
    }
    const clip = clippedBox(source!)
    if (sourceRect.top < clip.top || sourceRect.bottom > clip.bottom ||
        sourceRect.left < clip.left || sourceRect.right > clip.right) {attr(svg, 'hidden', ''); return}
    const t = view.box, gap = 12, sy = middle(sourceRect).y
    let sx: number, ex: number, ey: number, points: string, arrow: string
    if (t.left >= p.right) {
      sx = sourceRect.right+7; ex = middle(t).x; ey = t.top-7
      const lane = Math.min(p.right+gap, t.left-8)
      points = `M ${sx} ${sy} H ${lane} V ${ey-12} H ${ex} V ${ey}`
      arrow = `M ${ex-5} ${ey-6} L ${ex} ${ey} L ${ex+5} ${ey-6}`
    } else if (t.right <= p.left) {
      sx = sourceRect.left-7; ex = t.right+7; ey = middle(t).y
      const lane = Math.max(p.left-gap, t.right+9)
      points = `M ${sx} ${sy} H ${lane} V ${ey} H ${ex}`
      arrow = `M ${ex+6} ${ey-5} L ${ex} ${ey} L ${ex+6} ${ey+5}`
    } else {
      // A target above/below the panel: route around its nearest outside edge.
      const right = maxX-p.right >= p.left
      sx = right ? sourceRect.right+7 : sourceRect.left-7
      const lane = right ? Math.min(maxX-6, p.right+gap) : Math.max(6, p.left-gap)
      ex = middle(t).x
      const below = t.top >= p.bottom
      ey = below ? t.top-7 : t.bottom+7
      const approach = below ? ey-12 : ey+12
      points = `M ${sx} ${sy} H ${lane} V ${approach} H ${ex} V ${ey}`
      arrow = `M ${ex-5} ${ey+(below?-6:6)} L ${ex} ${ey} L ${ex+5} ${ey+(below?-6:6)}`
    }
    attr(svg, 'viewBox', `0 0 ${win.innerWidth} ${win.innerHeight}`)
    attr(path, 'd', points); attr(head, 'd', arrow)
    attr(dot, 'cx', String(sx)); attr(dot, 'cy', String(sy))
    svg.removeAttribute('hidden')
  }
  const update = contained('submit guide layout', () => {
    try {updateLayout()} catch {clear()} // A visual failure cannot leave a stale arrow.
  })
  const schedule = () => {if (alive && !frame) frame = win.requestAnimationFrame(update)}
  const observer = new MutationObserver(contained('submit guide page changes', schedule))
  observer.observe(doc.documentElement, {subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ['style', 'class', 'hidden', 'open', 'disabled', 'aria-disabled', 'aria-hidden', 'inert',
      'id', 'form', 'role', 'aria-label']})
  const panelObserver = new MutationObserver(contained('submit guide panel changes', changes => {
    if (changes.some(change => !overlay.contains(change.target))) schedule()
  }))
  panelObserver.observe(root, {subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ['style', 'class', 'data-mode']})
  const resize = typeof ResizeObserver === 'undefined' ? null
    : new ResizeObserver(contained('submit guide resize', schedule))
  resize?.observe(target); resize?.observe(host)
  for (let node = target.parentElement; node; node = node.parentElement) resize?.observe(node)
  stops.push(listen('submit guide scroll', doc, 'scroll', schedule, {capture: true, passive: true}),
    listen('submit guide panel scroll', root, 'scroll', schedule, {capture: true, passive: true}),
    listen('submit guide page transition', doc, 'transitionend', schedule, true),
    listen('submit guide panel transition', root, 'transitionend', schedule, true),
    listen('submit guide panel animation', root, 'animationend', schedule, true),
    listen('submit guide resize', win, 'resize', schedule, {passive: true}),
    listen('submit guide viewport', win.visualViewport, 'resize', schedule, {passive: true}),
    listen('submit guide viewport scroll', win.visualViewport, 'scroll', schedule, {passive: true}),
    listen('submit guide navigation', win, 'popstate', clear),
    listen('submit guide fragment', win, 'hashchange', clear),
    listen('submit guide click', doc, 'click', (event: MouseEvent) => {
      if (event.composedPath().includes(target)) clear()
    }, true),
    listen('submit guide submitted', doc, 'submit', (event: Event) => {
      if (event.target === (target as HTMLButtonElement).form) clear()
    }, true))
  schedule()
  return {clear, active, preserveOnInteraction: true}
}
