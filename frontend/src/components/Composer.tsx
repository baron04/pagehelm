import {useEffect, useLayoutEffect, useRef, useState} from 'react'
import {ArrowUp, Square} from 'lucide-react'
import type {Labels, PanelLimits} from '../contract/panel'

export interface ComposerProps {
  labels: Labels
  limits: PanelLimits
  busy: boolean
  stopping: boolean
  disabled: boolean
  placeholder: string
  draft: string
  onDraft: (value: string) => void
  onSubmit: (text: string) => void
  onCancel: () => void
}

export function Composer(props: ComposerProps): React.JSX.Element {
  const {labels, limits, busy, stopping, disabled, placeholder, draft, onDraft} = props
  const input = useRef<HTMLTextAreaElement>(null)
  const [focused, setFocused] = useState(false)
  const [atLimit, setAtLimit] = useState(false)

  useLayoutEffect(() => {
    const node = input.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(node.scrollHeight, 168)}px`
  }, [draft])

  useEffect(() => {
    if (!disabled && !busy) input.current?.focus()
  }, [disabled, busy])

  const send = () => {
    const text = draft.trim()
    if (!text || busy || disabled) return
    props.onSubmit(text)
  }

  return (
    <form className="woa-composer" onSubmit={event => {
      event.preventDefault()
      send()
    }}>
      <div className="woa-field" data-focus={focused ? 'true' : undefined}>
        <label className="woa-sr" htmlFor="woa-composer-input">{labels.input}</label>
        <textarea
          id="woa-composer-input"
          ref={input}
          rows={1}
          value={draft}
          maxLength={limits.composer_max_chars}
          placeholder={placeholder}
          disabled={disabled}
          aria-describedby="woa-composer-hint"
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          onChange={event => {
            onDraft(event.target.value)
            setAtLimit(event.target.value.length >= limits.composer_max_chars)
          }}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              send()
            }
          }}
        />
        {/* Kept for screen readers only: the visible hint was noise for everyone else. */}
        <span className="woa-sr" id="woa-composer-hint">{labels.keyHint}</span>
        {busy
          ? (
            <button type="button" className="woa-send" data-stop="true"
                    aria-label={labels.stop} disabled={stopping} onClick={props.onCancel}>
              <Square size={13} strokeWidth={2} aria-hidden="true" />
            </button>
          )
          : (
            <button type="submit" className="woa-send" aria-label={labels.send}
                    data-off={disabled || !draft.trim() ? 'true' : undefined}
                    disabled={disabled || !draft.trim()}>
              <ArrowUp size={14} strokeWidth={2} aria-hidden="true" />
            </button>
          )}
      </div>
      {atLimit && <p className="woa-composer__limit">{limits.composer_max_chars}</p>}
    </form>
  )
}
