/** Generic Element Plus table (`.el-table`), row binding, switch toggle, and line-control helpers. */
import {isElAvailable, isElInViewport, isElVisible} from './vue.js'

const SENSITIVE_NAME = /password|token|secret|credential|api.?key|密码|密钥|令牌/i

const attribute = value => /[\s"'<>=]/.test(value) ? `"${value.replace(/"/g, "'")}"` : value

/** Collect `data` prop arrays from visible on-page `.el-table` instances outside dialogs. */
export function collectVisibleElTableData(doc, findComponent, visibleFn = isElVisible) {
  const tables = []
  for (const root of Array.from(doc.querySelectorAll('.el-table'))) {
    if (!visibleFn(root) || root.closest('[role="dialog"],dialog')) continue
    if (root.parentElement?.closest('.el-table')) continue
    const data = findComponent(root, 'ElTable')?.instance?.props?.data
    if (Array.isArray(data)) tables.push(data)
  }
  return tables
}

/** Read a row from the same mounted `.el-table` that renders it. A row index alone
 * never survives sorting, filtering, replacement or a credential/page change. */
export function boundElTableRecord(element, findComponent) {
  const row = element?.closest?.('.el-table__body-wrapper tbody > tr')
  const table = row?.closest('.el-table')
  if (!row || !isElAvailable(element) || !isElAvailable(row) || table.closest('form,[role="dialog"],dialog')) return undefined
  const binding = findComponent(table, 'ElTable')
  // Vue may have queued new rows while callbacks still close over the old row.
  if (!binding || binding.owner.next || binding.instance.next ||
      !Number.isInteger(binding.owner.job?.flags) || !Number.isInteger(binding.instance.job?.flags) ||
      binding.owner.job?.flags & 1 || binding.instance.job?.flags & 1) return undefined
  // The body renders the store's filtered/sorted rows, not the input props.
  // Require the mounted body and the exact store it was rendered with.
  const bodies = [...table.querySelectorAll('.el-table__body-wrapper tbody')]
    .filter(item => item.closest('.el-table') === table)
  const body = bodies.length === 1 && findComponent(bodies[0], 'ElTableBody')
  if (!body || body.owner !== binding.owner || body.instance.next ||
      !Number.isInteger(body.instance.job?.flags) || body.instance.job.flags & 1) return undefined
  const store = body.instance.props?.store
  const stores = [binding.instance.store, binding.instance.proxy?.store, binding.instance.setupState?.store].filter(Boolean)
  if (!store || stores.some(item => item !== store)) return undefined
  const data = store.states?.data?.value
  const rows = [...table.querySelectorAll('.el-table__body-wrapper tbody > tr')]
    .filter(item => item.closest('.el-table') === table)
  const index = rows.indexOf(row)
  // Tree/expanded/virtual tables need their own component contract. Never align
  // a DOM row against a differently shaped data array.
  if (!Array.isArray(data) || rows.length !== data.length || index < 0 ||
      rows.some(item => item.matches('.el-table__row[class*="el-table__row--level-"]') ||
        item.querySelector(':scope > .el-table__expanded-cell')) ||
      !Number.isSafeInteger(data[index]?.id) || data[index].id < 1 ||
      data.filter(item => item?.id === data[index].id).length !== 1) return undefined
  const heads = [...table.querySelectorAll('.el-table__header-wrapper thead tr:last-child th')]
  const cells = [...row.children]
  if (heads.length !== cells.length || heads.length > 80) return undefined
  const text = item => (item.textContent || '').replace(/\s+/g, ' ').trim()
  const scalars = Object.values(data[index]).filter(value => ['string', 'number'].includes(typeof value)).map(String)
  const values = heads.flatMap((head, i) => {
    const label = text(head), value = text(cells[i])
    return label && label.length <= 80 && value.length <= 300 &&
      !SENSITIVE_NAME.test(label) &&
      !cells[i].querySelector('button,input,textarea,select') &&
      (!cells[i].querySelector('a') || scalars.includes(value)) ? [[label, value]] : []
  })
  return {id: String(data[index].id), table: String(binding.instance.uid), values}
}

export function readElTableRowToggle(element, row, findComponent) {
  const area = element.closest('.el-switch'), tableRoot = area?.closest('.el-table')
  const binding = area && findComponent(area, 'ElSwitch')
  const table = tableRoot && findComponent(tableRoot, 'ElTable')
  if (!binding || !table || binding.owner !== table.owner || binding.instance.props?.disabled) return undefined
  const cell = area.closest('td')
  const store = table.instance.store || table.instance.proxy?.store || table.instance.setupState?.store
  const column = store?.states?.columns?.value?.find(c => cell?.classList.contains(c.id))
  const field = column?.property
  const records = store?.states?.data?.value
  const record = Array.isArray(records) && records.find(r => String(r.id) === row.id)
  const props = binding.instance.props
  const scalar = value => ['string', 'boolean', 'number'].includes(typeof value) &&
    (typeof value !== 'number' || Number.isFinite(value))
  if (typeof field !== 'string' || !/^[a-z][A-Za-z0-9]{0,59}$/.test(field) || !record ||
      !scalar(props.activeValue) || !scalar(props.inactiveValue) || props.activeValue === props.inactiveValue ||
      props.modelValue !== record[field] || ![props.activeValue, props.inactiveValue].includes(record[field])) return undefined
  return {
    field,
    before: record[field],
    after: record[field] === props.activeValue ? props.inactiveValue : props.activeValue,
    label: String(column.label || field).slice(0, 80),
    checked: record[field] === props.activeValue,
  }
}

/**
 * The controls of an editable `.el-table` inside an `.el-form`, each with the row and
 * column it belongs to.
 */
export function collectElFormLineControls(root = document) {
  const text = element => (element.textContent || '').replace(/\s+/g, ' ').trim()
  const found = []
  for (const table of root.querySelectorAll('.el-form .el-table')) {
    const heads = [...table.querySelectorAll('.el-table__header-wrapper thead tr:last-child th')].map(text)
    const rows = [...table.querySelectorAll('.el-table__body-wrapper tbody > tr')]
    rows.forEach((row, at) => {
      for (const cell of row.children) {
        const column = cell instanceof HTMLTableCellElement ? heads[cell.cellIndex] : ''
        if (!column) continue
        for (const control of cell.querySelectorAll('input:not([type="hidden"]):not([type="file"]),textarea')) {
          if (control.disabled || control.closest('.is-disabled')) continue
          const wrapper = control.closest('.el-select__wrapper')
          const area = wrapper && cell.contains(wrapper) ? wrapper : control
          if (!isElInViewport(area)) continue
          const hint = wrapper && wrapper.querySelector('.el-select__placeholder')
          found.push({area, control, row: at + 1, column,
            select: Boolean(wrapper) && control.getAttribute('role') === 'combobox',
            ...(hint ? {[hint.classList.contains('is-transparent') ? 'placeholder' : 'value']: text(hint)} : {})})
        }
      }
    })
  }
  return found
}

/**
 * Annotate line-table controls in `html` and index unindexed `.el-select__wrapper` controls
 * into `selectorMap`.
 */
export function annotateElLineControls(
  selectorMap,
  html,
  lines,
  formatLabel = line => `Line ${line.row} · ${line.column}`,
) {
  if (!lines.length) return html
  const texts = html.split('\n'), indexOf = new Map()
  for (const [index, node] of selectorMap) indexOf.set(node.ref, index)
  const at = index => texts.findIndex(line => new RegExp(`^\\s*\\*?\\[${index}\\]<`).test(line))
  let top = Math.max(-1, ...selectorMap.keys())
  const anchors = new Map(), missing = []
  for (const line of lines) {
    const label = attribute(formatLabel(line))
    const index = indexOf.get(line.control) ?? indexOf.get(line.area)
    if (index === undefined) { missing.push([line, label]); continue }
    const position = at(index)
    if (position < 0) continue
    if (!anchors.has(line.row)) anchors.set(line.row, index)
    // Only a record without text of its own: `[n]<input ... />`.
    const record = /^(\s*\*?\[\d+\]<\w+)([^>]*?)\s*\/>$/.exec(texts[position])
    if (record && !/\stitle=/.test(record[2])) texts[position] = `${record[1]}${record[2]} title=${label} />`
  }
  for (const [line, label] of missing) {
    const index = ++top
    selectorMap.set(index, {ref: line.area, tagName: 'input', isInteractive: true, highlightIndex: index})
    const anchor = anchors.has(line.row) ? at(anchors.get(line.row)) : -1
    const indent = anchor >= 0 ? /^\s*/.exec(texts[anchor])[0] : ''
    const said = line.value ? ` value=${attribute(line.value)}` : line.placeholder ? ` placeholder=${attribute(line.placeholder)}` : ''
    const record = `${indent}[${index}]<input${line.select ? ' role=combobox aria-haspopup=listbox' : ''} title=${label}${said} />`
    if (anchor >= 0) texts.splice(anchor, 0, record)
    else texts.push(record)
  }
  return texts.join('\n')
}

/** Bind visible `.el-table` cells to their own observed row and column. */
export function annotateObservedElTableRows(
  html,
  root = document,
  selectorMap = new Map(),
  {formatRow = (rowIndex, columns) => `Row ${rowIndex + 1}: ${columns.join('; ')}`,
    formatSummary = facts => 'Visible table rows: ' + facts.join(' | ').slice(0, 3000)} = {},
) {
  if ([...root.querySelectorAll('.el-dialog')].some(isElInViewport)) return html
  const text = element => (element.textContent || '').replace(/\s+/g, ' ').trim()
  const safeValue = value => value.length > 0 && value.length <= 80 && !/[\n\r]/.test(value)
  const facts = []
  for (const table of root.querySelectorAll('.el-table')) {
    if (table.closest('.el-form') || !isElInViewport(table) || facts.length >= 20) continue
    const heads = [...table.querySelectorAll('.el-table__header-wrapper thead tr:last-child th')]
    const rows = [...table.querySelectorAll('.el-table__body-wrapper tbody > tr')].filter(isElInViewport).slice(0, 20)
    if (!rows.length || heads.length > 40) continue
    // Find the controller record emitted by the closest indexed wrapper inside this
    // table. A number in another component, even an identical one, cannot prove this
    // row's cell was in the page observation.
    const wrappers = [...selectorMap].filter(([, node]) =>
      node.ref instanceof Element && table.contains(node.ref) && rows.every(row => node.ref.contains(row)))
      .sort((a, b) => a[1].ref === b[1].ref ? 0 : a[1].ref.contains(b[1].ref) ? 1 : -1)
    let observed = ''
    for (const [index, node] of wrappers) {
      const marker = new RegExp(`^\\s*\\*?\\[${index}\\]<${node.tagName || node.ref.tagName.toLowerCase()}\\b`, 'm')
      const start = marker.exec(html)
      if (!start) continue
      const end = html.indexOf(' />', start.index)
      if (end < 0 || end - start.index > 8192) continue
      observed = html.slice(start.index, end + 3)
      break
    }
    const body = observed.slice(observed.indexOf('>') + 1, -3)
    const identities = rows.map(row => [...row.children].map(text).find(value =>
      value.length >= 2 && value.length <= 80 && /[^\d.,-]/.test(value)))
    if (identities.some(identity => !identity) || new Set(identities).size !== rows.length) continue
    const positions = identities.map(identity => body.indexOf(identity))
    if (positions.some((position, index) => position < 0 || body.indexOf(identities[index], position + 1) >= 0 ||
        index > 0 && position <= positions[index - 1])) continue
    for (let rowIndex = 0; rowIndex < rows.length && facts.length < 20; rowIndex++) {
      const cells = [...rows[rowIndex].children]
      if (cells.length !== heads.length) continue
      const segment = body.slice(positions[rowIndex], positions[rowIndex + 1] ?? body.length)
      const lines = segment.split('\n').map(value => value.trim())
      const cellCounts = new Map()
      for (const value of cells.map(text).filter(safeValue)) {
        cellCounts.set(value, (cellCounts.get(value) || 0) + 1)
      }
      const observedCounts = new Map()
      for (const value of lines.filter(safeValue)) {
        observedCounts.set(value, (observedCounts.get(value) || 0) + 1)
      }
      const columns = []
      for (let i = 0; i < cells.length && columns.length < 20; i++) {
        const label = text(heads[i]), value = text(cells[i])
        if (label && label.length <= 40 && !SENSITIVE_NAME.test(label) && safeValue(value) &&
            observedCounts.get(value) >= cellCounts.get(value)) columns.push(`${label}=${value}`)
      }
      if (columns.length) facts.push(formatRow(rowIndex, columns))
    }
  }
  return facts.length ? formatSummary(facts) + '\n' + html : html
}
