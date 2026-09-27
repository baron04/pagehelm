import {afterEach, describe, expect, it, vi} from 'vitest'
import {examplesFromPage, parseExamples, watchForExamples} from '../src/runtime/examples'

const LIMIT = 40

/** `document.readyState` is read-only; a harness racing the parser is tested under a document
 * that still claims to be loading, restored after. */
function whileLoading(run: () => void): void {
  const real = Object.getOwnPropertyDescriptor(Document.prototype, 'readyState')
  Object.defineProperty(document, 'readyState', {configurable: true, get: () => 'loading'})
  try {
    run()
  } finally {
    if (real) Object.defineProperty(Document.prototype, 'readyState', real)
  }
}

describe('the examples a page offers a new conversation', () => {
  it('reads one per kind, in the order the page gives them, trimmed', () => {
    expect(parseExamples(JSON.stringify([
      {kind: 'find', text: ' 在哪里新建报价单 '},
      {kind: 'query', text: '列出所有待开票的销售订单'},
    ]), LIMIT)).toEqual([
      {kind: 'find', text: '在哪里新建报价单'},
      {kind: 'query', text: '列出所有待开票的销售订单'},
    ])
  })

  it('offers none when the page offers none', () => {
    expect(parseExamples(undefined, LIMIT)).toEqual([])
    expect(parseExamples('  ', LIMIT)).toEqual([])
    expect(parseExamples('[]', LIMIT)).toEqual([])
  })

  it('refuses the whole list rather than show part of a wrong one', () => {
    // A page is input: an example that is not what it says is left out with the rest, and
    // the panel shows the way in that needs no example.
    for (const value of [
      '{',
      '{"kind": "query", "text": "x"}',
      JSON.stringify([{kind: 'write', text: '把 S00002 确认成订单'}]),
      JSON.stringify([{kind: 'query', text: 'a'}, {kind: 'query', text: 'b'}]),
      JSON.stringify([{kind: 'query', text: ''}]),
      JSON.stringify([{kind: 'query', text: 'x'.repeat(LIMIT + 1)}]),
      JSON.stringify([{kind: 'query', text: '第一行\n第二行'}]),
      JSON.stringify([{kind: 'query'}]),
      JSON.stringify([null]),
    ]) {
      expect(parseExamples(value, LIMIT), value).toEqual([])
    }
  })

  it('comes from the tag that asked for the assistant', () => {
    document.body.innerHTML = `<script type="module" data-woa-bootstrap="/assistant/bootstrap"
      data-woa-examples='[{"kind": "operate", "text": "打开待开票的销售订单列表"}]'></script>`
    expect(examplesFromPage(document, LIMIT))
      .toEqual([{kind: 'operate', text: '打开待开票的销售订单列表'}])
    document.body.innerHTML = '<script type="module" data-woa-bootstrap="/b"></script>'
    expect(examplesFromPage(document, LIMIT)).toEqual([])
  })
})

describe('watching for examples a harness injects ahead of the page', () => {
  afterEach(() => { document.body.innerHTML = '' })

  it('reads them at once when the tag is already there', () => {
    document.body.innerHTML = `<script data-woa-bootstrap="/b"
      data-woa-examples='[{"kind": "query", "text": "列出所有待开票的销售订单"}]'></script>`
    const found: unknown[] = []
    watchForExamples(document, LIMIT, examples => found.push(examples))
    expect(found).toEqual([[{kind: 'query', text: '列出所有待开票的销售订单'}]])
  })

  it('does nothing on a settled document with no tag', () => {
    // `readyState` is only 'loading' while a harness's script runs ahead of the parser; a
    // page loaded the ordinary way is already settled by the time any script of its own runs.
    const found: unknown[] = []
    watchForExamples(document, LIMIT, examples => found.push(examples))
    expect(found).toEqual([])
  })

  it('catches the tag the parser inserts after the one first read missed it', async () => {
    // The exact race this exists for: a harness's script runs before the parser reaches the
    // tag, so the immediate read finds nothing; `DOMContentLoaded` can itself fire while a
    // large document is still being parsed, so waiting for only that event still missed the
    // tag some of the time. The tag arriving as a plain DOM mutation, well after both, must
    // still be caught.
    const found: unknown[] = []
    whileLoading(() => watchForExamples(document, LIMIT, examples => found.push(examples)))
    expect(found).toEqual([])
    document.body.innerHTML = `<script data-woa-bootstrap="/b"
      data-woa-examples='[{"kind": "find", "text": "在哪里新建报价单"}]'></script>`
    await vi.waitFor(() => expect(found).toEqual([[{kind: 'find', text: '在哪里新建报价单'}]]))
  })

  it('gives up once the document has finished loading with nothing to find', () => {
    const found: unknown[] = []
    whileLoading(() => watchForExamples(document, LIMIT, examples => found.push(examples)))
    document.dispatchEvent(new Event('DOMContentLoaded'))
    document.body.innerHTML = `<script data-woa-bootstrap="/b"
      data-woa-examples='[{"kind": "operate", "text": "打开待开票的销售订单列表"}]'></script>`
    expect(found).toEqual([])
  })

  it('stops watching once asked to, even before the tag arrives', () => {
    const found: unknown[] = []
    let stop = () => {}
    whileLoading(() => { stop = watchForExamples(document, LIMIT, examples => found.push(examples)) })
    stop()
    document.body.innerHTML = `<script data-woa-bootstrap="/b"
      data-woa-examples='[{"kind": "query", "text": "列出所有待开票的销售订单"}]'></script>`
    expect(found).toEqual([])
  })
})
