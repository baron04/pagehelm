import {describe, expect, it} from 'vitest'
import {examplesFromPage, parseExamples} from '../src/runtime/examples'

const LIMIT = 40

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
