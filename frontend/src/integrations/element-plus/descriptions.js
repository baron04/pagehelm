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
  const pushPair = (label, value) => {
    if (!label || !value || label.length > 60 || value.length > 200 || SENSITIVE_LABEL.test(label) || /[\r\n]/.test(value)) {
      return
    }
    const key = `${label}\0${value}`
    if (!seen.has(key)) {
      seen.add(key)
      pairs.push([label, value])
    }
  }
  for (const block of descriptionsFromContainer(root)) {
    if (pairs.length >= maxPairs) break
    const rows = Array.from(block.querySelectorAll('tr'))
    if (rows.length) {
      for (let r = 0; r < rows.length && pairs.length < maxPairs; r++) {
        const cells = Array.from(rows[r].children || [])
        if (r + 1 < rows.length && cells.length &&
            cells.every(c => c.matches('.el-descriptions__label') && !c.matches('.el-descriptions__content'))) {
          const nextCells = Array.from(rows[r + 1].children || [])
          if (nextCells.length &&
              nextCells.every(c => c.matches('.el-descriptions__content') && !c.matches('.el-descriptions__label'))) {
            for (let k = 0; k < Math.min(cells.length, nextCells.length) && pairs.length < maxPairs; k++) {
              pushPair(cleanText(cells[k]), cleanText(nextCells[k]))
            }
            r += 1
            continue
          }
        }
        for (let i = 0; i < cells.length && pairs.length < maxPairs; i++) {
          const cell = cells[i]
          if (cell.matches('.el-descriptions__label') && i + 1 < cells.length &&
              cells[i + 1].matches('.el-descriptions__content')) {
            pushPair(cleanText(cell), cleanText(cells[i + 1]))
            i += 1
          } else if (cell.matches('.el-descriptions__cell')) {
            pushPair(cleanText(cell.querySelector('.el-descriptions__label')), cleanText(cell.querySelector('.el-descriptions__content')))
          }
        }
      }
    }
    for (const cell of Array.from(block.querySelectorAll('.el-descriptions__cell'))) {
      if (pairs.length >= maxPairs) break
      if (cell.closest('tr')) continue
      pushPair(cleanText(cell.querySelector('.el-descriptions__label')), cleanText(cell.querySelector('.el-descriptions__content')))
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
