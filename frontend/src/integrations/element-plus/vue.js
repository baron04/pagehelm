/** Generic Vue 3 VNode traversal and Element Plus DOM visibility/ownership helpers. */

export function isElVisible(element) {
  if (!element) return false
  const rect = element.getBoundingClientRect()
  return Boolean(rect.width && rect.height && !element.closest('[hidden],[aria-hidden="true"]'))
}

export function isElRendered(element) {
  if (!element) return false
  const rect = element.getBoundingClientRect()
  return Boolean(rect.width && rect.height)
}

export function isElInViewport(element) {
  if (!(element instanceof Element) || !element.isConnected) return false
  const rect = element.getBoundingClientRect()
  return rect.width > 0 && rect.height > 0 &&
    rect.bottom > 0 && rect.top < (window.innerHeight || 0) &&
    !element.closest('[hidden],[aria-hidden="true"]')
}

export function isElAvailable(element) {
  if (!(element instanceof HTMLElement) || !element.isConnected) return false
  const rect = element.getBoundingClientRect()
  return rect.width > 0 && rect.height > 0 &&
    !element.matches(':disabled,.is-disabled,[aria-disabled="true"]') &&
    !element.closest('[hidden],[aria-hidden="true"]')
}

export function isLiveComponent(item) {
  return item?.isMounted === true && item.isUnmounted === false && item.isDeactivated === false
}

export function collectLiveVNodes(owner, maxNodes = 65536) {
  if (!owner) return []
  const stack = [owner.subTree], seen = new Set(), nodes = []
  while (stack.length && seen.size < maxNodes) {
    const node = stack.pop()
    if (!node || seen.has(node)) continue
    seen.add(node)
    nodes.push(node)
    const c = node.component
    if (c && isLiveComponent(c) && c.appContext === owner.appContext) stack.push(c.subTree)
    if (Array.isArray(node.children)) stack.push(...node.children)
    if (node.suspense?.activeBranch) stack.push(node.suspense.activeBranch)
  }
  return stack.length ? [] : nodes
}

/**
 * Bind a mounted Element Plus component instance to its DOM root within an active
 * Vue page component. Scans up to 65,536 nodes for dialogs or 8,192 on-page.
 */
export function findOwnedElComponent({owner, root, componentName, allowDialog = false}) {
  if (!owner || !root) return undefined
  const stack = [owner.subTree], seen = new Set()
  let instance
  const owned = []
  const maxNodes = allowDialog ? 65536 : 8192
  while (stack.length && seen.size < maxNodes) {
    const node = stack.pop()
    if (!node || seen.has(node)) continue
    if (!allowDialog && node.el instanceof HTMLElement && node.el !== root && !node.el.contains(root) &&
        (componentName !== 'ElForm' || !node.el.matches('form.el-form') && !node.el.querySelector('form.el-form'))) continue
    seen.add(node)
    if (node.component && isLiveComponent(node.component)) {
      if ((node.component.type?.name || node.component.type?.__name) === componentName &&
          node.component.appContext === owner.appContext) {
        if (node.component.vnode?.ctx === owner && !node.component.subTree?.el?.closest('[role="dialog"],dialog')) {
          owned.push(node.component)
        }
        if (node.component.subTree?.el === root) {
          if (instance && instance !== node.component) return undefined
          instance = node.component
        }
      }
      // Other top-level forms must still be counted for query ownership. Once a form
      // has been counted, its input/option trees cannot contain another root form unless
      // the DOM actually contains one; avoid scanning thousands of unrelated options.
      if (componentName === 'ElForm' && (node.component.type?.name || node.component.type?.__name) === 'ElForm' &&
          node.component.subTree?.el instanceof HTMLElement &&
          node.component.subTree.el.matches('form.el-form') &&
          !node.component.subTree.el.querySelector('form.el-form')) continue
      stack.push(node.component.subTree)
    }
    if (Array.isArray(node.children)) stack.push(...node.children)
  }
  return stack.length || !instance ? undefined : {owner, instance, owned}
}

export function findAncestorElComponent(instance, componentName, {ownerAppContext, maxDepth = 12, stopAt} = {}) {
  let parent = instance
  for (let depth = 0; parent && depth < maxDepth; depth++, parent = parent.parent) {
    if (ownerAppContext && parent.appContext !== ownerAppContext) break
    if (!isLiveComponent(parent)) break
    const name = parent.type?.name || parent.type?.__name
    if (stopAt && name === stopAt) break
    if (name === componentName) return parent
  }
  return undefined
}
