/**
 * The plain text of an answer, for the clipboard.
 *
 * Copied from the parsed answer rather than from the page, so it is the whole answer even
 * while the prose is still being revealed, and a table arrives as tab-separated rows -- the
 * shape a spreadsheet splits into cells on paste.
 */
import {parseMarkdown, type Block, type Inline} from './parse'

export function inlineText(nodes: Inline[]): string {
  return nodes.map(node => ('children' in node ? inlineText(node.children) : node.value)).join('')
}

function tableText(block: Extract<Block, {type: 'table'}>): string {
  // A tab or newline inside a cell would split it on paste; a space keeps it one cell.
  const cell = (nodes: Inline[]) => inlineText(nodes).replace(/[\t\n]+/g, ' ')
  return [block.head, ...block.rows].map(row => row.map(cell).join('\t')).join('\n')
}

function blockText(block: Block): string {
  switch (block.type) {
    case 'paragraph':
    case 'heading':
    case 'quote':
      return inlineText(block.children)
    case 'list':
      return block.items.map((item, index) =>
        `${block.ordered ? `${index + 1}.` : '-'} ${inlineText(item)}`).join('\n')
    case 'code':
      return block.value
    case 'table':
      return tableText(block)
    case 'rule':
      return ''
  }
}

export function plainText(markdown: string): string {
  return parseMarkdown(markdown).map(blockText).filter(Boolean).join('\n\n')
}
