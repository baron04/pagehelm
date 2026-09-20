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
    // The ceiling lives in the stylesheet; reading it back keeps one value in one place.
    const ceiling = parseFloat(getComputedStyle(node).maxHeight)
    const needed = node.scrollHeight
    node.style.height = `${Number.isFinite(ceiling) ? Math.min(needed, ceiling) : needed}px`
    // And the box only scrolls once it has stopped growing. Left on `auto`, the half pixel
    // a fractional line height leaves over is enough to park a scrollbar next to a single
    // line of placeholder -- permanently, for anyone who asks for non-overlay scrollbars.
    node.style.overflowY = Number.isFinite(ceiling) && needed > ceiling ? 'auto' : 'hidden'
    // Disabling the field lowers its ceiling, so it is measured again when that changes.
  }, [draft, disabled])

  // Deliberately keyed on `disabled` alone: the panel remounts the composer every time it
  // opens, so "it mounted usable" is the moment to take focus, and "a lock lifted" is the
  // next. A turn *ending* (`busy` flipping to false) is not -- that lands while the person
  // may be pointing at the host page behind us, and yanking the caret into the textarea is
  // the panel stealing the operator's own work. `busy` is read when focus is decided, not
  // followed: hence the ref, which keeps the effect honest with the exhaustive-deps rule.
  const busyNow = useRef(busy)
  busyNow.current = busy
  useEffect(() => {
    if (!disabled && !busyNow.current) input.current?.focus()
  }, [disabled])

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
