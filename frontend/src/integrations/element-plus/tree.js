/** Generic Element Plus active-dialog `.el-tree` selection reader. */
import {isElVisible} from './vue.js'

const SENSITIVE_LABEL = /(?:pass(?:word)?|pwd|secret|token|credential|auth|cookie|session|api.?key|密码|口令|密钥|令牌)/i
const OVERLAY_SELECTOR = '[role="dialog"],dialog,.el-drawer'

function labelFromNodeData(data, labelKey = 'name') {
  if (!data || typeof data !== 'object') return ''
  for (const candidate of [data[labelKey], data.label, data.name, data.title]) {
    if (typeof candidate === 'string' && candidate.trim()) {
      return candidate.replace(/\s+/g, ' ').trim()
    }
  }
  return ''
}

function dedupeBounded(items, maxItems) {
  const out = []
  const seen = new Set()
  for (const item of items) {
    if (typeof item !== 'string') continue
    const clean = item.replace(/\s+/g, ' ').trim()
    if (!clean || clean.length > 60 || SENSITIVE_LABEL.test(clean) || /[\r\n]/.test(clean) || seen.has(clean)) {
      continue
    }
    seen.add(clean)
    out.push(clean)
    if (out.length >= maxItems) break
  }
  return out
}

function resolveElTreeInstance(treeRoot, findComponent) {
  const bound = typeof findComponent === 'function' ? findComponent(treeRoot, 'ElTree', true) : undefined
  if (bound?.instance) return bound.instance
  const direct = treeRoot?.__vueParentComponent
  const directName = direct?.type?.name || direct?.type?.__name
  return directName === 'ElTree' ? direct : undefined
}

function readTreeInstanceSelections(instance) {
  const labelKey = instance?.props?.props?.label || 'name'
  const api = instance?.exposed || instance?.proxy
  const rawChecked = typeof api?.getCheckedNodes === 'function' ? api.getCheckedNodes(false, false) : undefined
  const rawHalf = typeof api?.getHalfCheckedNodes === 'function' ? api.getHalfCheckedNodes() : undefined
  if (Array.isArray(rawChecked)) {
    return {
      checkedAll: rawChecked.map(node => labelFromNodeData(node, labelKey)).filter(Boolean),
      halfAll: Array.isArray(rawHalf)
        ? rawHalf.map(node => labelFromNodeData(node, labelKey)).filter(Boolean)
        : [],
    }
  }
  const nodesMap = instance?.store?.nodesMap || instance?.proxy?.store?.nodesMap || instance?.setupState?.store?.nodesMap
  if (nodesMap && typeof nodesMap === 'object') {
    const entries = nodesMap instanceof Map ? Array.from(nodesMap.values()) : Object.values(nodesMap)
    const checkedAll = []
    const halfAll = []
    for (const node of entries) {
      const label = (typeof node?.label === 'string' && node.label.trim())
        ? node.label.replace(/\s+/g, ' ').trim()
        : labelFromNodeData(node?.data, labelKey)
      if (!label) continue
      if (node?.checked) checkedAll.push(label)
      else if (node?.indeterminate) halfAll.push(label)
    }
    return {checkedAll, halfAll}
  }
  return null
}

function readTreeDomSelections(treeRoot) {
  const checkedAll = []
  const halfAll = []
  for (const nodeEl of Array.from(treeRoot.querySelectorAll('.el-tree-node'))) {
    const content = nodeEl.querySelector(':scope > .el-tree-node__content') || nodeEl
    const box = content.querySelector('.el-checkbox')
    const label = (content.querySelector('.el-tree-node__label')?.textContent || '').replace(/\s+/g, ' ').trim()
    if (!box || !label) continue
    if (box.classList.contains('is-checked')) checkedAll.push(label)
    else if (box.classList.contains('is-indeterminate')) halfAll.push(label)
  }
  return {checkedAll, halfAll}
}

/** Collect checked and half-checked labels from checkbox `.el-tree` widgets inside visible overlays. */
export function collectActiveDialogElTreeSelections(
  root = document,
  {findComponent, maxChecked = 80, maxHalfChecked = 40} = {},
) {
  if (!root?.querySelectorAll) return []
  const trees = []
  for (const overlay of Array.from(root.querySelectorAll(OVERLAY_SELECTOR))) {
    if (!isElVisible(overlay)) continue
    for (const treeRoot of Array.from(overlay.querySelectorAll('.el-tree'))) {
      if (!isElVisible(treeRoot)) continue
      const instance = resolveElTreeInstance(treeRoot, findComponent)
      const isCheckboxTree = Boolean(treeRoot.querySelector('.el-checkbox') || instance?.props?.showCheckbox === true)
      if (!isCheckboxTree) continue
      const fromInstance = instance ? readTreeInstanceSelections(instance) : null
      const raw = fromInstance || readTreeDomSelections(treeRoot)
      const checked = dedupeBounded(raw.checkedAll, maxChecked)
      const halfChecked = dedupeBounded(raw.halfAll, maxHalfChecked)
      if (!checked.length && !halfChecked.length) continue
      const label = (
        treeRoot.closest('.el-form-item')?.querySelector('.el-form-item__label')?.textContent
        || overlay.querySelector('.el-dialog__title,.el-drawer__title,.el-dialog__header')?.textContent
        || ''
      ).replace(/\s+/g, ' ').trim()
      trees.push({
        label,
        checked,
        checkedCount: raw.checkedAll.length,
        halfChecked,
        halfCheckedCount: raw.halfAll.length,
      })
    }
  }
  return trees
}

export function annotateActiveDialogElTrees(
  html,
  root = document,
  {
    findComponent,
    maxChecked = 80,
    maxHalfChecked = 40,
    formatTree = tree => {
      const prefix = tree.label ? `${tree.label} ` : ''
      const parts = []
      if (tree.checked.length) {
        const suffix = tree.checkedCount > tree.checked.length ? ` (showing ${tree.checked.length}/${tree.checkedCount})` : ''
        parts.push(`${prefix}tree checked (${tree.checkedCount}): ${tree.checked.join(', ')}${suffix}`)
      }
      if (tree.halfChecked.length) {
        parts.push(`${prefix}tree half-checked (${tree.halfCheckedCount}): ${tree.halfChecked.join(', ')}`)
      }
      return parts.join(' | ')
    },
  } = {},
) {
  const trees = collectActiveDialogElTreeSelections(root, {findComponent, maxChecked, maxHalfChecked})
  if (!trees.length) return html
  const lines = trees.map(formatTree).filter(Boolean).map(line => line.slice(0, 2400))
  return lines.length ? lines.join('\n') + '\n' + html : html
}
