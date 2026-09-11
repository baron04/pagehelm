import {ListOrdered, Plus, Search} from 'lucide-react'
import type {Labels} from '../contract/panel'
import {Mark} from './Mark'

/**
 * The default state is also the only capability disclosure this product gets. The three
 * suggestions are the workflow kinds the live gate actually qualifies -- enumerate, filter,
 * write -- so nothing here promises something the agent has not been shown to do.
 *
 * Picking one fills the composer. It never submits: a write is not something to start with
 * a single stray click.
 */
const SUGGESTIONS = [
  {icon: ListOrdered, text: '列出所有待开票的销售订单', kind: '列举'},
  {icon: Search, text: 'Acme 有哪些草稿报价单', kind: '条件查询'},
  {icon: Plus, text: '把报价单 S00002 确认成销售订单', kind: '写入'},
] as const

export function EmptyState({labels, onPick, compact}: {
  labels: Labels
  onPick: (text: string) => void
  compact?: boolean
}): React.JSX.Element {
  return (
    <div className="woa-empty" data-compact={compact ? 'true' : undefined}>
      {!compact && <span className="woa-empty__hero"><Mark size={22} /></span>}
      <h3>{labels.emptyGreeting}</h3>
      {!compact && <p>{labels.emptyDetail}</p>}
      <ul className="woa-suggestions">
        {SUGGESTIONS.map(item => (
          <li key={item.text}>
            <button type="button" onClick={() => onPick(item.text)}>
              <item.icon size={13} strokeWidth={1.7} aria-hidden="true" />
              <span>{item.text}</span>
              <span className="woa-suggestions__kind">{item.kind}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
