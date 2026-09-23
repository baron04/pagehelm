import {beforeEach, describe, expect, it} from 'vitest'
import {selectedControlsFromPage} from '../src/runtime/pageSelection'
import {createRedactor} from '../src/runtime/redaction'

const redactor = createRedactor()

beforeEach(() => { document.body.innerHTML = '' })

function visible(group: Element): void {
  Object.defineProperty(group, 'getBoundingClientRect', {configurable: true, value: () => ({
    width: 200, height: 30, top: 10, left: 10, bottom: 40, right: 210,
  })})
}

describe('selectedControlsFromPage', () => {
  it('reports a visible exclusive view even if PageController omitted its buttons', () => {
    document.body.innerHTML = '<nav><button data-tooltip="List"></button>' +
      '<button class="active" data-tooltip="Pivot"></button></nav>'
    visible(document.querySelector('nav')!)
    expect(selectedControlsFromPage(document, redactor)).toEqual(['Pivot'])
  })

  it('ignores lone, hidden, offscreen and ambiguous active buttons', () => {
    document.body.innerHTML = '<nav id="lone"><button class="active">List</button></nav>' +
      '<nav id="hidden" hidden><button class="active">Graph</button><button>List</button></nav>' +
      '<nav id="ambiguous"><button class="active">List</button>' +
      '<button class="active">Kanban</button></nav>' +
      '<nav id="offscreen"><button class="active">Calendar</button><button>List</button></nav>'
    for (const group of document.querySelectorAll('nav')) visible(group)
    Object.defineProperty(document.querySelector('#offscreen'), 'getBoundingClientRect', {
      value: () => ({width: 200, height: 30, top: 2000, left: 10, bottom: 2030, right: 210}),
    })
    expect(selectedControlsFromPage(document, redactor)).toEqual([])
  })

  it('accepts explicit pressed state and does not report a sensitive name', () => {
    document.body.innerHTML = '<div role="group"><button aria-pressed="true" ' +
      'aria-label="Calendar"></button><button aria-pressed="false">List</button></div>' +
      '<nav><button class="active">API Key</button><button>Other</button></nav>'
    for (const group of document.querySelectorAll('nav, [role="group"]')) visible(group)
    expect(selectedControlsFromPage(document, redactor)).toEqual(['Calendar'])
  })

  it('does not report a toolbar covered by a different top layer', () => {
    document.body.innerHTML = '<nav><button class="active">Graph</button>' +
      '<button>List</button></nav><div id="cover"></div>'
    visible(document.querySelector('nav')!)
    const original = Object.getOwnPropertyDescriptor(document, 'elementFromPoint')
    Object.defineProperty(document, 'elementFromPoint', {
      configurable: true, value: () => document.querySelector('#cover'),
    })
    try {
      expect(selectedControlsFromPage(document, redactor)).toEqual([])
    } finally {
      if (original) Object.defineProperty(document, 'elementFromPoint', original)
      else delete (document as Document & {elementFromPoint?: unknown}).elementFromPoint
    }
  })
})
