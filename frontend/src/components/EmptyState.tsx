import {ListOrdered, Plus, Search} from 'lucide-react'
import type {Labels, Suggestion} from '../contract/panel'
import {iconProps} from './icon'
import {Mark} from './Mark'

/**
 * The default state is also the only capability disclosure this product gets. The
 * suggestions are goals the application declares for the workflows the live gate qualifies
 * -- enumerate, filter, write -- so nothing here promises something the agent has not been
 * shown to do. They arrive on the configuration because the promise is the application's;
 * this file may not invent one, and an application that declares none gets none.
 *
 * What is local here is only which glyph decorates which workflow -- an icon is not a
 * string anyone reads aloud.
 *
 * Picking one fills the composer. It never submits: a write is not something to start with
 * a single stray click.
 */
const WORKFLOW_ICONS = {enumerate: ListOrdered, filter: Search, write: Plus} as const

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
      {suggestions.length > 0 && <ul className="woa-suggestions">
        {suggestions.map(item => {
          const Icon = WORKFLOW_ICONS[item.workflow]
          return (
            <li key={item.text}>
              <button type="button" onClick={() => onPick(item.text)}>
                <span className="woa-suggestions__icon"><Icon {...iconProps(14)} /></span>
                <span>{item.text}</span>
                <span className="woa-suggestions__kind">{item.kind}</span>
              </button>
            </li>
          )
        })}
      </ul>}
    </div>
  )
}
