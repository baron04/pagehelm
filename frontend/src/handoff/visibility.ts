/** Browser-only geometry. These states describe a pointer target, never permission to submit. */
export interface Box {left: number; top: number; right: number; bottom: number}
export type Direction = 'above' | 'below' | 'left' | 'right'
export type TargetView = {kind: 'visible'; box: Box} | {kind: 'outside'; direction: Direction; clip: Box}
  | {kind: 'hidden' | 'disabled' | 'assistant' | 'covered'}

export function clippedBox(element: Element): Box {
  const view = element.ownerDocument.defaultView!
  const viewport = view.visualViewport
  const clip = {left: viewport?.offsetLeft ?? 0, top: viewport?.offsetTop ?? 0,
    right: (viewport?.offsetLeft ?? 0) + (viewport?.width ?? view.innerWidth),
    bottom: (viewport?.offsetTop ?? 0) + (viewport?.height ?? view.innerHeight)}
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    const style = view.getComputedStyle(parent)
    const x = /auto|scroll|hidden|clip|overlay/.test(style.overflowX)
    const y = /auto|scroll|hidden|clip|overlay/.test(style.overflowY)
    if (!x && !y) continue
    const r = parent.getBoundingClientRect()
    // Border and native scrollbars are outside a container's content viewport.
    const scaleX = parent.offsetWidth ? r.width / parent.offsetWidth : 1
    const scaleY = parent.offsetHeight ? r.height / parent.offsetHeight : 1
    if (x) {clip.left = Math.max(clip.left, r.left + parent.clientLeft * scaleX)
      clip.right = Math.min(clip.right, r.left + (parent.clientLeft + parent.clientWidth) * scaleX)}
    if (y) {clip.top = Math.max(clip.top, r.top + parent.clientTop * scaleY)
      clip.bottom = Math.min(clip.bottom, r.top + (parent.clientTop + parent.clientHeight) * scaleY)}
  }
  return clip
}

export function targetView(element: HTMLElement, panel: HTMLElement): TargetView {
  const doc = element.ownerDocument, view = doc.defaultView!
  const r = element.getBoundingClientRect()
  if (!r.width || !r.height || element.closest('[hidden],[aria-hidden="true"]')) return {kind: 'hidden'}
  for (let node: HTMLElement | null = element; node; node = node.parentElement) {
    const style = view.getComputedStyle(node)
    if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0)
      return {kind: 'hidden'}
  }
  if (element.matches(':disabled') || element.closest('[aria-disabled="true"],[inert]') ||
      view.getComputedStyle(element).pointerEvents === 'none') return {kind: 'disabled'}
  const clip = clippedBox(element)
  const overflow: [Direction, number][] = [['above', clip.top-r.top], ['below', r.bottom-clip.bottom],
    ['left', clip.left-r.left], ['right', r.right-clip.right]]
  const outside = overflow.filter(([, distance]) => distance > 1).sort((a, b) => b[1]-a[1])[0]
  if (outside) return {kind: 'outside', direction: outside[0], clip}
  const samples = [[.5,.5], [.18,.2], [.82,.2], [.18,.8], [.82,.8]]
  let assistant = false, covered = false
  for (const [x=0, y=0] of samples) {
    const hit = doc.elementFromPoint(r.left+r.width*x, r.top+r.height*y)
    if (hit && (hit === element || element.contains(hit))) continue
    if (hit && (hit === panel || panel.contains(hit))) assistant = true
    else covered = true
  }
  return assistant ? {kind: 'assistant'} : covered ? {kind: 'covered'} : {kind: 'visible', box: r}
}
