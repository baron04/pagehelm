import {memo, useMemo, useState} from 'react'
import type {Block} from '../markdown/parse'
import {countCharacters, parseMarkdown} from '../markdown/parse'
import type {Labels} from '../contract/panel'
import {useReveal} from '../state/typewriter'
import {CopyButton} from './CopyButton'
import {DataTable} from './DataTable'
import {Budget, isAllowedOrigin, renderInline} from './inline'

function CodeBlock({block, labels}: {block: Extract<Block, {type: 'code'}>; labels: Labels}): React.JSX.Element {
  const lines = block.value.split('\n')
  const [expanded, setExpanded] = useState(lines.length <= 12)
  const shown = expanded ? block.value : lines.slice(0, 12).join('\n')
  return (
    <figure className="woa-md__block">
      <header>
        <span className="woa-md__lang">{block.lang || 'text'}</span>
        <CopyButton text={() => block.value} label={labels.copy} copiedLabel={labels.copied}
                    className="woa-md__copy" showLabel />
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
  // Tables render immediately and bypass typewriter animation even in live mode.
  if (block.type === 'table') {
    return <DataTable key={key} block={block} labels={labels} allow={allow} />
  }
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
  }
}

function countRevealCharacters(blocks: Block[]): number {
  return blocks.reduce((total, block) => (
    block.type === 'table' ? total : total + countCharacters([block])
  ), 0)
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
  const total = useMemo(() => countRevealCharacters(blocks), [blocks])
  const reveal = useReveal(total, live && authored === 'model')
  const allow = useMemo(() => isAllowedOrigin(allowedOrigins), [allowedOrigins])

  const budget = new Budget(reveal.visible)
  return (
    <div className="woa-md" data-authored={authored}
         onClick={reveal.done ? undefined : reveal.skip}>
      {blocks.map((block, index) => renderBlock(block, index, budget, labels, allow))}
      {!reveal.done && <span className="woa-md__caret" aria-hidden="true" />}
    </div>
  )
})
