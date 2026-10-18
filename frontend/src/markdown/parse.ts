/**
 * A small Markdown subset, parsed to a typed tree.
 *
 * The panel runs in a page a host application may have locked down with a strict CSP and
 * Trusted Types, and model output is untrusted input. So there is no HTML string anywhere
 * in this path: the parser produces nodes, React builds elements from them, and there is
 * never an `innerHTML` for a sanitiser to have to get right.
 *
 * The grammar is deliberately closed. Raw HTML, images and arbitrary link schemes are not
 * in it, so they cannot appear by accident when a model writes something unexpected.
 */

export type Inline =
  | {type: 'text'; value: string}
  | {type: 'strong'; children: Inline[]}
  | {type: 'em'; children: Inline[]}
  | {type: 'code'; value: string}
  | {type: 'link'; href: string; children: Inline[]}

export type Block =
  | {type: 'paragraph'; children: Inline[]}
  | {type: 'heading'; depth: 2 | 3; children: Inline[]}
  | {type: 'list'; ordered: boolean; items: Inline[][]}
  | {type: 'quote'; children: Inline[]}
  | {type: 'code'; lang: string; value: string}
  | {type: 'table'; head: Inline[][]; rows: Inline[][][]; align: ('left' | 'right')[]}
  | {type: 'rule'}

const FENCE = /^```([A-Za-z0-9_+-]{0,24})\s*$/
const HEADING = /^(#{1,6})\s+(.*)$/
const BULLET = /^\s{0,3}[-*]\s+(.*)$/
const ORDERED = /^\s{0,3}\d{1,3}[.)]\s+(.*)$/
const QUOTE = /^\s{0,3}>\s?(.*)$/
const RULE = /^\s{0,3}(?:-{3,}|\*{3,})\s*$/
const ROW = /^\s*\|(.+)\|\s*$/
const DELIMITER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/

function cells(line: string): string[] {
  const match = ROW.exec(line)
  const body = match ? match[1] ?? '' : line
  const out: string[] = []
  let cell = ''
  for (let index = 0; index < body.length; index += 1) {
    const ch = body[index] ?? ''
    if (ch === '\\' && index + 1 < body.length) {
      cell += ch + body[index + 1]
      index += 1
    } else if (ch === '|') {
      out.push(cell.trim())
      cell = ''
    } else cell += ch
  }
  out.push(cell.trim())
  return out
}

/** Inline parsing is a single left-to-right scan; no backtracking, no regex on user text. */
export function parseInline(source: string): Inline[] {
  const out: Inline[] = []
  let text = ''
  const flush = () => {
    if (text) out.push({type: 'text', value: text})
    text = ''
  }
  let index = 0
  while (index < source.length) {
    const rest = source.slice(index)
    // Escaped punctuation is literal application data, never formatting or a link.
    const escaped = /^\\([!"#$%&'()*+,\-./:;<=>?@[\]\\^_`{|}~])/.exec(rest)
    if (escaped) {
      text += escaped[1]
      index += escaped[0].length
      continue
    }
    const code = /^`([^`]+)`/.exec(rest)
    if (code) {
      flush()
      out.push({type: 'code', value: code[1] ?? ''})
      index += code[0].length
      continue
    }
    const strong = /^\*\*([^*]+)\*\*/.exec(rest)
    if (strong) {
      flush()
      out.push({type: 'strong', children: parseInline(strong[1] ?? '')})
      index += strong[0].length
      continue
    }
    const em = /^\*([^*]+)\*/.exec(rest)
    if (em) {
      flush()
      out.push({type: 'em', children: parseInline(em[1] ?? '')})
      index += em[0].length
      continue
    }
    const link = /^\[([^\]]*)\]\(([^)\s]+)\)/.exec(rest)
    if (link) {
      flush()
      out.push({type: 'link', href: link[2] ?? '', children: parseInline(link[1] ?? '')})
      index += link[0].length
      continue
    }
    text += source[index]
    index += 1
  }
  flush()
  return out
}

export function parseMarkdown(source: string): Block[] {
  const lines = String(source ?? '').replace(/\r\n/g, '\n').split('\n')
  const blocks: Block[] = []
  let index = 0

  const paragraph: string[] = []
  const flushParagraph = () => {
    if (!paragraph.length) return
    blocks.push({type: 'paragraph', children: parseInline(paragraph.join('\n').trim())})
    paragraph.length = 0
  }

  while (index < lines.length) {
    const line = lines[index] ?? ''

    const fence = FENCE.exec(line)
    if (fence) {
      flushParagraph()
      const body: string[] = []
      index += 1
      while (index < lines.length && !FENCE.test(lines[index] ?? '')) {
        body.push(lines[index] ?? '')
        index += 1
      }
      index += 1
      blocks.push({type: 'code', lang: fence[1] ?? '', value: body.join('\n')})
      continue
    }

    if (!line.trim()) {
      flushParagraph()
      index += 1
      continue
    }

    if (RULE.test(line)) {
      flushParagraph()
      blocks.push({type: 'rule'})
      index += 1
      continue
    }

    const heading = HEADING.exec(line)
    if (heading) {
      flushParagraph()
      blocks.push({
        type: 'heading',
        depth: (heading[1] ?? '##').length <= 2 ? 2 : 3,
        children: parseInline(heading[2] ?? ''),
      })
      index += 1
      continue
    }

    if (ROW.test(line) && DELIMITER.test(lines[index + 1] ?? '')) {
      flushParagraph()
      const head = cells(line)
      const align: ('left' | 'right')[] = cells(lines[index + 1] ?? '')
        .map(cell => (cell.endsWith(':') && !cell.startsWith(':') ? 'right' : 'left'))
      const rows: Inline[][][] = []
      index += 2
      while (index < lines.length && ROW.test(lines[index] ?? '')) {
        rows.push(cells(lines[index] ?? '').map(parseInline))
        index += 1
      }
      blocks.push({type: 'table', head: head.map(parseInline), rows, align})
      continue
    }

    if (QUOTE.test(line)) {
      flushParagraph()
      const body: string[] = []
      while (index < lines.length && QUOTE.test(lines[index] ?? '')) {
        body.push(QUOTE.exec(lines[index] ?? '')?.[1] ?? '')
        index += 1
      }
      blocks.push({type: 'quote', children: parseInline(body.join('\n'))})
      continue
    }

    const bullet = BULLET.exec(line)
    const ordered = ORDERED.exec(line)
    if (bullet || ordered) {
      flushParagraph()
      const isOrdered = Boolean(ordered)
      const items: Inline[][] = []
      while (index < lines.length) {
        const current = lines[index] ?? ''
        const match = isOrdered ? ORDERED.exec(current) : BULLET.exec(current)
        if (!match) break
        items.push(parseInline(match[1] ?? ''))
        index += 1
      }
      blocks.push({type: 'list', ordered: isOrdered, items})
      continue
    }

    paragraph.push(line)
    index += 1
  }
  flushParagraph()
  return blocks
}

/** Total visible characters, so the typewriter can reveal a parsed tree without re-parsing. */
export function countCharacters(blocks: Block[]): number {
  const inline = (nodes: Inline[]): number => nodes.reduce((total, node) => {
    if (node.type === 'text' || node.type === 'code') return total + node.value.length
    return total + inline(node.children)
  }, 0)
  return blocks.reduce((total, block) => {
    switch (block.type) {
      case 'paragraph': case 'heading': case 'quote': return total + inline(block.children)
      case 'list': return total + block.items.reduce((sum, item) => sum + inline(item), 0)
      case 'code': return total + block.value.length
      case 'table': return total + block.head.reduce((sum, cell) => sum + inline(cell), 0)
        + block.rows.reduce((sum, row) =>
          sum + row.reduce((cell, node) => cell + inline(node), 0), 0)
      case 'rule': return total + 1
    }
  }, 0)
}
