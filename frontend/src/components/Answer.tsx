import {memo, useMemo, useState} from 'react'
import {Check, Copy} from 'lucide-react'
import type {Block, Inline} from '../markdown/parse'
import {countCharacters, parseMarkdown} from '../markdown/parse'
import type {Labels} from '../contract/panel'
import {useReveal} from '../state/typewriter'

/** A budget that walks the tree once per render, revealing a prefix of the visible text. */
class Budget {
  private left: number

  constructor(limit: number) {
    this.left = limit
  }

  take(text: string): string {
    if (this.left <= 0) return ''
    const slice = text.slice(0, this.left)
    this.left -= slice.length
    return slice
  }

  get exhausted(): boolean {
    return this.left <= 0
  }
}

function renderInline(nodes: Inline[], budget: Budget, allow: (href: string) => boolean): React.ReactNode[] {
  const out: React.ReactNode[] = []
  nodes.forEach((node, index) => {
    if (budget.exhausted) return
    switch (node.type) {
      case 'text':
        out.push(budget.take(node.value))
        break
      case 'code':
        out.push(<code key={index} className="woa-md__code">{budget.take(node.value)}</code>)
        break
      case 'strong':
        out.push(<strong key={index}>{renderInline(node.children, budget, allow)}</strong>)
        break
      case 'em':
        out.push(<em key={index}>{renderInline(node.children, budget, allow)}</em>)
        break
      case 'link':
        // Model output is untrusted. A link only becomes clickable when it points at an
        // origin this session is already bound to; anything else renders as plain text.
        out.push(allow(node.href)
          ? (
            <a key={index} href={node.href} rel="noreferrer noopener" target="_blank">
              {renderInline(node.children, budget, allow)}
            </a>
          )
          : <span key={index} className="woa-md__inert">{renderInline(node.children, budget, allow)}</span>)
        break
    }
  })
  return out
}

function CodeBlock({block, labels}: {block: Extract<Block, {type: 'code'}>; labels: Labels}): React.JSX.Element {
  const [copied, setCopied] = useState(false)
  const lines = block.value.split('\n')
  const [expanded, setExpanded] = useState(lines.length <= 12)
  const shown = expanded ? block.value : lines.slice(0, 12).join('\n')
  return (
    <figure className="woa-md__block">
      <header>
        <span className="woa-md__lang">{block.lang || 'text'}</span>
        <button type="button" className="woa-md__copy" onClick={() => {
          void navigator.clipboard?.writeText(block.value).then(() => {
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          }).catch(() => setCopied(false))
        }}>
          {copied
            ? <Check size={12} strokeWidth={1.9} aria-hidden="true" />
            : <Copy size={12} strokeWidth={1.7} aria-hidden="true" />}
          {copied ? labels.copied : labels.copy}
        </button>
      </header>
      <pre>{shown}</pre>
      {!expanded && (
        <button type="button" className="woa-md__more" onClick={() => setExpanded(true)}>
          {labels.expandAll}
        </button>
      )}
    </figure>
  )
}

function renderBlock(block: Block, key: number, budget: Budget, labels: Labels,
                     allow: (href: string) => boolean): React.ReactNode {
  if (budget.exhausted) return null
  switch (block.type) {
    case 'paragraph':
      return <p key={key}>{renderInline(block.children, budget, allow)}</p>
    case 'heading':
      return block.depth === 2
        ? <h2 key={key}>{renderInline(block.children, budget, allow)}</h2>
        : <h3 key={key}>{renderInline(block.children, budget, allow)}</h3>
    case 'quote':
      return <blockquote key={key}>{renderInline(block.children, budget, allow)}</blockquote>
    case 'rule':
      return budget.take(' ') ? <hr key={key} /> : null
    case 'list':
      return block.ordered
        ? (
          <ol key={key} className="woa-md__list">
            {block.items.map((item, index) => <li key={index}>{renderInline(item, budget, allow)}</li>)}
          </ol>
        )
        : (
          <ul key={key} className="woa-md__list">
            {block.items.map((item, index) => <li key={index}>{renderInline(item, budget, allow)}</li>)}
          </ul>
        )
    case 'code':
      return budget.take(block.value).length
        ? <CodeBlock key={key} block={block} labels={labels} /> : null
    case 'table':
      return (
        <div key={key} className="woa-md__tablewrap">
          <table>
            <thead>
              <tr>{block.head.map((cell, index) => (
                <th key={index} data-align={block.align[index] ?? 'left'}>
                  {renderInline(cell, budget, allow)}
                </th>
              ))}</tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>{row.map((cell, index) => (
                  <td key={index} data-align={block.align[index] ?? 'left'}>
                    {renderInline(cell, budget, allow)}
                  </td>
                ))}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )
  }
}

export interface AnswerProps {
  text: string
  /** Only model-authored prose is revealed progressively; see state/typewriter.ts. */
  authored: 'model' | 'server'
  live: boolean
  labels: Labels
  allowedOrigins: readonly string[]
}

export const Answer = memo(function Answer(props: AnswerProps): React.JSX.Element {
  const {text, authored, live, labels, allowedOrigins} = props
  // Parsed once per distinct text, never per frame: the reveal moves a number, not a parser.
  const blocks = useMemo(() => parseMarkdown(text), [text])
  const total = useMemo(() => countCharacters(blocks), [blocks])
  const reveal = useReveal(total, live && authored === 'model')
  const allow = useMemo(() => (href: string) => {
    try {
      return allowedOrigins.includes(new URL(href, window.location.href).origin)
    } catch {
      return false
    }
  }, [allowedOrigins])

  const budget = new Budget(reveal.visible)
  return (
    <div className="woa-md" data-authored={authored}
         onClick={reveal.done ? undefined : reveal.skip}>
      {blocks.map((block, index) => renderBlock(block, index, budget, labels, allow))}
      {!reveal.done && <span className="woa-md__caret" aria-hidden="true" />}
    </div>
  )
})
