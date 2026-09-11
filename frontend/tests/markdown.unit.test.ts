import {describe, expect, it} from 'vitest'
import {countCharacters, parseInline, parseMarkdown} from '../src/markdown/parse'

describe('the markdown grammar is closed', () => {
  it('produces nodes, never html, for the constructs an answer uses', () => {
    const blocks = parseMarkdown([
      '## 结果',
      '',
      '3 张销售订单待开票：',
      '',
      '| 单号 | 客户 | 金额 |',
      '| --- | --- | ---: |',
      '| S00007 | Deco Addict | 1,231.00 |',
      '',
      '- 第一条',
      '- 第二条',
      '',
      '```python',
      "print('hi')",
      '```',
    ].join('\n'))
    expect(blocks.map(block => block.type)).toEqual(
      ['heading', 'paragraph', 'table', 'list', 'code'])
    const table = blocks[2]
    expect(table?.type).toBe('table')
    if (table?.type === 'table') {
      expect(table.align).toEqual(['left', 'left', 'right'])
      expect(table.rows).toHaveLength(1)
    }
  })

  it('never yields a raw html node, whatever the model writes', () => {
    const blocks = parseMarkdown('<img src=x onerror=alert(1)>\n\n<script>bad()</script>')
    expect(blocks.every(block => block.type === 'paragraph')).toBe(true)
    // The angle brackets survive as text, which is the point: they are shown, not parsed.
    expect(JSON.stringify(blocks)).toContain('onerror=alert(1)')
    expect(blocks.some(block => 'type' in block && String(block.type).includes('html')))
      .toBe(false)
  })

  it('keeps an unterminated fence from swallowing the rest of the answer silently', () => {
    const blocks = parseMarkdown('前言\n\n```\nnever closed')
    expect(blocks[0]?.type).toBe('paragraph')
    expect(blocks[1]?.type).toBe('code')
  })

  it('counts exactly the characters the reveal will show', () => {
    const blocks = parseMarkdown('**粗** 与 `码`')
    expect(countCharacters(blocks)).toBe('粗 与 码'.length)
  })

  it('parses inline spans left to right without backtracking', () => {
    expect(parseInline('a **b** c `d` [e](/f)').map(node => node.type))
      .toEqual(['text', 'strong', 'text', 'code', 'text', 'link'])
  })
})
