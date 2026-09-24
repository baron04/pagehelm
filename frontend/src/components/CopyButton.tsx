import {useEffect, useRef, useState} from 'react'
import {Check, Copy} from 'lucide-react'
import {copyText} from './clipboard'
import {iconProps} from './icon'

/**
 * A button that copies what `text` returns and says so for a moment.
 *
 * The text is produced at the press, not at render, so a long answer is not flattened on
 * every frame of its reveal. `showLabel` puts the words beside the glyph; without it the
 * glyph stands alone and the words are its tooltip and accessible name.
 */
export function CopyButton({text, label, copiedLabel, className, showLabel = false}: {
  text: () => string
  label: string
  copiedLabel: string
  className: string
  showLabel?: boolean
}): React.JSX.Element {
  const [copied, setCopied] = useState(false)
  const timer = useRef(0)
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const name = copied ? copiedLabel : label
  return (
    <button type="button" className={className} aria-label={name}
            data-tip={showLabel ? undefined : name}
            data-copied={copied ? 'true' : undefined}
            onClick={event => {
              const button = event.currentTarget
              void copyText(text(), button).then(done => {
                if (!done) return
                setCopied(true)
                window.clearTimeout(timer.current)
                timer.current = window.setTimeout(() => setCopied(false), 1500)
              })
            }}>
      {copied ? <Check {...iconProps(14)} /> : <Copy {...iconProps(14)} />}
      {showLabel && <span>{name}</span>}
    </button>
  )
}
