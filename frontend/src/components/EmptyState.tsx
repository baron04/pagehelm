import {ListOrdered, Plus, Search} from 'lucide-react'
import type {Labels, Suggestion} from '../contract/panel'
import {iconProps} from './icon'
import {Mark} from './Mark'

/**
 * The default state is also the only capability disclosure this product gets. The three
 * suggestions are the workflow kinds the live gate actually qualifies -- enumerate, filter,
 * write -- so nothing here promises something the agent has not been shown to do. That is
 * why they arrive on the configuration (`contract["suggestions"]`): the promise itself is
 * the server's, and this file may not invent a fourth.
 *
 * What is local here is only which glyph decorates which row -- an icon is not a string
 * anyone reads aloud.
 *
 * Picking one fills the composer. It never submits: a write is not something to start with
 * a single stray click.
 */
const SUGGESTION_ICONS = [ListOrdered, Search, Plus]

export function EmptyState({labels, suggestions, onPick, compact}: {
  labels: Labels
  suggestions: readonly Suggestion[]
  onPick: (text: string) => void
  compact?: boolean
}): React.JSX.Element {
  return (
    <div className="woa-empty" data-compact={compact ? 'true' : undefined}>
      {!compact && <span className="woa-empty__hero"><Mark size={22} /></span>}
      <h3>{labels.emptyGreeting}</h3>
      {!compact && <p>{labels.emptyDetail}</p>}
      <ul className="woa-suggestions">
        {suggestions.map((item, index) => {
          const Icon = SUGGESTION_ICONS[index] ?? Search
          return (
            <li key={item.text}>
              <button type="button" onClick={() => onPick(item.text)}>
                <Icon {...iconProps(14)} />
                <span>{item.text}</span>
                <span className="woa-suggestions__kind">{item.kind}</span>
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
