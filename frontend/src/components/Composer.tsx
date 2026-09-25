import {useEffect, useLayoutEffect, useRef, useState} from 'react'
import {ArrowUp, Square} from 'lucide-react'
import type {Labels, PanelLimits} from '../contract/panel'
import {iconProps} from './icon'

export interface ComposerProps {
  labels: Labels
  limits: PanelLimits
  busy: boolean
  canReplace: boolean
  stopping: boolean
  disabled: boolean
  /** The session is over: nothing can be sent from here again, so nothing offers to. */
  ended: boolean
  /** The goals already asked in this conversation, oldest first, for ↑ to bring back. */
  history: readonly string[]
  placeholder: string
  draft: string
  /** Bumped when something outside fills the draft for the person to edit: take the caret. */
  focusRequest?: number
  onDraft: (value: string) => void
  onSubmit: (text: string) => void
  onCancel: () => void
}

export function Composer(props: ComposerProps): React.JSX.Element {
  const {labels, limits, busy, canReplace, stopping, disabled, placeholder, draft, onDraft} = props
  const input = useRef<HTMLTextAreaElement>(null)
  const [focused, setFocused] = useState(false)
  const [atLimit, setAtLimit] = useState(false)
  // Which earlier goal the arrows last brought back. Typing ends the walk: from then on the
  // text is the person's own again and the arrows move the caret.
  const recall = useRef<number | null>(null)

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
  // A picked example: the caret goes to the end of it, ready to edit or send.
  useEffect(() => {
    const node = input.current
    if (!props.focusRequest || !node || disabled) return
    node.focus()
    node.setSelectionRange(node.value.length, node.value.length)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.focusRequest])

  const send = () => {
    const text = draft.trim()
    if (!text || disabled || (busy && !canReplace)) return
    props.onSubmit(text)
  }

  return (
    <form className="woa-composer" onSubmit={event => {
      event.preventDefault()
      send()
    }}>
      <div className="woa-field" data-focus={focused ? 'true' : undefined}
           data-ended={props.ended ? 'true' : undefined}>
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
            recall.current = null
            onDraft(event.target.value)
            setAtLimit(event.target.value.length >= limits.composer_max_chars)
          }}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              send()
              return
            }
            const plain = !event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey
              && !event.nativeEvent.isComposing
            if (!plain || (event.key !== 'ArrowUp' && event.key !== 'ArrowDown')) return
            const {history} = props
            const at = recall.current
            const untouched = at !== null && draft === history[at]
            if (event.key === 'ArrowUp' && history.length > 0
                && (draft === '' || untouched) && at !== 0) {
              event.preventDefault()
              const back = at === null ? history.length - 1 : at - 1
              recall.current = back
              onDraft(history[back] ?? '')
            } else if (event.key === 'ArrowDown' && untouched) {
              event.preventDefault()
              const forward = at + 1
              recall.current = forward < history.length ? forward : null
              onDraft(history[forward] ?? '')
            }
          }}
        />
        {/* Kept for screen readers only: the visible hint was noise for everyone else. */}
        <span className="woa-sr" id="woa-composer-hint">{labels.keyHint}</span>
        {props.ended ? null : busy && !canReplace
          ? (
            <button type="button" className="woa-send" data-stop="true"
                    aria-label={labels.stop} disabled={stopping} onClick={props.onCancel}>
              <Square {...iconProps(12, 1.2)} fill="currentColor" />
            </button>
          )
          : (
            <button type="submit" className="woa-send" aria-label={labels.send}
                    data-off={disabled || !draft.trim() ? 'true' : undefined}
                    disabled={disabled || !draft.trim()}>
              <ArrowUp {...iconProps(16, 1.5)} />
            </button>
          )}
      </div>
      {/* The words are the server's, the number is the server's bound, read at the place
          it applies: a bare "2000" in warning orange explained nothing. */}
      {atLimit && <p className="woa-composer__limit">
        {labels.composerAtLimit} · {limits.composer_max_chars}
      </p>}
      <p className="woa-composer__disclaimer">{labels.aiDisclaimer}</p>
    </form>
  )
}
