/** Generic Element Plus `.el-descriptions` key/value reader and annotation helper. */
import {isElVisible} from './vue.js'

const SENSITIVE_LABEL = /(?:pass(?:word)?|pwd|secret|token|credential|auth|cookie|session|api.?key|密码|口令|密钥|令牌)/i
const OVERLAY_SELECTOR = '[role="dialog"],dialog,.el-drawer'

function cleanText(node) {
  return (node?.textContent || '').replace(/\s+/g, ' ').trim()
}

function descriptionsFromContainer(root, {overlaysFirst = true} = {}) {
  const tables = Array.from(root.querySelectorAll('.el-descriptions')).filter(isElVisible)
  if (!overlaysFirst) return tables
  const overlayTables = tables.filter(el => el.closest(OVERLAY_SELECTOR))
  if (overlayTables.length) return overlayTables
  return tables
}

/** Extract bounded `(label, value)` pairs from visible `.el-descriptions` blocks. */
export function collectElDescriptionPairs(root = document, {maxPairs = 40} = {}) {
  if (!root?.querySelectorAll) return []
  const pairs = []
  const seen = new Set()
  for (const block of descriptionsFromContainer(root)) {
    if (pairs.length >= maxPairs) break
    const rows = Array.from(block.querySelectorAll('tr'))
    if (rows.length) {
      for (const tr of rows) {
        if (pairs.length >= maxPairs) break
        const cells = Array.from(tr.children || [])
        for (let i = 0; i < cells.length && pairs.length < maxPairs; i++) {
          const cell = cells[i]
          if (cell.matches('.el-descriptions__label') && i + 1 < cells.length &&
              cells[i + 1].matches('.el-descriptions__content')) {
            const label = cleanText(cell)
            const value = cleanText(cells[i + 1])
            i += 1
            if (label && value && label.length <= 60 && value.length <= 200 &&
                !SENSITIVE_LABEL.test(label) && !/[\r\n]/.test(value)) {
              const key = `${label}\0${value}`
              if (!seen.has(key)) {
                seen.add(key)
                pairs.push([label, value])
              }
            }
          } else if (cell.matches('.el-descriptions__cell')) {
            const label = cleanText(cell.querySelector('.el-descriptions__label'))
            const value = cleanText(cell.querySelector('.el-descriptions__content'))
            if (label && value && label.length <= 60 && value.length <= 200 &&
                !SENSITIVE_LABEL.test(label) && !/[\r\n]/.test(value)) {
              const key = `${label}\0${value}`
              if (!seen.has(key)) {
                seen.add(key)
                pairs.push([label, value])
              }
            }
          }
        }
      }
    }
    for (const cell of Array.from(block.querySelectorAll('.el-descriptions__cell'))) {
      if (pairs.length >= maxPairs) break
      if (cell.closest('tr')) continue
      const label = cleanText(cell.querySelector('.el-descriptions__label'))
      const value = cleanText(cell.querySelector('.el-descriptions__content'))
      if (label && value && label.length <= 60 && value.length <= 200 &&
          !SENSITIVE_LABEL.test(label) && !/[\r\n]/.test(value)) {
        const key = `${label}\0${value}`
        if (!seen.has(key)) {
          seen.add(key)
          pairs.push([label, value])
        }
      }
    }
  }
  return pairs
}

export function annotateElDescriptions(
  html,
  root = document,
  {
    maxPairs = 40,
    formatSummary = pairs => 'Visible descriptions: ' + pairs.map(([k, v]) => `${k}=${v}`).join('; ').slice(0, 2400),
  } = {},
) {
  const pairs = collectElDescriptionPairs(root, {maxPairs})
  if (!pairs.length) return html
  const summary = formatSummary(pairs)
  return summary ? summary + '\n' + html : html
}
