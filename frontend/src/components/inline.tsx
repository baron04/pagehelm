import type {Inline} from '../markdown/parse'

/** A budget that walks the tree once per render, revealing a prefix of the visible text. */
export class Budget {
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

export function renderInline(
  nodes: Inline[],
  budget: Budget,
  allow: (href: string) => boolean,
): React.ReactNode[] {
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

export function renderStaticInline(
  nodes: Inline[],
  allow: (href: string) => boolean,
): React.ReactNode[] {
  return renderInline(nodes, new Budget(Number.POSITIVE_INFINITY), allow)
}
