import {afterEach, describe, expect, it, vi} from 'vitest'

vi.mock('@page-agent/page-controller', () => ({
  PageController: class PageController extends EventTarget {
    selectorMap = new Map()
    elementTextMap = new Map()
    simplifiedHTML = ''
    dispose() {}
    async updateTree() { return '' }
    async inputText() { return {success: true, message: 'generic'} }
  },
  getElementByIndex(selectorMap: Map<number, {ref: HTMLElement}>, index: number) {
    return selectorMap.get(index)?.ref
  },
  async scrollIntoViewIfNeeded() {},
}))

// @ts-expect-error Application module is served separately from the generic Runtime.
import {PageController as PageControllerCtor} from '../src/integrations/yudao/yudao-page-controller.js'

const cleanups: Array<() => void> = []

function rect(width = 100, height = 24): DOMRect {
  return {width, height, top: 10, bottom: 10 + height, left: 0, right: width, x: 0, y: 10, toJSON: () => ({})} as DOMRect
}

function buildRangeDom(doc: Document, {popupInitiallyOpen = true, initialEndTime = '00:00:00'} = {}) {
  const host = doc.createElement('div')
  host.innerHTML = `
    <div class="el-date-editor el-date-editor--datetimerange">
      <input class="el-range-input" aria-haspopup="dialog" aria-controls="dt-pop" value="">
      <input class="el-range-input" aria-haspopup="dialog" aria-controls="dt-pop" value="">
    </div>
    <div id="dt-pop" role="dialog"${popupInitiallyOpen ? '' : ' aria-hidden="true"'}>
      <div class="el-date-range-picker__time-header">
        <span class="el-date-range-picker__editors-wrap">
          <input value="2026-10-01">
          <input value="00:00:00">
        </span>
        <span class="el-date-range-picker__editors-wrap">
          <input value="2026-10-31">
          <input value="${initialEndTime}">
        </span>
      </div>
      <div class="el-date-range-picker__content is-left">
        <div class="el-date-range-picker__header">
          <div class="el-date-range-picker__header-label">2026年</div>
          <div class="el-date-range-picker__header-label">10月</div>
        </div>
        <table role="grid"><tbody><tr>
          <td class="available"><span class="el-date-table-cell__text">1</span></td>
          <td class="available"><span class="el-date-table-cell__text">31</span></td>
        </tr></tbody></table>
      </div>
      <div class="el-picker-panel__footer">
        <button type="button" class="el-button is-text">清空</button>
        <button type="button" class="el-button is-plain">确定</button>
      </div>
    </div>
  `
  doc.body.append(host)
  const popup = host.querySelector('#dt-pop') as HTMLElement
  for (const el of host.querySelectorAll('*')) {
    ;(el as HTMLElement).getBoundingClientRect = () =>
      el === popup && popup.getAttribute('aria-hidden') === 'true' ? rect(0, 0) : rect(100, 24)
  }
  const [startInput, endInput] = [...host.querySelectorAll('.el-range-input')] as HTMLInputElement[]
  startInput!.addEventListener('click', () => popup.removeAttribute('aria-hidden'))
  const wraps = [...host.querySelectorAll('.el-date-range-picker__editors-wrap')]
  const startTimeInput = wraps[0]!.querySelectorAll('input')[1] as HTMLInputElement
  const endTimeInput = wraps[1]!.querySelectorAll('input')[1] as HTMLInputElement
  const clearBtn = host.querySelector('button.is-text') as HTMLButtonElement
  const confirmBtn = host.querySelector('button.is-plain:not(.is-text)') as HTMLButtonElement
  const [day1, day31] = [...host.querySelectorAll('td.available')] as HTMLElement[]
  return {host, popup, startInput: startInput!, endInput: endInput!, startTimeInput, endTimeInput, clearBtn, confirmBtn, day1: day1!, day31: day31!}
}

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  document.body.innerHTML = ''
  document.head.innerHTML = ''
  if ('adoptedStyleSheets' in document && Array.isArray(document.adoptedStyleSheets)) {
    document.adoptedStyleSheets = []
  }
  vi.restoreAllMocks()
})

describe('yudao PageController datetimerange isolated ownerDocument behavior', () => {
  it('sets end time to 23:59:59 and auto-confirms on date-only end without touching global document', async () => {
    const decoy = buildRangeDom(document, {popupInitiallyOpen: true, initialEndTime: '00:00:00'})
    const decoyEndEvents: string[] = []
    let decoyConfirmed = 0
    decoy.endTimeInput.addEventListener('input', () => decoyEndEvents.push('input'))
    decoy.endTimeInput.addEventListener('change', () => decoyEndEvents.push('change'))
    decoy.confirmBtn.addEventListener('click', () => { decoyConfirmed += 1 })

    const isolatedDoc = document.implementation.createHTMLDocument('isolated')
    const iso = buildRangeDom(isolatedDoc, {popupInitiallyOpen: false, initialEndTime: '00:00:00'})
    const startTimeEvents: string[] = []
    const endTimeEvents: string[] = []
    const clickedDays: string[] = []
    let clearClicks = 0
    let confirmClicks = 0
    iso.startTimeInput.addEventListener('input', () => startTimeEvents.push('input'))
    iso.endTimeInput.addEventListener('input', () => endTimeEvents.push('input'))
    iso.endTimeInput.addEventListener('change', () => endTimeEvents.push('change'))
    iso.day1.addEventListener('click', () => clickedDays.push('1'))
    iso.day31.addEventListener('click', () => clickedDays.push('31'))
    iso.clearBtn.addEventListener('click', () => { clearClicks += 1 })
    iso.confirmBtn.addEventListener('click', () => { confirmClicks += 1 })
    const endBlurSpy = vi.spyOn(iso.endInput, 'blur')

    const controller = new PageControllerCtor({enableMask: false})
    cleanups.push(() => controller.dispose())
    controller.selectorMap.set(0, {ref: iso.startInput})
    controller.selectorMap.set(1, {ref: iso.endInput})
    controller.elementTextMap.set(0, '[0]<input />')
    controller.elementTextMap.set(1, '[1]<input />')

    const globalGetByIdSpy = vi.spyOn(document, 'getElementById')
    const globalQuerySpy = vi.spyOn(document, 'querySelector')
    const globalQueryAllSpy = vi.spyOn(document, 'querySelectorAll')

    const startRes = await controller.inputText(0, '2026-10-01')
    expect(startRes.success).toBe(true)
    expect(clickedDays).toEqual(['1'])
    expect(iso.startTimeInput.value).toBe('00:00:00')
    expect(iso.endTimeInput.value).toBe('00:00:00')
    expect(startTimeEvents).toEqual([])
    expect(endTimeEvents).toEqual([])
    expect(confirmClicks).toBe(0)
    expect(clearClicks).toBe(0)

    const endRes = await controller.inputText(1, '2026-10-31')
    expect(endRes.success).toBe(true)
    expect(clickedDays).toEqual(['1', '31'])
    expect(iso.endTimeInput.value).toBe('23:59:59')
    expect(endTimeEvents).toEqual(['input', 'change'])
    expect(confirmClicks).toBe(1)
    expect(clearClicks).toBe(0)
    expect(endBlurSpy).toHaveBeenCalledTimes(1)

    expect(globalGetByIdSpy).not.toHaveBeenCalled()
    expect(globalQuerySpy).not.toHaveBeenCalled()
    expect(globalQueryAllSpy).not.toHaveBeenCalled()
    expect(decoy.endTimeInput.value).toBe('00:00:00')
    expect(decoyEndEvents).toEqual([])
    expect(decoyConfirmed).toBe(0)
  })

  it('applies explicit end time and preserves already non-midnight end time on date-only end', async () => {
    const doc1 = document.implementation.createHTMLDocument('explicit-time')
    const iso1 = buildRangeDom(doc1, {popupInitiallyOpen: true, initialEndTime: '00:00:00'})
    const events1: string[] = []
    let confirms1 = 0
    iso1.endTimeInput.addEventListener('input', () => events1.push('input'))
    iso1.endTimeInput.addEventListener('change', () => events1.push('change'))
    iso1.confirmBtn.addEventListener('click', () => { confirms1 += 1 })

    const controller1 = new PageControllerCtor({enableMask: false})
    cleanups.push(() => controller1.dispose())
    controller1.selectorMap.set(0, {ref: iso1.startInput})
    controller1.selectorMap.set(1, {ref: iso1.endInput})

    const res1 = await controller1.inputText(1, '2026-10-31 14:30:00')
    expect(res1.success).toBe(true)
    expect(iso1.endTimeInput.value).toBe('14:30:00')
    expect(events1).toEqual(['input', 'change'])
    expect(confirms1).toBe(1)

    const doc2 = document.implementation.createHTMLDocument('preserve-time')
    const iso2 = buildRangeDom(doc2, {popupInitiallyOpen: true, initialEndTime: '18:00:00'})
    const events2: string[] = []
    let confirms2 = 0
    iso2.endTimeInput.addEventListener('input', () => events2.push('input'))
    iso2.endTimeInput.addEventListener('change', () => events2.push('change'))
    iso2.confirmBtn.addEventListener('click', () => { confirms2 += 1 })

    const controller2 = new PageControllerCtor({enableMask: false})
    cleanups.push(() => controller2.dispose())
    controller2.selectorMap.set(0, {ref: iso2.startInput})
    controller2.selectorMap.set(1, {ref: iso2.endInput})

    const res2 = await controller2.inputText(1, '2026-10-31')
    expect(res2.success).toBe(true)
    expect(iso2.endTimeInput.value).toBe('18:00:00')
    expect(events2).toEqual([])
    expect(confirms2).toBe(1)
  })
})
