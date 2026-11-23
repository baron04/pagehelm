/** Generic Element Plus date-editor / range-picker detection and binding helpers. */
import {collectLiveVNodes, findAncestorElComponent, isElAvailable} from './vue.js'

export function isElDateRangeInput(element) {
  return element instanceof HTMLInputElement && element.matches('.el-date-editor--daterange > .el-range-input')
}

export function isElDateOrDatetimeRangeInput(element) {
  return element instanceof HTMLInputElement &&
    element.matches('.el-date-editor--daterange > .el-range-input,.el-date-editor--datetimerange > .el-range-input')
}

export function isElMonthRangeInput(element) {
  return element instanceof HTMLInputElement && element.matches('.el-date-editor--monthrange > .el-range-input')
}

export function isElRangePickerInput(element) {
  return isElDateOrDatetimeRangeInput(element) || isElMonthRangeInput(element)
}

function rangePopupDialog(element) {
  const id = element?.getAttribute?.('aria-controls')
  if (!id || !/^[A-Za-z][A-Za-z0-9_:.-]{0,199}$/.test(id) || element.getAttribute('aria-haspopup') !== 'dialog') return null
  const popups = element.ownerDocument.querySelectorAll(`[id="${id}"]`)
  if (popups.length !== 1 || popups[0].getAttribute('role') !== 'dialog') return null
  return popups[0]
}

export function isElRangePopupOpen(element) {
  const popup = rangePopupDialog(element)
  if (!popup || !popup.isConnected || popup.closest('[hidden],[aria-hidden="true"]')) return false
  const box = popup.getBoundingClientRect()
  return Boolean(box.width && box.height)
}

/** Read precision from a mounted `ElDatePicker`, never from placeholders or CSS classes. */
export function readElDatePickerPrecision(element, form, findComponent, expectedOwner) {
  const dateRoot = element instanceof HTMLInputElement && element.closest('.el-date-editor')
  const input = dateRoot && form.contains(dateRoot) && findComponent(dateRoot, 'ElInput', true)
  const picker = input?.owner === expectedOwner
    ? findAncestorElComponent(input.instance.parent, 'ElDatePicker', {
      ownerAppContext: expectedOwner.appContext, maxDepth: 12, stopAt: 'ElForm',
    })
    : undefined
  const type = picker?.props?.type
  return picker && ['date', 'datetime'].includes(type) && !picker.props.format
    ? {inputKind: type} : {}
}

/** Locate an exact enabled native calendar day without touching Vue state. */
export function nativeRangeDateCell(element, value) {
  const parts = /^(\d{4}-\d{2}-\d{2})(?: \d{2}:\d{2}:\d{2})?$/.exec(value || '')
  if (!isElDateOrDatetimeRangeInput(element) || element.disabled || element.readOnly || !parts) return null
  const datePart = parts[1]
  const popup = rangePopupDialog(element)
  if (!popup) return null
  const [year, month, day] = datePart.split('-').map(Number)
  if (new Date(Date.UTC(year, month - 1, day)).toISOString().slice(0, 10) !== datePart) return null
  const matches = [...popup.querySelectorAll('.el-date-range-picker__content')].flatMap(panel => {
    const labels = [...panel.querySelectorAll('.el-date-range-picker__header [role="button"],.el-date-range-picker__header-label')]
      .map(e => (e.textContent || '').replace(/\s/g, ''))
    if (!labels.includes(`${year}年`) || !labels.includes(`${month}月`)) return []
    return [...panel.querySelectorAll('table[role="grid"] td.available')].filter(cell =>
      cell.getAttribute('aria-disabled') !== 'true' && !cell.classList.contains('disabled') &&
      cell.querySelector('.el-date-table-cell__text')?.textContent?.trim() === String(day))
  })
  return matches.length === 1 ? matches[0] : null
}

/** One bounded native calendar step toward a date outside the displayed months. */
export function nativeRangeDateStep(element, value) {
  const parts = /^(\d{4}-\d{2}-\d{2})(?: \d{2}:\d{2}:\d{2})?$/.exec(value || '')
  if (!isElDateOrDatetimeRangeInput(element) || element.disabled || element.readOnly || !parts) return null
  const datePart = parts[1]
  const [year, monthNumber, day] = datePart.split('-').map(Number)
  if (new Date(Date.UTC(year, monthNumber - 1, day)).toISOString().slice(0, 10) !== datePart) return null
  const popup = rangePopupDialog(element)
  if (!popup) return null
  const left = popup.querySelector('.el-date-range-picker__content.is-left')
  const right = popup.querySelector('.el-date-range-picker__content.is-right')
  if (!left || !right) return null
  const month = panel => {
    const labels = [...panel.querySelectorAll('.el-date-range-picker__header [role="button"],.el-date-range-picker__header-label')]
      .map(e => (e.textContent || '').replace(/\s/g, ''))
    const yearMatch = labels.map(t => /^(\d{4})年$/.exec(t)).find(Boolean)
    const number = labels.map(t => /^(\d{1,2})月$/.exec(t)).find(Boolean)
    if (!yearMatch || !number || +number[1] < 1 || +number[1] > 12) return null
    return +yearMatch[1] * 12 + +number[1] - 1
  }
  const first = month(left), last = month(right)
  if (first === null || last !== first + 1) return null
  const target = +datePart.slice(0, 4) * 12 + +datePart.slice(5, 7) - 1
  let button = null
  if (target < first) {
    button = (first - target >= 12 && left.querySelector('button.d-arrow-left')) ||
      left.querySelector('button.arrow-left')
  } else if (target > last) {
    button = (target - last >= 12 && right.querySelector('button.d-arrow-right')) ||
      right.querySelector('button.arrow-right')
  }
  return button instanceof HTMLButtonElement && !button.disabled && button.isConnected ? button : null
}

/** Locate an exact enabled native calendar month cell for `.el-date-editor--monthrange`. */
export function nativeMonthRangeCell(element, value) {
  const parts = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(value || '')
  if (!isElMonthRangeInput(element) || element.disabled || element.readOnly || !parts) return null
  const popup = rangePopupDialog(element)
  if (!popup) return null
  const year = parts[1], month = Number(parts[2])
  const matches = [...popup.querySelectorAll('.el-date-range-picker__content')].flatMap(panel => {
    const headerText = (panel.querySelector('.el-date-range-picker__header')?.textContent || '').replace(/\s+/g, '')
    if (!headerText.includes(`${year}年`)) return []
    return [...panel.querySelectorAll('table.el-month-table td')].filter(cell =>
      !cell.classList.contains('disabled') && cell.getAttribute('aria-disabled') !== 'true' &&
      (cell.getAttribute('aria-label') || '').replace(/\s+/g, '') === `${month}月`)
  })
  return matches.length === 1 ? matches[0] : null
}

/** One bounded native year step for `.el-date-editor--monthrange`. */
export function nativeMonthRangeStep(element, value) {
  const parts = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(value || '')
  if (!isElMonthRangeInput(element) || element.disabled || element.readOnly || !parts) return null
  const popup = rangePopupDialog(element)
  if (!popup) return null
  const left = popup.querySelector('.el-date-range-picker__content.is-left')
  const right = popup.querySelector('.el-date-range-picker__content.is-right')
  if (!left || !right) return null
  const yearOf = panel => {
    const m = /(\d{4})年/.exec((panel.querySelector('.el-date-range-picker__header')?.textContent || '').replace(/\s+/g, ''))
    return m ? Number(m[1]) : null
  }
  const first = yearOf(left), last = yearOf(right), target = Number(parts[1])
  if (first === null || last === null) return null
  const button = target < first
    ? left.querySelector('button.d-arrow-left')
    : target > last ? right.querySelector('button.d-arrow-right') : null
  return button instanceof HTMLButtonElement && !button.disabled && button.isConnected ? button : null
}

/** Bind a mounted `ElDatePicker` range input through its real component callbacks. */
export async function bindElDateRangeInput(element, text, invoked, {
  findComponent,
  isAllowedControl,
  currentPageOwner,
}) {
  const root = element.closest('.el-date-editor--daterange,.el-date-editor--datetimerange')
  if (!root) return false
  const binding = findComponent(root, 'PickerRangeTrigger')
  const inputs = [...root.querySelectorAll(':scope > .el-range-input')]
  const end = inputs.indexOf(element)
  if (!binding || inputs.length !== 2 || end < 0) return false
  let picker = binding.instance.parent
  for (let depth = 0; picker && depth < 12 && picker.type?.name !== 'ElDatePicker'; depth++) picker = picker.parent
  if (picker?.type?.name !== 'ElDatePicker' || !['daterange', 'datetimerange'].includes(picker.props?.type) ||
      !picker.isMounted || picker.isUnmounted || picker.isDeactivated ||
      typeof picker.exposed?.handleOpen !== 'function') return false
  const datetime = picker.props.type === 'datetimerange'
  const parts = /^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}):(\d{2}):(\d{2}))?$/.exec(text)
  if (!parts) throw new Error('the native date range requires a date or full date and time')
  const [year, month, day] = parts.slice(1, 4).map(Number)
  if (new Date(Date.UTC(year, month - 1, day)).toISOString().slice(0, 10) !== text.slice(0, 10) ||
      parts[4] && (!datetime || Number(parts[4]) > 23 || Number(parts[5]) > 59 || Number(parts[6]) > 59)) {
    throw new Error('the native date range refuses this date or time')
  }
  // Opening and selecting are real component callbacks, not synthetic DOM clicks.
  // The range panel owns formatting and real update/change events. Date-only
  // queries on a datetime range mean inclusive natural days, not native defaults.
  try { invoked() } catch { /* Diagnostics cannot stop the component callback. */ }
  picker.exposed.handleOpen()
  await Promise.resolve()
  // The component's opening watcher saves its pre-edit model on nextTick.
  // Let that native baseline settle before pick closes the range again.
  await new Promise(resolve => window.requestAnimationFrame(resolve))
  const tables = collectLiveVNodes(binding.owner).filter(node => {
    if (node.component?.type?.__name !== 'basic-date-table' || !node.component.isMounted ||
        node.component.isUnmounted || node.component.isDeactivated ||
        node.component.props?.selectionMode !== 'range' || typeof node.props?.onPick !== 'function') return false
    let parent = node.component.parent
    for (let depth = 0; parent && depth < 12; depth++, parent = parent.parent) if (parent === picker) return true
    return false
  })
  const handlers = new Set(tables.map(node => node.props.onPick))
  if (!isElAvailable(element) || element.readOnly || currentPageOwner() !== binding.owner ||
      !isAllowedControl(element) || handlers.size !== 1 || !tables.length) {
    throw new Error('the mounted native date range contract changed after opening')
  }
  const props = tables[0].component.props
  const base = props.date
  if (props.disabled || typeof base?.startOf !== 'function' || typeof base?.toDate !== 'function') {
    throw new Error('the mounted native date range is unavailable')
  }
  let value = base.startOf('day').date(1).year(year).month(month - 1).date(day)
  const time = parts[4] ? parts.slice(4).map(Number) : [end === 1 ? 23 : 0, end === 1 ? 59 : 0, end === 1 ? 59 : 0]
  if (datetime) value = value.hour(time[0]).minute(time[1]).second(time[2])
  if (!value.isValid() || value.format(parts[4] ? 'YYYY-MM-DD HH:mm:ss' : 'YYYY-MM-DD') !== text ||
      props.disabledDate?.(value.toDate())) {
    throw new Error('the native date range refuses this date')
  }
  const selected = {minDate: end === 0 ? value : props.minDate, maxDate: end === 1 ? value : props.maxDate}
  if (!selected.minDate) throw new Error('the native date range has no selected start date')
  if (end === 1 && selected.maxDate.isBefore(selected.minDate)) throw new Error('the native date range ends before it starts')
  // A datetime panel can also have calendar time defaults. Its own time editor
  // callbacks retain the exact query times after calendar selection applies them.
  let timeEditors, panel
  if (datetime && picker.props.defaultTime) {
    panel = tables[0].component.parent
    if (panel?.type?.__name !== 'panel-date-range' ||
        picker.props.format && picker.props.format !== 'YYYY-MM-DD HH:mm:ss' ||
        picker.props.timeFormat && picker.props.timeFormat !== 'HH:mm:ss') {
      throw new Error('the native datetime range time contract is unavailable')
    }
    const wraps = [...panel.subTree.el.querySelectorAll('.el-date-range-picker__time-header .el-date-range-picker__editors-wrap')]
    const nodes = collectLiveVNodes(binding.owner)
    timeEditors = wraps.map(wrap => {
      const rootEl = wrap.querySelectorAll('.el-input')[1]
      const matches = nodes.filter(node => node.el === rootEl && node.component?.type?.name === 'ElInput'
        && node.component.parent === panel && node.component.isMounted &&
        !node.component.isUnmounted && !node.component.isDeactivated)
      return matches.length === 1 ? matches[0] : undefined
    })
    if (timeEditors.length !== 2 || timeEditors.some(editor => typeof editor?.props?.onInput !== 'function' ||
        typeof editor.props?.onChange !== 'function')) {
      throw new Error('the native datetime range time editors are unavailable')
    }
  }
  try { invoked() } catch { /* Diagnostics cannot stop the component callback. */ }
  await tables[0].props.onPick(selected, datetime ? !!timeEditors : true)
  if (timeEditors) {
    for (const [i, date] of [selected.minDate, selected.maxDate].entries()) {
      if (date) {
        if (timeEditors[i].component.props.disabled || timeEditors[i].component.props.readonly ||
            !timeEditors[i].el.isConnected || timeEditors[i].el.closest('[hidden],[aria-hidden="true"]') ||
            currentPageOwner() !== binding.owner) {
          throw new Error('the native datetime range time editor changed after selection')
        }
        try { invoked() } catch { /* Diagnostics cannot stop the component callback. */ }
        await timeEditors[i].component.vnode.props.onInput(date.format('HH:mm:ss'))
      }
    }
    if (selected.maxDate) {
      for (const editor of timeEditors) {
        try { invoked() } catch { /* Diagnostics cannot stop the component callback. */ }
        await editor.component.vnode.props.onChange()
      }
      await Promise.resolve()
      const buttons = [...panel.subTree.el.querySelectorAll('.el-picker-panel__footer button.is-plain')]
      const confirms = collectLiveVNodes(binding.owner).filter(node => node.el === buttons[0] &&
        node.component?.type?.name === 'ElButton' && node.component.parent === panel && node.component.isMounted &&
        !node.component.isUnmounted && !node.component.isDeactivated && !node.component.props.disabled)
      if (buttons.length !== 1 || confirms.length !== 1 || !buttons[0].isConnected ||
          buttons[0].closest('[hidden],[aria-hidden="true"]') ||
          typeof confirms[0].props?.onClick !== 'function' || currentPageOwner() !== binding.owner) {
        throw new Error('the native datetime range confirmation is unavailable')
      }
      try { invoked() } catch { /* Diagnostics cannot stop the component callback. */ }
      await confirms[0].props.onClick()
    }
  }
  await Promise.resolve()
  return true
}
